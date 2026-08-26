"""Challenge room commit and helper-allocation coordination for Carbon."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
import logging
import time

from carbon.gamemanager.protocol import (
    NGL_FOOTER_WITH_TRAILER,
    REDUNDANT_BODY_SEPARATOR,
    with_plain_terminator,
)
from carbon.gamemanager.race_session import (
    decode_session_attributes,
    session_confirm,
    start_lock_host_properties,
)
from carbon.gamemanager.race_state import GameRaceState
from carbon.gamemanager.session_codec import encode_active
from carbon.gamemanager.session_object import (
    first_block_identity,
    is_session_object_complete,
)
from carbon.rebroadcaster.outbound import EndpointPublisher
from carbon.rebroadcaster.session_objects import SessionObjectCoordinator
from carbon.rebroadcaster.state import Address, EndpointWireState, SourceKey
from carbon.theater.directory import CarbonGame, CarbonTicketResolution
from carbon.theater.matchmaking import (
    selected_challenge_event,
    selected_race_property,
)
from carbon.transport.prototunnel import TunnelDatagram, TunnelPacket


_SEQUENCE_MASK = 0x0FFFFFFF

Replies = list[tuple[bytes, Address]]
SessionEndpoints = Callable[[str], tuple[Address, ...]]
IsHost = Callable[[CarbonTicketResolution], bool]
SourceKeyFor = Callable[[CarbonTicketResolution], SourceKey]
LockRoomAccess = Callable[..., bool]
SeedCountdown = Callable[[Replies, CarbonGame], None]
CurrentActiveGameBody = Callable[[bytes], bytes | None]
StateValue = Callable[[bytes], int | None]
ClockOrigin = Callable[[], float]


class RoomCommitCoordinator:
    """Own Challenge allocation release and the final room commit window."""

    def __init__(
        self,
        publisher: EndpointPublisher,
        session_objects: SessionObjectCoordinator,
        wires: MutableMapping[Address, EndpointWireState],
        bindings: Mapping[Address, CarbonTicketResolution],
        races: MutableMapping[str, GameRaceState],
        *,
        session_endpoints: SessionEndpoints,
        is_host: IsHost,
        source_key: SourceKeyFor,
        lock_room_access: LockRoomAccess,
        seed_countdown: SeedCountdown,
        current_active_game_body: CurrentActiveGameBody,
        state_value: StateValue,
        clock_origin: ClockOrigin,
        logger: logging.Logger | None = None,
    ) -> None:
        self.publisher = publisher
        self.session_objects = session_objects
        self._wires = wires
        self._bindings = bindings
        self._races = races
        self._session_endpoints = session_endpoints
        self._is_host = is_host
        self._source_key = source_key
        self._lock_room_access = lock_room_access
        self._seed_countdown = seed_countdown
        self._current_active_game_body = current_active_game_body
        self._state_value = state_value
        self._clock_origin = clock_origin
        self._room_commit_monotonic: dict[str, float] = {}
        self.log = logger or logging.getLogger(__name__)

    def clear_room(self, gid: str) -> None:
        room_id = str(gid)
        self._room_commit_monotonic.pop(room_id, None)
        for address, binding in tuple(self._bindings.items()):
            if binding.game.gid != room_id:
                continue
            wire = self._wires.get(address)
            if wire is None:
                continue
            wire.room_commit_prerequisite_sequence = 0
            wire.room_commit_prerequisite_wait_logged = False

    def hold_helper_allocation_generation(
        self,
        address: Address,
        binding: CarbonTicketResolution,
    ) -> bool:
        """Preserve a complete helper generation 2 until generation 3."""

        wire = self._wires[address]
        eligible_helper = (
            not self._is_host(binding)
            and binding.game.server_hosted
            and str(binding.game.properties.get("B-U-game_type", "")) == "2"
            and len(self._session_endpoints(binding.game.gid)) >= 2
        )
        already_held = (
            eligible_helper
            and wire.session_generation == 2
            and not wire.allocation_lock_triggered
            and bool(wire.pending_allocation_blocks)
            and wire.pending_allocation_object_id == wire.session_object_id
        )
        if already_held:
            return True

        should_hold = (
            eligible_helper
            and wire.session_generation == 2
            and is_session_object_complete(wire.session_blocks.values())
            and not wire.allocation_lock_triggered
            and not wire.pending_allocation_blocks
        )
        if not should_hold:
            return False
        wire.pending_allocation_object_id = wire.session_object_id
        wire.pending_allocation_blocks = tuple(wire.session_blocks.values())
        self.log.info(
            "Carbon GM release helper generation held for allocation window: "
            "gid=%s helper=%s:%d generation=2 object=%d blocks=%d "
            "session_confirmed=%d action=wait-for-generation3-offset1e4",
            binding.game.gid,
            address[0],
            address[1],
            wire.pending_allocation_object_id,
            len(wire.pending_allocation_blocks),
            int(wire.session_confirmed),
        )
        return True

    @staticmethod
    def _sequence_acked(acknowledgement: int, target: int) -> bool:
        delta = (int(acknowledgement) - int(target)) & _SEQUENCE_MASK
        return delta < 0x08000000

    def _window_acknowledged(self, targets: Mapping[Address, int]) -> bool:
        return bool(targets) and all(
            endpoint in self._wires
            and self._sequence_acked(
                self._wires[endpoint].last_client_acknowledgement,
                target,
            )
            for endpoint, target in targets.items()
        )

    def advance_helper_generation_barrier(
        self,
        replies: Replies,
        game: CarbonGame,
        *,
        current_address: Address,
    ) -> bool:
        """Advance allocation -> generation-3 -> room-commit ACK barriers.

        Official Challenge traffic places those publications in separate
        reliable receive windows.  When a client uploads every generation-3
        fragment in one UDP datagram, this barrier preserves the same order
        instead of sending all dependent windows back-to-back.
        """

        current_blocked = False
        for helper in self._session_endpoints(game.gid):
            binding = self._bindings.get(helper)
            wire = self._wires.get(helper)
            if (
                binding is None
                or wire is None
                or self._is_host(binding)
                or not wire.allocation_lock_triggered
            ):
                continue

            if wire.room_commit_prerequisite_sequence:
                self.maybe_finalize_room_session(replies, game)
                if wire.room_commit_prerequisite_sequence:
                    current_blocked = current_blocked or helper == current_address
                continue

            if wire.allocation_release_final_sequences:
                if not self._window_acknowledged(
                    wire.allocation_release_final_sequences
                ):
                    current_blocked = current_blocked or helper == current_address
                    if not wire.allocation_release_wait_logged:
                        wire.allocation_release_wait_logged = True
                        self.log.info(
                            "Carbon GM release generation 3 deferred: "
                            "gid=%s helper=%s:%d targets=%s "
                            "action=wait-for-allocation-acks",
                            game.gid,
                            helper[0],
                            helper[1],
                            ",".join(
                                f"{endpoint[0]}:{endpoint[1]}={target:07x}"
                                for endpoint, target in sorted(
                                    wire.allocation_release_final_sequences.items()
                                )
                            ),
                        )
                    continue

                wire.allocation_release_final_sequences.clear()
                wire.allocation_release_wait_logged = False
                reflection_targets: dict[Address, int] = {}
                before = int(wire.next_server_sequence) & _SEQUENCE_MASK
                self.session_objects.append_local_parts(
                    replies,
                    helper,
                    binding,
                )
                after = int(wire.next_server_sequence) & _SEQUENCE_MASK
                if after != before:
                    reflection_targets[helper] = (after - 1) & _SEQUENCE_MASK
                for peer in self._session_endpoints(game.gid):
                    if peer == helper:
                        continue
                    peer_wire = self._wires.get(peer)
                    if peer_wire is None:
                        continue
                    before = int(peer_wire.next_server_sequence) & _SEQUENCE_MASK
                    self.session_objects.append_remote_parts(
                        replies,
                        helper,
                        peer,
                        offsets={0, 0x1E4, 0x3C8},
                    )
                    after = int(peer_wire.next_server_sequence) & _SEQUENCE_MASK
                    if after != before:
                        reflection_targets[peer] = (after - 1) & _SEQUENCE_MASK
                wire.allocation_reflection_final_sequences = reflection_targets
                wire.allocation_reflection_wait_logged = False
                current_blocked = current_blocked or helper == current_address
                self.log.info(
                    "Carbon GM release generation 3 published after allocation ACK: "
                    "gid=%s helper=%s:%d endpoints=%d targets=%s "
                    "action=wait-for-generation3-acks",
                    game.gid,
                    helper[0],
                    helper[1],
                    len(reflection_targets),
                    ",".join(
                        f"{endpoint[0]}:{endpoint[1]}={target:07x}"
                        for endpoint, target in sorted(reflection_targets.items())
                    ) or "-",
                )
                continue

            if wire.allocation_reflection_final_sequences:
                if not self._window_acknowledged(
                    wire.allocation_reflection_final_sequences
                ):
                    current_blocked = current_blocked or helper == current_address
                    if not wire.allocation_reflection_wait_logged:
                        wire.allocation_reflection_wait_logged = True
                        self.log.info(
                            "Carbon GM release room commit deferred: "
                            "gid=%s helper=%s:%d targets=%s "
                            "action=wait-for-generation3-acks",
                            game.gid,
                            helper[0],
                            helper[1],
                            ",".join(
                                f"{endpoint[0]}:{endpoint[1]}={target:07x}"
                                for endpoint, target in sorted(
                                    wire.allocation_reflection_final_sequences.items()
                                )
                            ),
                        )
                    continue

                wire.allocation_reflection_final_sequences.clear()
                wire.allocation_reflection_wait_logged = False
                self.log.info(
                    "Carbon GM release generation 3 acknowledged: "
                    "gid=%s helper=%s:%d action=allow-room-commit",
                    game.gid,
                    helper[0],
                    helper[1],
                )
                self.maybe_finalize_room_session(replies, game)

        return current_blocked

    def release_pending_helper_allocation(
        self,
        replies: Replies,
        address: Address,
        binding: CarbonTicketResolution,
    ) -> None:
        """Release generation 2 after generation 3 reaches offset 0x1e4."""
        wire = self._wires[address]
        if (
            wire.allocation_lock_triggered
            or not wire.pending_allocation_blocks
            or not wire.session_confirmed
            or wire.session_generation < 3
            or 0 not in wire.session_blocks
            or 0x1E4 not in wire.session_blocks
        ):
            return

        current_object_id = wire.session_object_id
        current_generation = wire.session_generation
        current_blocks = dict(wire.session_blocks)
        current_reflected_object_id = wire.local_reflected_object_id
        pending_object_id = wire.pending_allocation_object_id
        pending_blocks = wire.pending_allocation_blocks

        wire.session_object_id = pending_object_id
        wire.session_blocks = {
            int.from_bytes(block[13:17], "big"): block
            for block in pending_blocks
        }
        wire.local_reflected_object_id = wire.pending_allocation_reflected_object_id
        endpoints = self._session_endpoints(binding.game.gid)

        if not wire.pending_allocation_offset_zero_sent:
            self.session_objects.append_local_parts(
                replies,
                address,
                binding,
                offsets={0},
            )
            for peer in endpoints:
                if peer != address:
                    self.session_objects.append_remote_parts(
                        replies,
                        address,
                        peer,
                        offsets={0},
                    )
            wire.pending_allocation_reflected_object_id = (
                wire.local_reflected_object_id
            )
            wire.pending_allocation_offset_zero_sent = True
            wire.session_object_id = current_object_id
            wire.session_generation = current_generation
            wire.session_blocks = current_blocks
            wire.local_reflected_object_id = current_reflected_object_id
            self.log.info(
                "Carbon GM release held helper generation offset zero sent: "
                "gid=%s helper=%s:%d held_generation=2 current_generation=%d "
                "current_offsets=%s endpoints=%d "
                "action=wait-for-generation3-offset3c8",
                binding.game.gid,
                address[0],
                address[1],
                current_generation,
                ",".join(hex(offset) for offset in sorted(current_blocks)),
                len(endpoints),
            )
            return

        if 0x3C8 not in current_blocks:
            wire.session_object_id = current_object_id
            wire.session_generation = current_generation
            wire.session_blocks = current_blocks
            wire.local_reflected_object_id = current_reflected_object_id
            return

        race = self._races.setdefault(binding.game.gid, GameRaceState())
        destinations = [address, *(peer for peer in endpoints if peer != address)]
        local_continuations, reflected_object_id, local_slot = (
            self.session_objects.select_local_parts(
                address,
                binding,
                offsets={0x1E4, 0x3C8},
            )
        )
        local_final_sequence = self._append_allocation_continuation_bundle(
            replies,
            address,
            continuations=local_continuations,
            max_hosted_players=binding.game.session.capacity,
        )
        release_targets: dict[Address, int] = {}
        if local_final_sequence is not None:
            release_targets[address] = local_final_sequence
        if local_continuations:
            self.log.info(
                "Carbon GM release local session object reflected with "
                "allocation HostProps: gid=%s dst=%s:%d object=%d slot=%d "
                "offsets=0x1e4,0x3c8",
                binding.game.gid,
                address[0],
                address[1],
                reflected_object_id,
                local_slot,
            )
        for peer in endpoints:
            if peer == address:
                continue
            remote_continuations = self.session_objects.select_remote_parts(
                address,
                peer,
                offsets={0x1E4, 0x3C8},
            )
            remote_final_sequence = self._append_allocation_continuation_bundle(
                replies,
                peer,
                continuations=remote_continuations,
                max_hosted_players=binding.game.session.capacity,
            )
            if remote_final_sequence is not None:
                release_targets[peer] = remote_final_sequence
            if remote_continuations:
                source_key = self._source_key(binding)
                cached_remote = self._wires[peer].published_remote_objects.get(
                    source_key,
                    remote_continuations,
                )
                remote_object_id, remote_pid, remote_name = first_block_identity(
                    cached_remote
                )
                self.log.info(
                    "Carbon GM release V681 reciprocal joiner session object sent "
                    "with allocation HostProps: gid=%s source=%s:%d "
                    "destination=%s:%d remote_object=%d offsets=0x1e4,0x3c8 "
                    "pid=%d name=%s blocks=2",
                    binding.game.gid,
                    address[0],
                    address[1],
                    peer[0],
                    peer[1],
                    remote_object_id,
                    remote_pid,
                    remote_name or "-",
                )
        self._lock_room_access(
            binding.game,
            race,
            reason="helper-allocation-lock",
        )

        wire.session_object_id = current_object_id
        wire.session_generation = current_generation
        wire.session_blocks = current_blocks
        wire.local_reflected_object_id = current_reflected_object_id
        source_key = self._source_key(binding)
        wire.published_session_offsets.pop(source_key, None)
        for peer_wire in self._wires.values():
            if peer_wire is wire:
                continue
            peer_wire.published_remote_objects.pop(source_key, None)
            peer_wire.published_session_offsets.pop(source_key, None)

        wire.pending_allocation_object_id = 0
        wire.pending_allocation_reflected_object_id = 0
        wire.pending_allocation_blocks = ()
        wire.pending_allocation_offset_zero_sent = False
        wire.allocation_lock_triggered = True
        wire.allocation_release_final_sequences = release_targets
        wire.allocation_reflection_final_sequences.clear()
        wire.allocation_release_wait_logged = False
        wire.allocation_reflection_wait_logged = False
        self.log.info(
            "Carbon GM release dedicated helper allocation lock sent: "
            "gid=%s helper=%s:%d held_generation=2 current_generation=%d "
            "current_offsets=%s endpoints=%d "
            "action=lock-then-reflect-held-generation",
            binding.game.gid,
            address[0],
            address[1],
            current_generation,
            ",".join(hex(offset) for offset in sorted(current_blocks)),
            len(destinations),
        )

    def release_stalled_helper_allocation_on_state7(
        self,
        replies: Replies,
        address: Address,
        binding: CarbonTicketResolution,
    ) -> bool:
        """Release complete generation 2 when host state 7 proves a deadlock.

        The normal retail path remains generation 2 -> generation 3 prefix ->
        allocation lock.  Gold Challenge clients can instead complete
        generation 2 and stop producing session objects while host state 7 is
        held behind the missing allocation lock.  The complete held object is
        sufficient for the capture-confirmed allocation publication in that
        case; every other room and generation continues through the normal
        path above.
        """
        wire = self._wires[address]
        race = self._races.setdefault(binding.game.gid, GameRaceState())
        if (
            wire.allocation_lock_triggered
            or not race.coop_host_state7_seen
            or not wire.session_confirmed
            or wire.session_generation != 2
            or not wire.pending_allocation_blocks
            or wire.pending_allocation_offset_zero_sent
            or self._is_host(binding)
            or not binding.game.server_hosted
            or str(binding.game.properties.get("B-U-game_type", "")) != "2"
            or len(self._session_endpoints(binding.game.gid)) < 2
        ):
            return False

        pending_offsets = {
            int.from_bytes(block[13:17], "big")
            for block in wire.pending_allocation_blocks
            if len(block) >= 17
        }
        if pending_offsets != {0, 0x1E4, 0x3C8}:
            return False

        endpoints = self._session_endpoints(binding.game.gid)
        self.session_objects.append_local_parts(
            replies,
            address,
            binding,
            offsets={0},
        )
        for peer in endpoints:
            if peer != address:
                self.session_objects.append_remote_parts(
                    replies,
                    address,
                    peer,
                    offsets={0},
                )

        local_continuations, reflected_object_id, local_slot = (
            self.session_objects.select_local_parts(
                address,
                binding,
                offsets={0x1E4, 0x3C8},
            )
        )
        self._append_allocation_continuation_bundle(
            replies,
            address,
            continuations=local_continuations,
            max_hosted_players=binding.game.session.capacity,
        )
        if local_continuations:
            self.log.info(
                "Carbon GM release stalled helper local generation reflected "
                "with allocation HostProps: gid=%s dst=%s:%d object=%d slot=%d "
                "offsets=0x0,0x1e4,0x3c8",
                binding.game.gid,
                address[0],
                address[1],
                reflected_object_id,
                local_slot,
            )

        for peer in endpoints:
            if peer == address:
                continue
            remote_continuations = self.session_objects.select_remote_parts(
                address,
                peer,
                offsets={0x1E4, 0x3C8},
            )
            self._append_allocation_continuation_bundle(
                replies,
                peer,
                continuations=remote_continuations,
                max_hosted_players=binding.game.session.capacity,
            )

        wire.pending_allocation_object_id = 0
        wire.pending_allocation_reflected_object_id = 0
        wire.pending_allocation_blocks = ()
        wire.pending_allocation_offset_zero_sent = False
        wire.allocation_lock_triggered = True
        wire.allocation_release_final_sequences.clear()
        wire.allocation_reflection_final_sequences.clear()
        wire.allocation_release_wait_logged = False
        wire.allocation_reflection_wait_logged = False
        self._lock_room_access(
            binding.game,
            race,
            reason="stalled-helper-allocation-lock",
        )
        self.log.info(
            "Carbon GM release stalled helper allocation lock sent: "
            "gid=%s helper=%s:%d generation=2 endpoints=%d "
            "reason=host-state7-without-generation3",
            binding.game.gid,
            address[0],
            address[1],
            len(endpoints),
        )
        return True

    def maybe_finalize_room_session(
        self,
        replies: Replies,
        game: CarbonGame,
        *,
        barrier_host: Address | None = None,
        barrier_token: bytes = b"",
    ) -> None:
        race = self._races.setdefault(game.gid, GameRaceState())
        if race.post_race_reopened:
            return
        endpoints = self._session_endpoints(game.gid)
        if len(endpoints) < 2:
            return
        if any(
            not self._wires.get(endpoint)
            or not self._wires[endpoint].session_confirmed
            or not is_session_object_complete(
                self._wires[endpoint].session_blocks.values()
            )
            for endpoint in endpoints
        ):
            return
        if any(
            self._wires[endpoint].pending_session_releases
            for endpoint in endpoints
        ):
            return

        if str(game.properties.get("B-U-game_type", "")) == "2":
            if any(
                not self._is_host(self._bindings[endpoint])
                and (
                    self._wires[endpoint].allocation_release_final_sequences
                    or self._wires[endpoint].allocation_reflection_final_sequences
                )
                for endpoint in endpoints
            ):
                return
            if barrier_host is not None:
                race.coop_barrier_host = barrier_host
            if barrier_token:
                race.coop_barrier_token = bytes(barrier_token)
            barrier_host = race.coop_barrier_host
            barrier_token = race.coop_barrier_token
            if not race.coop_host_state7_seen:
                return
            for endpoint in endpoints:
                binding = self._bindings[endpoint]
                if not self._is_host(binding):
                    self.release_stalled_helper_allocation_on_state7(
                        replies,
                        endpoint,
                        binding,
                    )
            if barrier_host is None or len(barrier_token) != 4:
                return
            if any(
                not self._is_host(self._bindings[endpoint])
                and not self._wires[endpoint].allocation_lock_triggered
                for endpoint in endpoints
            ):
                return

        for source in endpoints:
            for destination in endpoints:
                if source != destination:
                    self.session_objects.append_remote_parts(
                        replies,
                        source,
                        destination,
                        offsets={0, 0x1E4, 0x3C8},
                    )

        if str(game.properties.get("B-U-game_type", "")) == "2":
            self._publish_coop_room_commit(
                replies,
                game,
                barrier_host=barrier_host,
                barrier_token=barrier_token,
            )
            return
        self._seed_countdown(replies, game)

    def _coop_room_commit_context(
        self,
        game: CarbonGame,
        race: GameRaceState,
        endpoints: tuple[Address, ...],
        barrier_host: Address | None,
        barrier_token: bytes,
    ) -> tuple[bytes, bytes, bytes, Address, bytes, bytes] | None:
        if (
            barrier_host is None
            or len(barrier_token) != 4
            or not race.coop_host_state7_seen
            or any(
                not self._is_host(self._bindings[endpoint])
                and not self._wires[endpoint].allocation_lock_triggered
                for endpoint in endpoints
            )
        ):
            return None

        attributes = race.attributes
        timer = race.latest_room_timer
        if not attributes or not timer:
            self.log.info(
                "Carbon GM release co-op room commit deferred: "
                "gid=%s attributes=%d timer=%d",
                game.gid,
                int(bool(attributes)),
                int(bool(timer)),
            )
            return None

        try:
            decoded_attributes = decode_session_attributes(attributes)
        except Exception as exc:
            self.log.warning(
                "Carbon GM release co-op room commit deferred: gid=%s "
                "attributes=invalid error=%s",
                game.gid,
                exc,
            )
            return None
        if selected_challenge_event(decoded_attributes) is None:
            self.log.info(
                "Carbon GM release co-op room commit deferred: gid=%s "
                "challenge_event=missing-or-invalid action=wait-for-cs-event",
                game.gid,
            )
            return None

        raw_state7 = race.pending_coop_host_state7
        state7 = self._current_active_game_body(raw_state7)
        if state7 is None or self._state_value(state7) != 7:
            self.log.warning(
                "Carbon GM release co-op room commit deferred: gid=%s "
                "pending_state7=invalid bytes=%d",
                game.gid,
                len(raw_state7),
            )
            return None
        if state7 != raw_state7:
            self.log.info(
                "Carbon GM release Challenge state7 history trimmed: gid=%s "
                "raw_bytes=%d current_bytes=%d",
                game.gid,
                len(raw_state7),
                len(state7),
            )
            race.pending_coop_host_state7 = state7

        helper = next(
            (
                endpoint
                for endpoint in endpoints
                if not self._is_host(self._bindings[endpoint])
                and endpoint not in race.coop_committed_helpers
            ),
            None,
        )
        host_latency = (
            self._wires[barrier_host].latest_latency_info
            if barrier_host in endpoints
            else b""
        )
        helper_latency = (
            self._wires[helper].latest_latency_info
            if helper is not None
            else b""
        )
        if helper is None or not host_latency or not helper_latency:
            self.log.info(
                "Carbon GM release co-op room commit deferred: gid=%s "
                "host_latency=%d helper_latency=%d pending_state7=%d",
                game.gid,
                int(bool(host_latency)),
                int(bool(helper_latency)),
                int(bool(race.pending_coop_host_state7)),
            )
            return None
        return (
            bytes(attributes),
            bytes(timer),
            bytes(state7),
            helper,
            bytes(host_latency),
            bytes(helper_latency),
        )

    def _append_coop_room_commit_windows(
        self,
        replies: Replies,
        game: CarbonGame,
        barrier_host: Address,
        barrier_token: bytes,
        helper: Address,
        attributes: bytes,
        timer: bytes,
        state7: bytes,
        host_latency: bytes,
        helper_latency: bytes,
    ) -> None:
        elapsed = max(0.0, time.monotonic() - self._clock_origin())
        confirmation = session_confirm(barrier_token, elapsed)
        host_wire = self._wires[barrier_host]
        host_ack = int(host_wire.last_client_sequence) & _SEQUENCE_MASK
        host_base = int(host_wire.next_server_sequence) & _SEQUENCE_MASK
        commit = (attributes, timer, attributes, state7)
        host_packets = [
            TunnelPacket(
                1,
                encode_active(
                    (host_base + index) & _SEQUENCE_MASK,
                    host_ack,
                    bytes(logical) + b"\x04",
                ),
            )
            for index, logical in enumerate((confirmation, *commit))
        ]
        host_packets.append(
            TunnelPacket(
                1,
                encode_active(
                    0x10000000 | ((host_base + 5) & _SEQUENCE_MASK),
                    host_ack,
                    self.publisher.commudp_aggregate_payload(
                        (host_latency, state7)
                    ),
                ),
            )
        )
        host_packets.append(
            TunnelPacket(
                1,
                encode_active(
                    0x20000000 | ((host_base + 6) & _SEQUENCE_MASK),
                    host_ack,
                    self.publisher.commudp_aggregate_payload(
                        (helper_latency, host_latency, state7)
                    ),
                ),
            )
        )
        host_wire.next_server_sequence = (host_base + 7) & _SEQUENCE_MASK
        self.publisher.append_datagram(
            replies,
            TunnelDatagram(host_wire.next_offset_words, tuple(host_packets)),
            barrier_host,
            confirmation="room-commit-host",
        )
        self.log.info(
            "Carbon GM release secondary session 0x03 and retail co-op room "
            "commit bundled: gid=%s dst=%s:%d pid=%d token=%s clock=%.3f "
            "packets=7 flags=0,0,0,0,0,1,2",
            game.gid,
            barrier_host[0],
            barrier_host[1],
            self._bindings[barrier_host].participant.player_id,
            barrier_token.hex(),
            elapsed,
        )

        self.publisher.append_active_record_batch(
            replies,
            helper,
            (timer, attributes, state7, host_latency, helper_latency),
            confirmation="room-commit-helper",
        )
        decoded = decode_session_attributes(attributes)
        self.log.info(
            "Carbon GM release retail co-op state7 aggregates sent: gid=%s "
            "host=%s:%d helper=%s:%d host_flags=1,2 helper_flags=4 "
            "helper_records=timer,attributes,state7,host-latency,helper-latency "
            "game_type=%s help_type=%s game_mode=%s host_latency=%s "
            "helper_latency=%s",
            game.gid,
            barrier_host[0],
            barrier_host[1],
            helper[0],
            helper[1],
            decoded.get("game_type", "?"),
            decoded.get("help_type", "?"),
            decoded.get("game_mode", "?"),
            host_latency.hex(),
            helper_latency.hex(),
        )

    def _helper_commit_predecessor_acknowledged(
        self,
        game: CarbonGame,
        helper: Address,
    ) -> bool:
        """Wait for the helper ACK immediately preceding retail room commit."""

        wire = self._wires[helper]
        target = int(wire.room_commit_prerequisite_sequence) & _SEQUENCE_MASK
        if not target:
            target = (int(wire.next_server_sequence) - 1) & _SEQUENCE_MASK
            if self._sequence_acked(
                wire.last_client_acknowledgement,
                target,
            ):
                return True
            wire.room_commit_prerequisite_sequence = target

        if not self._sequence_acked(
            wire.last_client_acknowledgement,
            target,
        ):
            if not wire.room_commit_prerequisite_wait_logged:
                wire.room_commit_prerequisite_wait_logged = True
                self.log.info(
                    "Carbon GM release co-op room commit deferred: "
                    "gid=%s helper=%s:%d ack=%07x target=%07x "
                    "action=wait-for-helper-context-ack",
                    game.gid,
                    helper[0],
                    helper[1],
                    int(wire.last_client_acknowledgement) & _SEQUENCE_MASK,
                    target,
                )
            return False

        wire.room_commit_prerequisite_sequence = 0
        wire.room_commit_prerequisite_wait_logged = False
        self.log.info(
            "Carbon GM release helper context acknowledged: "
            "gid=%s helper=%s:%d ack=%07x target=%07x "
            "action=allow-room-commit",
            game.gid,
            helper[0],
            helper[1],
            int(wire.last_client_acknowledgement) & _SEQUENCE_MASK,
            target,
        )
        return True

    def _publish_coop_room_commit(
        self,
        replies: Replies,
        game: CarbonGame,
        *,
        barrier_host: Address | None,
        barrier_token: bytes,
    ) -> None:
        race = self._races.setdefault(game.gid, GameRaceState())
        endpoints = self._session_endpoints(game.gid)
        if len(endpoints) < 2:
            return
        pending_helpers = tuple(
            endpoint
            for endpoint in endpoints
            if not self._is_host(self._bindings[endpoint])
            and endpoint not in race.coop_committed_helpers
        )
        if not pending_helpers:
            return
        context = self._coop_room_commit_context(
            game,
            race,
            endpoints,
            barrier_host,
            barrier_token,
        )
        if context is None or barrier_host is None:
            return
        (
            attributes,
            timer,
            state7,
            helper,
            host_latency,
            helper_latency,
        ) = context
        if not self._helper_commit_predecessor_acknowledged(game, helper):
            return
        self._lock_room_access(game, race, reason="coop-room-commit")
        self._append_coop_room_commit_windows(
            replies,
            game,
            barrier_host,
            barrier_token,
            helper,
            attributes,
            timer,
            state7,
            host_latency,
            helper_latency,
        )
        race.coop_committed_helpers.add(helper)
        race.room_commit_sent = True
        self._room_commit_monotonic[game.gid] = time.monotonic()
        decoded = decode_session_attributes(attributes)
        event_property = selected_race_property(decoded)
        event_identity = (
            f"{event_property}:{decoded.get(event_property, '')}"
            if event_property is not None
            else "none"
        )
        self.log.info(
            "Carbon GM release co-op room commit sent: gid=%s endpoints=%d "
            "committed_helpers=%d pending_helpers=%d "
            "barrier_host=%s game_type=%s help_type=%s game_mode=%s "
            "car_tier=%s event=%s timer=%s",
            game.gid,
            len(endpoints),
            len(race.coop_committed_helpers),
            max(0, len(pending_helpers) - 1),
            f"{barrier_host[0]}:{barrier_host[1]}",
            decoded.get("game_type", "?"),
            decoded.get("help_type", "?"),
            decoded.get("game_mode", "?"),
            decoded.get("car_tier", "?"),
            event_identity,
            timer.hex(),
        )

    def _append_allocation_continuation_bundle(
        self,
        replies: Replies,
        destination: Address,
        *,
        continuations: tuple[bytes, ...],
        max_hosted_players: int,
    ) -> int | None:
        """Bundle held object continuations with capture-shaped HostProps."""
        if len(continuations) != 2:
            return None
        wire = self._wires[destination]
        footer = wire.footer
        base = int(wire.next_server_sequence) & _SEQUENCE_MASK
        acknowledgement = int(wire.last_client_sequence) & _SEQUENCE_MASK
        elements = [
            with_plain_terminator(properties.encode())
            for properties in start_lock_host_properties(
                int(max_hosted_players),
                wire_flag0=False,
            )
        ]
        hostprops = (
            elements[0] + footer + NGL_FOOTER_WITH_TRAILER,
            elements[1]
            + elements[0]
            + REDUNDANT_BODY_SEPARATOR
            + footer
            + NGL_FOOTER_WITH_TRAILER,
            elements[2]
            + elements[1]
            + REDUNDANT_BODY_SEPARATOR
            + elements[0]
            + REDUNDANT_BODY_SEPARATOR,
            (
                elements[3]
                + elements[2]
                + REDUNDANT_BODY_SEPARATOR
                + elements[1]
                + REDUNDANT_BODY_SEPARATOR
                + elements[0]
                + REDUNDANT_BODY_SEPARATOR
                + footer
                + NGL_FOOTER_WITH_TRAILER
            ),
        )
        bodies = (
            continuations[0] + b"\x04",
            continuations[1] + b"\x04",
            *hostprops,
        )
        flags = (0, 0, 0, 1, 2, 3)
        packets = tuple(
            TunnelPacket(
                1,
                encode_active(
                    ((flag & 0x0F) << 28)
                    | ((base + index) & _SEQUENCE_MASK),
                    acknowledgement,
                    body,
                ),
            )
            for index, (flag, body) in enumerate(zip(flags, bodies))
        )
        self.publisher.append_datagram(
            replies,
            TunnelDatagram(wire.next_offset_words, packets),
            destination,
            confirmation="room-allocation-continuation",
        )
        wire.next_server_sequence = (base + len(packets)) & _SEQUENCE_MASK
        return (base + len(packets) - 1) & _SEQUENCE_MASK
