"""Challenge invite session-object barriers for Carbon.

The coordinator owns the ordering between complete session-object generations,
the invited-helper clock probe, the native 0x02 token and the server 0x03
confirmation.  Room commit and allocation-lock policy remain explicit effects
provided by the UDP service.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
import logging
import time

from carbon.gamemanager.protocol import with_plain_terminator
from carbon.gamemanager.race_session import (
    logical_type,
    session_confirm,
    session_probe,
)
from carbon.gamemanager.race_state import GameRaceState
from carbon.gamemanager.session_codec import encode_active
from carbon.gamemanager.session_object import is_session_object_complete
from carbon.rebroadcaster.outbound import EndpointPublisher
from carbon.rebroadcaster.session_objects import SessionObjectCoordinator
from carbon.rebroadcaster.state import Address, EndpointWireState, SourceKey
from carbon.theater.directory import CarbonGame, CarbonTicketResolution
from carbon.transport.prototunnel import TunnelDatagram, TunnelPacket


_SEQUENCE_MASK = 0x0FFFFFFF
_SEQUENCE_HALF = 0x08000000

Replies = list[tuple[bytes, Address]]
SessionEndpoints = Callable[[str], tuple[Address, ...]]
HostEndpoint = Callable[[CarbonGame], Address | None]
IsHost = Callable[[CarbonTicketResolution], bool]
SourceKeyFor = Callable[[CarbonTicketResolution], SourceKey]
FinalizeRoom = Callable[[Replies, CarbonGame], None]
ClockOrigin = Callable[[], float]
HoldHelperAllocation = Callable[[Address, CarbonTicketResolution], bool]


class InviteSessionBarrierCoordinator:
    """Own the capture-backed invited-helper session confirmation barrier."""

    def __init__(
        self,
        publisher: EndpointPublisher,
        session_objects: SessionObjectCoordinator,
        wires: MutableMapping[Address, EndpointWireState],
        bindings: Mapping[Address, CarbonTicketResolution],
        races: MutableMapping[str, GameRaceState],
        *,
        session_endpoints: SessionEndpoints,
        host_endpoint: HostEndpoint,
        is_host: IsHost,
        source_key: SourceKeyFor,
        finalize_room: FinalizeRoom,
        hold_helper_allocation: HoldHelperAllocation,
        clock_origin: ClockOrigin,
        logger: logging.Logger | None = None,
    ) -> None:
        self.publisher = publisher
        self.session_objects = session_objects
        self._wires = wires
        self._bindings = bindings
        self._races = races
        self._session_endpoints = session_endpoints
        self._host_endpoint = host_endpoint
        self._is_host = is_host
        self._source_key = source_key
        self._finalize_room = finalize_room
        self._hold_helper_allocation = hold_helper_allocation
        self._clock_origin = clock_origin
        self.log = logger or logging.getLogger(__name__)

    @staticmethod
    def _sequence_acked(acknowledgement: int, target: int) -> bool:
        delta = (int(acknowledgement) - int(target)) & _SEQUENCE_MASK
        return delta < _SEQUENCE_HALF

    def _publish_complete_session_prelude(
        self,
        replies: Replies,
        address: Address,
        binding: CarbonTicketResolution,
        wire: EndpointWireState,
    ) -> None:
        """Publish pre-confirm host continuations and the ordinary clock probe."""
        if not self._is_host(binding) and not wire.session_confirmed:
            host_address = self._host_endpoint(binding.game)
            host_wire = (
                self._wires.get(host_address)
                if host_address is not None
                else None
            )
            if (
                host_address is not None
                and host_wire is not None
                and host_wire.session_bootstrap_sent
            ):
                source_key = self._source_key(self._bindings[host_address])
                published = wire.published_session_offsets.get(source_key, set())
                if 0 in published:
                    self.session_objects.append_remote_parts(
                        replies,
                        host_address,
                        address,
                        offsets={0x1E4, 0x3C8},
                        bundle=True,
                    )

        is_invited_helper = bool(binding.participant.invite_remote_player_id)
        if not is_invited_helper and not wire.clock_probe_sent:
            self.publisher.append_active_body(
                replies,
                address,
                with_plain_terminator(session_probe()),
                confirmation="session-clock-probe",
            )
            wire.clock_probe_sent = True
            self.log.info(
                "Carbon GM release ClockSyncRequest sent: gid=%s dst=%s:%d pid=%d",
                binding.game.gid,
                address[0],
                address[1],
                binding.participant.player_id,
            )

    def _reflect_complete_session_generation(
        self,
        replies: Replies,
        address: Address,
        binding: CarbonTicketResolution,
        wire: EndpointWireState,
    ) -> bool:
        """Publish a completed local generation and report a finished host path."""
        if wire.session_confirmation_pending:
            self.session_objects.append_local_parts(
                replies,
                address,
                binding,
                offsets={0},
            )
        elif wire.session_confirmed:
            self.session_objects.append_local_parts(replies, address, binding)

        if wire.session_confirmation_pending and not self._is_host(binding):
            for peer in self._session_endpoints(binding.game.gid):
                if peer != address:
                    self.session_objects.append_remote_parts(
                        replies,
                        address,
                        peer,
                        offsets={0},
                    )

        if wire.session_confirmed:
            for peer in self._session_endpoints(binding.game.gid):
                if peer == address:
                    continue
                peer_wire = self._wires.get(peer)
                offsets = {0, 0x1E4, 0x3C8}
                if (
                    self._is_host(binding)
                    and peer_wire is not None
                    and not peer_wire.session_confirmed
                ):
                    offsets = {0}
                self.session_objects.append_remote_parts(
                    replies,
                    address,
                    peer,
                    offsets=offsets,
                )

        if wire.session_confirmed and not self._is_host(binding):
            self._finalize_room(replies, binding.game)

        if not self._is_host(binding):
            return False
        for peer in self._session_endpoints(binding.game.gid):
            if peer != address:
                self.session_objects.append_remote_parts(
                    replies,
                    address,
                    peer,
                    offsets={0},
                )
        return True

    def _advance_invite_session_barrier(
        self,
        replies: Replies,
        address: Address,
        binding: CarbonTicketResolution,
        wire: EndpointWireState,
    ) -> None:
        """Release the invited helper/host 0x01 barrier after real ACK progress."""
        if wire.session_probe_sent:
            return
        host_address = self._host_endpoint(binding.game)
        host_wire = (
            self._wires.get(host_address)
            if host_address is not None
            else None
        )
        if (
            host_address is None
            or host_wire is None
            or not host_wire.session_bootstrap_sent
        ):
            return
        source_key = self._source_key(self._bindings[host_address])
        offsets = wire.published_session_offsets.get(source_key, set())
        if 0 not in offsets:
            return

        continuation_final = int(
            wire.invite_host_continuation_final_sequence
        ) & _SEQUENCE_MASK
        if (
            continuation_final
            and not self._sequence_acked(
                wire.last_client_acknowledgement,
                continuation_final,
            )
        ):
            if not wire.invite_host_barrier_pending:
                wire.invite_host_barrier_deferred_client_sequence = (
                    int(wire.last_client_sequence) & _SEQUENCE_MASK
                )
                wire.invite_host_barrier_progress_wait_logged = False
                self.log.info(
                    "Carbon GM release session barrier 0x01 deferred: "
                    "gid=%s guest=%s:%d host=%s:%d guest_seq=%07x guest_ack=%07x "
                    "required_ack=%07x action=wait-for-final-host-continuation-ack",
                    binding.game.gid,
                    address[0],
                    address[1],
                    host_address[0],
                    host_address[1],
                    int(wire.last_client_sequence) & _SEQUENCE_MASK,
                    int(wire.last_client_acknowledgement) & _SEQUENCE_MASK,
                    continuation_final,
                )
            wire.invite_host_barrier_pending = True
            return

        if wire.invite_host_barrier_pending:
            deferred_sequence = (
                int(wire.invite_host_barrier_deferred_client_sequence)
                & _SEQUENCE_MASK
            )
            required_client_sequence = (deferred_sequence + 1) & _SEQUENCE_MASK
            if not self._sequence_acked(
                wire.last_client_sequence,
                required_client_sequence,
            ):
                if not wire.invite_host_barrier_progress_wait_logged:
                    self.log.info(
                        "Carbon GM release session barrier 0x01 remains deferred: "
                        "gid=%s guest=%s:%d host=%s:%d guest_seq=%07x "
                        "required_seq=%07x guest_ack=%07x required_ack=%07x "
                        "action=wait-for-client-application-progress",
                        binding.game.gid,
                        address[0],
                        address[1],
                        host_address[0],
                        host_address[1],
                        int(wire.last_client_sequence) & _SEQUENCE_MASK,
                        required_client_sequence,
                        int(wire.last_client_acknowledgement) & _SEQUENCE_MASK,
                        continuation_final,
                    )
                    wire.invite_host_barrier_progress_wait_logged = True
                return

        self.publisher.append_active_body(
            replies,
            address,
            with_plain_terminator(session_probe()),
            confirmation="session-helper-clock-probe",
        )
        self.log.info(
            "Carbon GM release invited helper standalone ClockSyncRequest sent: "
            "gid=%s dst=%s:%d pid=%d guest_seq=%07x guest_ack=%07x "
            "action=await-native-helper-0x02",
            binding.game.gid,
            address[0],
            address[1],
            binding.participant.player_id,
            int(wire.last_client_sequence) & _SEQUENCE_MASK,
            int(wire.last_client_acknowledgement) & _SEQUENCE_MASK,
        )
        self.publisher.append_active_body(
            replies,
            host_address,
            with_plain_terminator(session_probe()),
            confirmation="session-host-clock-probe",
        )
        host_wire.pending_session_releases.add(address)
        wire.session_probe_sent = True
        wire.invite_host_barrier_pending = False
        wire.invite_host_barrier_deferred_client_sequence = 0
        wire.invite_host_barrier_progress_wait_logged = False
        self.log.info(
            "Carbon GM release session barrier 0x01 sent: "
            "gid=%s guest=%s:%d host=%s:%d guest_ack=%07x required_ack=%07x "
            "action=wait-for-host-0x02",
            binding.game.gid,
            address[0],
            address[1],
            host_address[0],
            host_address[1],
            int(wire.last_client_acknowledgement) & _SEQUENCE_MASK,
            continuation_final,
        )

    def handle_complete_session_object(
        self,
        replies: Replies,
        address: Address,
        binding: CarbonTicketResolution,
    ) -> None:
        wire = self._wires[address]
        self._publish_complete_session_prelude(
            replies,
            address,
            binding,
            wire,
        )
        if self._hold_helper_allocation(address, binding):
            return
        if self._reflect_complete_session_generation(
            replies,
            address,
            binding,
            wire,
        ):
            return
        self._advance_invite_session_barrier(
            replies,
            address,
            binding,
            wire,
        )

    def handle_session_token(
        self,
        replies: Replies,
        address: Address,
        binding: CarbonTicketResolution,
        token: bytes,
    ) -> None:
        wire = self._wires[address]

        released_remote_continuations = False
        if wire.pending_session_releases:
            for guest_address in tuple(wire.pending_session_releases):
                self.session_objects.append_remote_parts(
                    replies,
                    address,
                    guest_address,
                    offsets={0x1E4, 0x3C8},
                )
                wire.pending_session_releases.discard(guest_address)
                self.log.info(
                    "Carbon GM release host 0x02 confirmed remote continuations: "
                    "gid=%s host=%s:%d guest=%s:%d token=%s",
                    binding.game.gid,
                    address[0],
                    address[1],
                    guest_address[0],
                    guest_address[1],
                    bytes(token).hex(),
                )
                released_remote_continuations = True

        if released_remote_continuations:
            race = self._races.setdefault(binding.game.gid, GameRaceState())
            race.coop_barrier_host = address
            race.coop_barrier_token = bytes(token)

        if not is_session_object_complete(wire.session_blocks.values()):
            self.log.info(
                "Carbon GM release session 0x02 deferred: "
                "gid=%s src=%s:%d reason=incomplete-local-object token=%s",
                binding.game.gid,
                address[0],
                address[1],
                bytes(token).hex(),
            )
            return
        if not wire.session_confirmed:
            wire.session_token = bytes(token)
            wire.session_confirmation_pending = True
            self.log.info(
                "Carbon GM release client 0x02 accepted: "
                "gid=%s src=%s:%d pid=%d token=%s action=wait-for-client-ack",
                binding.game.gid,
                address[0],
                address[1],
                binding.participant.player_id,
                bytes(token).hex(),
            )

    def append_session_confirmation(
        self,
        replies: Replies,
        address: Address,
        binding: CarbonTicketResolution,
    ) -> None:
        wire = self._wires[address]
        if len(wire.session_token) != 4 or wire.session_confirmed:
            return
        elapsed = max(0.0, time.monotonic() - self._clock_origin())
        continuations, reflected_object_id, local_slot = (
            self.session_objects.select_local_parts(
                address,
                binding,
                offsets={0x1E4, 0x3C8},
            )
        )
        acknowledgement = int(wire.last_client_sequence) & _SEQUENCE_MASK
        packets: list[TunnelPacket] = []
        for block in continuations:
            packets.append(
                TunnelPacket(
                    1,
                    encode_active(
                        self.publisher.take_server_sequence(wire),
                        acknowledgement,
                        block + b"\x04",
                    ),
                )
            )
        sequence = self.publisher.take_server_sequence(wire)
        packets.append(
            TunnelPacket(
                1,
                encode_active(
                    sequence,
                    acknowledgement,
                    with_plain_terminator(
                        session_confirm(wire.session_token, elapsed)
                    ),
                ),
            )
        )
        self.publisher.append_datagram(
            replies,
            TunnelDatagram(wire.next_offset_words, tuple(packets)),
            address,
            confirmation="session-confirmation",
        )
        if continuations:
            self.log.info(
                "Carbon GM release local session continuations bundled with 0x03: "
                "gid=%s dst=%s:%d object=%d slot=%d offsets=0x1e4,0x3c8 "
                "ack=%07x",
                binding.game.gid,
                address[0],
                address[1],
                reflected_object_id,
                local_slot,
                acknowledgement,
            )
        wire.session_confirmation_pending = False
        wire.session_confirmed = True
        self.log.info(
            "Carbon GM release session 0x03 sent: "
            "gid=%s dst=%s:%d pid=%d token=%s clock=%.3f",
            binding.game.gid,
            address[0],
            address[1],
            binding.participant.player_id,
            wire.session_token.hex(),
            elapsed,
        )
        if not self._is_host(binding):
            for peer in self._session_endpoints(binding.game.gid):
                if peer == address:
                    continue
                self.session_objects.append_remote_parts(
                    replies,
                    address,
                    peer,
                    offsets={0x1E4, 0x3C8},
                    bundle=True,
                    prefer_invite_sequence=False,
                )
        self._finalize_room(replies, binding.game)

    def hold_preconfirm(self, destination: Address, logical: bytes) -> bool:
        """Keep invite bootstrap and pre-0x02 reliable sequence gaps empty."""
        binding = self._bindings.get(destination)
        wire = self._wires.get(destination)
        if (
            binding is None
            or wire is None
            or not binding.game.server_hosted
            or str(binding.game.properties.get("B-U-game_type", "")) != "2"
            or not bool(binding.participant.invite_remote_player_id)
            or wire.session_confirmed
        ):
            return False

        kind = logical_type(logical)
        kind_value = -1 if kind is None else int(kind)
        if kind_value not in wire.preconfirm_deferred_types:
            wire.preconfirm_deferred_types.add(kind_value)
            self.log.info(
                "Carbon GM release invite preconfirm host delivery deferred: "
                "gid=%s dst=%s:%d pid=%d kind=%s next_server=%07x "
                "action=preserve-bootstrap-and-0x10a-until-guest-0x02",
                binding.game.gid,
                destination[0],
                destination[1],
                binding.participant.player_id,
                "none" if kind is None else f"0x{kind_value:02x}",
                wire.next_server_sequence,
            )
        return True
