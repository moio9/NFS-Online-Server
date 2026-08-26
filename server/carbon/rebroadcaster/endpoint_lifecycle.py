"""Endpoint disconnect, retry and expiry lifecycle for Carbon."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping, MutableSet
from contextlib import AbstractContextManager
import logging
import time
from typing import Any

from carbon.gamemanager.player_codec import encode_leave
from carbon.gamemanager.race_state import GameRaceState, RacePhase
from carbon.rebroadcaster.confirmations import ConfirmationManager
from carbon.rebroadcaster.gameplay_relay import GameplayRelayCoordinator
from carbon.rebroadcaster.handshake import EndpointHandshake
from carbon.rebroadcaster.room_lifecycle import RoomLifecycleCoordinator
from carbon.rebroadcaster.state import (
    Address,
    EndpointWireState,
    ReadyEpoch,
    ReadyStage,
    SourceKey,
)
from carbon.theater.directory import (
    CarbonGameDirectory,
    CarbonTicketResolution,
)


_SEQUENCE_MASK = 0x0FFFFFFF
_SEQUENCE_HALF = 0x08000000
_HOST_EXIT_DRAIN_TIMEOUT_SECONDS = 5.0

SessionEndpoints = Callable[[str], tuple[Address, ...]]
SourceKeyFor = Callable[[CarbonTicketResolution], SourceKey]
IsHost = Callable[[CarbonTicketResolution], bool]
class EndpointLifecycleCoordinator:
    """Own endpoint removal, account cleanup and reliable-window expiry."""

    def __init__(
        self,
        games: CarbonGameDirectory,
        gameplay_relay: GameplayRelayCoordinator,
        room_lifecycle: RoomLifecycleCoordinator,
        confirmations: ConfirmationManager,
        endpoints: MutableMapping[Address, EndpointHandshake],
        wires: MutableMapping[Address, EndpointWireState],
        bindings: MutableMapping[Address, CarbonTicketResolution],
        participant_endpoints: MutableMapping[SourceKey, Address],
        published_joins: MutableSet[tuple[str, int]],
        races: Mapping[str, GameRaceState],
        ready_epochs: Mapping[str, ReadyEpoch],
        lock: AbstractContextManager[Any],
        *,
        join_timeout_seconds: float,
        race_idle_timeout_seconds: float,
        session_endpoints: SessionEndpoints,
        source_key: SourceKeyFor,
        is_host: IsHost,
        logger: logging.Logger | None = None,
    ) -> None:
        self.games = games
        self.gameplay_relay = gameplay_relay
        self.room_lifecycle = room_lifecycle
        self.confirmations = confirmations
        self._endpoints = endpoints
        self._wire = wires
        self._bindings = bindings
        self._participant_endpoints = participant_endpoints
        self._published_joins = published_joins
        self._race = races
        self._ready_epochs = ready_epochs
        self._lock = lock
        self.join_timeout_seconds = float(join_timeout_seconds)
        self.race_idle_timeout_seconds = float(race_idle_timeout_seconds)
        self.session_endpoints = session_endpoints
        self._source_key = source_key
        self._is_host = is_host
        self.log = logger or logging.getLogger(__name__)

    @staticmethod
    def _sequence_acked(acknowledgement: int, target: int) -> bool:
        delta = (int(acknowledgement) - int(target)) & _SEQUENCE_MASK
        return delta < _SEQUENCE_HALF

    def drop_participant(self, gid: str, user_id: int) -> bool:
        """Handle Theater ECNL and report whether removal was deferred."""

        with self._lock:
            game_id = str(gid)
            key = (game_id, int(user_id))
            race = self._race.get(game_id)
            epoch = self._ready_epochs.get(game_id)
            ready_transition = (
                epoch is not None
                and epoch.stage
                in (
                    ReadyStage.SEED_SENT_WAIT_GUEST_13_15,
                    ReadyStage.STATE_PAIR_SENT_WAIT_HOST_NATIVE_BUNDLE,
                    ReadyStage.COUNTDOWN_ACTIVE,
                )
            )
            race_transport_owned = (
                race is not None
                and race.phase in (RacePhase.COUNTDOWN, RacePhase.RACING)
            )
            if ready_transition or race_transport_owned:
                self.log.info(
                    "Carbon GM deferred Theater ECNL during race transition: "
                    "gid=%s user_id=%d phase=%s ready_stage=%s "
                    "action=wait-for-udp-proof",
                    game_id,
                    int(user_id),
                    race.phase.name if race is not None else "NONE",
                    epoch.stage.name if epoch is not None else "NONE",
                )
                return True
            game = self.games.get(game_id)
            coordinator_left = bool(
                game is not None
                and (
                    (
                        game.server_hosted
                        and game.allocator_user_id is not None
                        and int(game.allocator_user_id) == int(user_id)
                    )
                    or (
                        not game.server_hosted
                        and int(game.host.user_id) == int(user_id)
                    )
                )
            )
            if coordinator_left:
                endpoint = self._participant_endpoints.get(key)
                if endpoint is not None:
                    self.drop_endpoint(endpoint, notify_peers=True)
                else:
                    participant = game.participants.get(int(user_id))
                    if participant is not None:
                        leave_body = encode_leave(participant.player_id, 0)
                        queued_at = time.monotonic()
                        for peer in self.session_endpoints(game_id):
                            peer_wire = self._wire.get(peer)
                            if peer_wire is None:
                                continue
                            self.confirmations.clear_endpoint(peer)
                            if leave_body not in peer_wire.pending_player_leaves:
                                peer_wire.pending_player_leaves.append(leave_body)
                            peer_wire.session_bootstrap_window = None
                            peer_wire.pending_ai_registration_windows.clear()
                            peer_wire.host_exit_queued_at = queued_at
                            peer_wire.host_exit_player_left_sent = False
                remaining = tuple(self.session_endpoints(game_id))
                if not remaining:
                    self.room_lifecycle.retire_transport_state(game_id)
                self.log.info(
                    "Carbon GM coordinator left; PlayerLeft queued: "
                    "gid=%s user_id=%d remaining_endpoints=%d",
                    game_id,
                    int(user_id),
                    len(remaining),
                )
                return False
            addr = self._participant_endpoints.get(key)
            if addr is not None:
                self.drop_endpoint(addr, notify_peers=True)
            return False

    def force_disconnect_user(self, user_id: int, *, reason: str) -> int:
        """Immediately remove one account identity from every Carbon room."""

        uid = int(user_id)
        if uid <= 0:
            return 0

        affected_gids: set[str] = set()
        coordinator_gids: set[str] = set()
        with self._lock:
            target_endpoints: list[Address] = []
            for endpoint, resolution in tuple(self._bindings.items()):
                if int(resolution.participant.identity.user_id) != uid:
                    continue
                gid = str(resolution.game.gid)
                affected_gids.add(gid)
                if self._is_host(resolution):
                    coordinator_gids.add(gid)
                target_endpoints.append(endpoint)

            for game in tuple(self.games.list()):
                gid = str(game.gid)
                is_coordinator = bool(
                    (
                        game.server_hosted
                        and game.allocator_user_id is not None
                        and int(game.allocator_user_id) == uid
                    )
                    or (
                        not game.server_hosted
                        and int(game.host.user_id) == uid
                    )
                )
                if uid in game.participants or is_coordinator:
                    affected_gids.add(gid)
                if is_coordinator:
                    coordinator_gids.add(gid)

            for endpoint in target_endpoints:
                self.drop_endpoint(endpoint, notify_peers=True)

            for gid in sorted(affected_gids):
                game = self.games.get(gid)
                retire_room = gid in coordinator_gids or game is None
                if retire_room:
                    for endpoint in tuple(self.session_endpoints(gid)):
                        self.drop_endpoint(endpoint, notify_peers=False)
                    if game is not None:
                        self.games.retire(gid, reason=reason)
                    self.room_lifecycle.retire_transport_state(gid)
                    continue

                if uid in game.participants:
                    self.games.leave(gid, uid, reason=reason)
                if self.games.get(gid) is None:
                    for endpoint in tuple(self.session_endpoints(gid)):
                        self.drop_endpoint(endpoint, notify_peers=False)
                    self.room_lifecycle.retire_transport_state(gid)

        if affected_gids:
            self.log.warning(
                "Carbon GM account policy cleanup: "
                "user_id=%d rooms=%d reason=%s",
                uid,
                len(affected_gids),
                reason,
            )
        return len(affected_gids)

    def drop_endpoint(
        self,
        addr: Address,
        *,
        notify_peers: bool = False,
    ) -> None:
        binding = self._bindings.pop(addr, None)
        self._endpoints.pop(addr, None)
        self.confirmations.clear_endpoint(addr)
        if binding is None:
            self._wire.pop(addr, None)
            return
        coordinator_left = bool(notify_peers and self._is_host(binding))
        room_retired = bool(self._wire.get(addr, EndpointWireState()).host_exit_queued_at)
        if notify_peers:
            leave_body = encode_leave(binding.participant.player_id, 0)
            queued_at = time.monotonic()
            for peer_addr, peer_binding in self._bindings.items():
                if (
                    peer_addr == addr
                    or peer_binding.game.gid != binding.game.gid
                ):
                    continue
                peer_wire = self._wire.get(peer_addr)
                if (
                    peer_wire is not None
                    and leave_body not in peer_wire.pending_player_leaves
                ):
                    peer_wire.pending_player_leaves.append(leave_body)
                if peer_wire is not None and coordinator_left:
                    # Old room confirmations must not overtake the terminal
                    # host leave. PlayerLeft becomes the only live reliable
                    # window for each remaining endpoint.
                    self.confirmations.clear_endpoint(peer_addr)
                    peer_wire.session_bootstrap_window = None
                    peer_wire.pending_ai_registration_windows.clear()
                    peer_wire.host_exit_queued_at = queued_at
                    peer_wire.host_exit_player_left_sent = False
        self._wire.pop(addr, None)
        source_key = self._source_key(binding)
        if self._participant_endpoints.get(source_key) == addr:
            self._participant_endpoints.pop(source_key, None)
        self._published_joins.discard(
            (binding.game.gid, int(binding.participant.player_id))
        )
        for peer_wire in self._wire.values():
            peer_wire.pending_session_releases.discard(addr)
            peer_wire.published_remote_objects.pop(source_key, None)
            peer_wire.published_session_offsets.pop(source_key, None)
        self.room_lifecycle.abort_ready_epoch(
            binding.game.gid,
            reason=f"endpoint-drop:{binding.participant.player_id}",
        )
        if not any(
            item.game.gid == binding.game.gid
            for item in self._bindings.values()
        ):
            if coordinator_left or room_retired:
                self.room_lifecycle.retire_transport_state(binding.game.gid)
            else:
                self.room_lifecycle.clear_unbound_transport_state(binding.game.gid)

    def publish_pending_player_leaves(
        self,
        replies: list[tuple[bytes, Address]],
        addr: Address,
        binding: CarbonTicketResolution,
        wire: EndpointWireState,
    ) -> int:
        """Publish queued PlayerLeft records on the destination's live stream."""

        if not wire.pending_player_leaves:
            return 0
        pending_leaves = tuple(wire.pending_player_leaves)
        wire.pending_player_leaves.clear()
        self.gameplay_relay.publisher.append_active_bodies(
            replies,
            addr,
            tuple(body + b"\x04" for body in pending_leaves),
            confirmation="player-left",
        )
        if wire.host_exit_queued_at:
            wire.host_exit_player_left_sent = True
        self.log.info(
            "Carbon GM release PlayerLeft sent: gid=%s dst=%s:%d count=%d "
            "host_exit=%d",
            binding.game.gid,
            addr[0],
            addr[1],
            len(pending_leaves),
            int(bool(wire.host_exit_queued_at)),
        )
        return len(pending_leaves)

    def poll_retries(
        self,
        *,
        now: float | None = None,
    ) -> list[tuple[bytes, Address]]:
        """Advance reliable windows and remove expired lifecycle state."""

        current = time.monotonic() if now is None else float(now)
        replies = self.confirmations.poll(now=current)
        expired_endpoints: dict[Address, str] = {}
        drained_host_exit_endpoints: dict[Address, str] = {}
        with self._lock:
            for addr, wire in tuple(self._wire.items()):
                binding = self._bindings.get(addr)
                if binding is not None:
                    self.publish_pending_player_leaves(
                        replies,
                        addr,
                        binding,
                        wire,
                    )
                if wire.host_exit_queued_at:
                    player_left_pending = any(
                        window.label == "player-left"
                        for window in self.confirmations.pending(addr)
                    )
                    if (
                        wire.host_exit_player_left_sent
                        and not player_left_pending
                    ):
                        drained_host_exit_endpoints[addr] = "acknowledged"
                        continue
                    if (
                        current - wire.host_exit_queued_at
                        >= _HOST_EXIT_DRAIN_TIMEOUT_SECONDS
                    ):
                        drained_host_exit_endpoints[addr] = "timeout"
                        continue
                window = wire.session_bootstrap_window
                if window is None:
                    continue
                if binding is None:
                    wire.session_bootstrap_window = None
                    continue
                if (
                    wire.session_confirmed
                    or bool(wire.session_blocks)
                ):
                    proof = (
                        "session-confirmed"
                        if wire.session_confirmed
                        else "session-object"
                    )
                    self.log.info(
                        "Carbon GM release session bootstrap application "
                        "confirmation observed: gid=%s dst=%s:%d pid=%d "
                        "proof=%s ack=%07x target=%07x",
                        binding.game.gid,
                        addr[0],
                        addr[1],
                        binding.participant.player_id,
                        proof,
                        int(wire.last_client_acknowledgement) & _SEQUENCE_MASK,
                        window.final_sequence,
                    )
                    wire.session_bootstrap_window = None
                    continue
                if (
                    not window.transport_acknowledged
                    and self._sequence_acked(
                        wire.last_client_acknowledgement,
                        window.final_sequence,
                    )
                ):
                    # CommUDP owns reliable delivery.  Once its cumulative ACK
                    # covers this exact encrypted window, do not manufacture a
                    # second application publication under fresh sequences.
                    # The official flows wait for the client's session object
                    # (or native OLMSG 0x02) after this transport proof.
                    window.transport_acknowledged = True
                    self.log.info(
                        "Carbon GM release session bootstrap transport ACK "
                        "observed; exact-wire retry stopped, awaiting "
                        "application confirmation: "
                        "gid=%s dst=%s:%d pid=%d ack=%07x target=%07x",
                        binding.game.gid,
                        addr[0],
                        addr[1],
                        binding.participant.player_id,
                        int(wire.last_client_acknowledgement) & _SEQUENCE_MASK,
                        window.final_sequence,
                    )
                exhaustion_reason = (
                    "deadline"
                    if (
                        window.transport_acknowledged
                        and current >= window.retry.deadline
                    )
                    else (
                        None
                        if window.transport_acknowledged
                        else window.retry.exhaustion_reason(current)
                    )
                )
                if exhaustion_reason is not None:
                    self.log.warning(
                        "Carbon GM release session bootstrap application wait "
                        "expired: "
                        "gid=%s dst=%s:%d pid=%d attempts=%d ack=%07x "
                        "target=%07x reason=%s elapsed=%.3f",
                        binding.game.gid,
                        addr[0],
                        addr[1],
                        binding.participant.player_id,
                        window.retry.retries_sent,
                        int(wire.last_client_acknowledgement) & _SEQUENCE_MASK,
                        window.final_sequence,
                        exhaustion_reason,
                        max(0.0, current - window.retry.opened_at),
                    )
                    wire.session_bootstrap_window = None
                    expired_endpoints[addr] = (
                        f"session-bootstrap-{exhaustion_reason}"
                    )
                    continue
                if window.transport_acknowledged:
                    continue
                if not window.retry.due(current):
                    continue
                replies.extend((payload, addr) for payload in window.records)
                datagrams = len(window.records)
                delay = window.retry.record_retry(current)
                self.log.info(
                    "Carbon GM release session bootstrap retried: "
                    "gid=%s dst=%s:%d pid=%d attempt=%d datagrams=%d "
                    "mode=%s ack=%07x target=%07x next_retry=%.3f",
                    binding.game.gid,
                    addr[0],
                    addr[1],
                    binding.participant.player_id,
                    window.retry.retries_sent,
                    datagrams,
                    "exact-wire",
                    int(wire.last_client_acknowledgement) & _SEQUENCE_MASK,
                    window.final_sequence,
                    delay,
                )
            for addr, binding in tuple(self._bindings.items()):
                if addr in expired_endpoints:
                    continue
                wire = self._wire.get(addr)
                if wire is not None and wire.host_exit_queued_at:
                    continue
                if self.gameplay_relay.update_ai_registration_delivery(
                    replies,
                    addr,
                    binding,
                    reason="poll",
                    now=current,
                ):
                    expired_endpoints[addr] = "ai-registration-timeout"

            for addr, reason in expired_endpoints.items():
                self.expire_endpoint(addr, reason=reason)
            for addr, outcome in drained_host_exit_endpoints.items():
                binding = self._bindings.get(addr)
                if binding is None:
                    continue
                gid = binding.game.gid
                self.log.info(
                    "Carbon GM host-exit delivery drained: gid=%s "
                    "dst=%s:%d outcome=%s",
                    gid,
                    addr[0],
                    addr[1],
                    outcome,
                )
                self.drop_endpoint(addr, notify_peers=False)
                if not self.session_endpoints(gid):
                    self.room_lifecycle.retire_transport_state(gid)
            self.expire_stale_rooms(current)
        return replies

    def expire_endpoint(self, addr: Address, *, reason: str) -> None:
        """Drop one failed participant and retire its room when required."""

        binding = self._bindings.get(addr)
        if binding is None:
            self.drop_endpoint(addr)
            return
        gid = binding.game.gid
        user_id = binding.participant.identity.user_id
        self.log.warning(
            "Carbon GM endpoint expired: gid=%s dst=%s:%d pid=%d "
            "user_id=%d reason=%s",
            gid,
            addr[0],
            addr[1],
            binding.participant.player_id,
            user_id,
            reason,
        )
        self.drop_endpoint(addr, notify_peers=True)
        self.games.leave(gid, user_id, reason=reason)
        if self.games.get(gid) is None:
            for peer in tuple(self.session_endpoints(gid)):
                self.drop_endpoint(peer)

    def retire_room(self, gid: str, *, reason: str) -> None:
        for addr in tuple(self.session_endpoints(gid)):
            self.drop_endpoint(addr)
        self.games.retire(gid, reason=reason)

    def expire_stale_rooms(self, now: float) -> None:
        """Retire local lifecycle state which has no wire progress."""

        current = float(now)
        for game in tuple(self.games.list()):
            age = current - float(game.created_at)
            if (
                game.server_hosted
                and not game.participants
                and age >= self.join_timeout_seconds
            ):
                self.retire_room(
                    game.gid,
                    reason="allocation-without-egam-timeout",
                )
                continue

            expired_members = [
                participant
                for participant in tuple(game.participants.values())
                if (
                    (game.gid, participant.identity.user_id)
                    not in self._participant_endpoints
                    and current - float(participant.entered_at)
                    >= self.join_timeout_seconds
                )
            ]
            for participant in expired_members:
                self.log.warning(
                    "Carbon directory join expired before GameManager bind: "
                    "gid=%s pid=%d user_id=%d elapsed=%.3f",
                    game.gid,
                    participant.player_id,
                    participant.identity.user_id,
                    max(0.0, current - float(participant.entered_at)),
                )
                self.games.leave(
                    game.gid,
                    participant.identity.user_id,
                    reason="gamemanager-bind-timeout",
                )
            if self.games.get(game.gid) is None:
                for addr in tuple(self.session_endpoints(game.gid)):
                    self.drop_endpoint(addr)
                continue

            race = self._race.get(game.gid)
            if (
                race is None
                or race.phase < RacePhase.COUNTDOWN
                or race.phase >= RacePhase.FINISHED
            ):
                continue
            endpoints = self.session_endpoints(game.gid)
            if not endpoints:
                continue
            # Every participant is required once the race lifecycle owns the
            # room. Use the oldest endpoint so one silent destination cannot
            # be masked by a peer which is still transmitting.
            activity = min(
                (
                    self._wire[addr].last_activity_at
                    or self._wire[addr].bound_at
                    or float(game.created_at)
                )
                for addr in endpoints
            )
            idle = current - float(activity)
            if idle < self.race_idle_timeout_seconds:
                continue
            self.log.warning(
                "Carbon GM race retired after transport idle timeout: "
                "gid=%s phase=%s endpoints=%d idle=%.3f timeout=%.3f",
                game.gid,
                race.phase.name,
                len(endpoints),
                max(0.0, idle),
                self.race_idle_timeout_seconds,
            )
            self.retire_room(
                game.gid,
                reason="race-transport-idle-timeout",
            )
