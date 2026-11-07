"""Local outbound ProtoTunnel/CommUDP publication for Carbon endpoints.

``EndpointPublisher`` is a server-side abstraction, not a recovered EA class.
It owns only destination-local reliable sequence and RC4 offset advancement;
room policy, footer selection and cross-flow ordering remain in the service.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from collections.abc import Callable
from dataclasses import dataclass
import logging
import time

from carbon.platforms.session_policy import policy_for
from carbon.platforms.xbox360.prototunnel import encode_clear_datagram
from carbon.gamemanager.protocol import OLMessageType
from carbon.gamemanager.race_session import logical_type
from carbon.gamemanager.session_codec import encode_active
from carbon.rebroadcaster.confirmations import ConfirmationManager
from carbon.rebroadcaster.ack_barrier import AckBarrier
from carbon.rebroadcaster.state import Address, DeferredPeerPublication, EndpointWireState
from carbon.theater.directory import CarbonTicketResolution
from carbon.transport.commudp import (
    CommUDPActive,
    game_manager_body,
    parse_channel_one,
)
from carbon.transport.prototunnel import (
    NATIVE_MAX_UDP_BYTES,
    NATIVE_MAX_VIRTUAL_PACKETS,
    ProtoTunnelError,
    TunnelDatagram,
    TunnelPacket,
    cipher_region_size,
)

_SEQUENCE_MASK = 0x0FFFFFFF
_MAX_DEFERRED_PEER_PUBLICATIONS = 64
_MAX_DEFERRED_PEER_BYTES = 65536


def _same_membership(first, second) -> bool:
    """Recognize a refreshed directory record without accepting a rejoin."""
    return second is not None and (
        first.identity.user_id, first.player_id, first.entered_at, first.descriptor_handle
    ) == (
        second.identity.user_id, second.player_id, second.entered_at, second.descriptor_handle
    )


class ReplyBuffer(list[tuple[bytes, Address]]):
    """Preserve built records for coordinators while hiding unsent records."""

    def __init__(self, items=()) -> None:
        super().__init__(items)
        self._held: set[int] = set()

    def append_held(self, item: tuple[bytes, Address]) -> None:
        self._held.add(len(self))
        self.append(item)

    def emitted(self) -> list[tuple[bytes, Address]]:
        return [item for index, item in enumerate(self) if index not in self._held]

    def discard_destinations(self, destinations: set[Address]) -> None:
        self._held.update(
            index for index, (_payload, target) in enumerate(self)
            if target in destinations
        )


@dataclass(frozen=True)
class _DeferredDatagram:
    payload: bytes
    sequences: tuple[int, ...]
    confirmation: str | None
    application_confirmation: bool


class EndpointPublisher:
    """Encode ordered endpoint replies and advance their transport state."""

    def __init__(
        self,
        key: bytes,
        wires: MutableMapping[Address, EndpointWireState],
        bindings: Mapping[Address, CarbonTicketResolution],
        *,
        confirmations: ConfirmationManager | None = None,
        is_host: Callable[[CarbonTicketResolution], bool] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.key = bytes(key)
        if not self.key:
            raise ValueError("Carbon outbound EKEY cannot be empty")
        self._wires = wires
        self._bindings = bindings
        self._is_host = is_host or (
            lambda binding: binding.participant.identity.user_id == binding.game.host.user_id
        )
        self.confirmations = confirmations
        self.join_ack_barrier: AckBarrier[_DeferredDatagram] = AckBarrier()
        self.log = logger or logging.getLogger(__name__)

    def defer_uninitialized_peer(
        self, source: Address, destination: Address, body: bytes, *,
        confirmation: str | None = None,
        on_sent: Callable[[int], None] | None = None,
    ) -> bool:
        """Queue peer room messages before allocating any transport sequence.

        This applies to Xbox Quick Match or client-hosted joiners; existing
        invite, dedicated Challenge and PC policy remains unchanged.
        """
        binding = self._bindings.get(destination)
        source_binding = self._bindings.get(source)
        source_wire = self._wires.get(source)
        wire = self._wires.get(destination)
        if (
            source == destination or binding is None or source_binding is None
            or source_wire is None or wire is None or binding.game.gid != source_binding.game.gid
            or (binding.game.server_hosted and str(binding.game.properties.get("B-U-game_type", "")) != "1")
            or bool(binding.participant.invite_remote_player_id)
            or not policy_for(binding.participant.identity.client_platform).defer_peer_publications
            or self._is_host(binding)
            or wire.session_confirmed or bool(wire.session_blocks)
        ):
            return False
        if wire.deferred_peer_overflow:
            return True
        payload = bytes(body)
        if (
            len(wire.deferred_peer_publications) >= _MAX_DEFERRED_PEER_PUBLICATIONS
            or wire.deferred_peer_bytes + len(payload) > _MAX_DEFERRED_PEER_BYTES
        ):
            wire.deferred_peer_overflow = True
            self.log.warning(
                "Carbon GM deferred peer publications overflow: dst=%s:%d "
                "items=%d bytes=%d action=expire-endpoint",
                destination[0], destination[1], len(wire.deferred_peer_publications),
                wire.deferred_peer_bytes,
            )
            return True
        wire.deferred_peer_publications.append(DeferredPeerPublication(
            source, source_binding, source_wire, payload, confirmation, on_sent,
        ))
        wire.deferred_peer_bytes += len(payload)
        if len(wire.deferred_peer_publications) == 1:
            self.log.info("Carbon GM deferred peer publications opened: dst=%s:%d", *destination)
        return True

    def release_deferred_peer(
        self, replies: list[tuple[bytes, Address]], destination: Address,
    ) -> int:
        """Release held messages in order after the joiner publishes a fragment."""
        wire = self._wires.get(destination)
        binding = self._bindings.get(destination)
        if (
            wire is None or binding is None or wire.deferred_peer_overflow
            or not (wire.session_blocks or wire.session_confirmed)
            or self.join_ack_barrier.pending(destination)
        ):
            return 0
        released = 0
        while wire.deferred_peer_publications:
            item = wire.deferred_peer_publications[0]
            # Ticket and directory refreshes may create new wrappers for the
            # same membership. A new wire, room or membership is a different
            # endpoint lifetime and must not replay old messages.
            current_source = self._bindings.get(item.source)
            original_participant = item.source_binding.participant
            if (
                current_source is None
                or current_source.game is not item.source_binding.game
                or not _same_membership(original_participant, current_source.participant)
                or not _same_membership(
                    original_participant,
                    current_source.game.participants.get(original_participant.identity.user_id),
                )
                or self._wires.get(item.source) is not item.source_wire
            ):
                wire.deferred_peer_publications.pop(0)
                wire.deferred_peer_bytes -= len(item.body)
                continue
            sequence = self.append_active_body(
                replies, destination, item.body, confirmation=item.confirmation,
            )
            wire.deferred_peer_publications.pop(0)
            wire.deferred_peer_bytes -= len(item.body)
            if item.on_sent is not None:
                item.on_sent(sequence)
            released += 1
        if released:
            self.log.info("Carbon GM deferred peer publications released: dst=%s:%d count=%d",
                          destination[0], destination[1], released)
        return released

    def release_join_ack(
        self, replies: list[tuple[bytes, Address]], destination: Address,
        acknowledgement: int, *, now: float | None = None,
    ) -> bool:
        """Publish held datagrams and start their timers only after the ACK."""
        ready = self.join_ack_barrier.acknowledge(destination, acknowledgement)
        if ready is None:
            return False
        current = time.monotonic() if now is None else float(now)
        wire = self._wires.get(destination)
        if wire is None:
            return False
        if wire.session_bootstrap_window is not None:
            window = wire.session_bootstrap_window
            window.retry = window.retry.policy.begin(current)
        for item in ready:
            replies.append((item.payload, destination))
            if item.confirmation and self.confirmations and item.sequences:
                self.confirmations.register(
                    destination, (item.payload,), base_sequence=item.sequences[0],
                    final_sequence=item.sequences[-1], label=item.confirmation,
                    application_confirmation=item.application_confirmation, now=current,
                )
        return True

    def append_transport_ack(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
    ) -> None:
        """Advertise, but do not consume, the next reliable sequence."""
        wire = self._wires[destination]
        active = (
            (int(wire.next_server_sequence) & _SEQUENCE_MASK).to_bytes(4, "big")
            + (int(wire.last_client_sequence) & _SEQUENCE_MASK).to_bytes(4, "big")
        )
        self.append_datagram(
            replies,
            TunnelDatagram(
                wire.next_offset_words,
                (TunnelPacket(1, active),),
            ),
            destination,
        )

    def append_active_bodies(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
        bodies: Sequence[bytes],
        *,
        confirmation: str | None = None,
    ) -> None:
        """Publish already-decorated logical bodies in one datagram."""
        wire = self._wires[destination]
        packets = tuple(
            TunnelPacket(
                1,
                encode_active(
                    self.take_server_sequence(wire),
                    int(wire.last_client_sequence) & _SEQUENCE_MASK,
                    bytes(body),
                ),
            )
            for body in bodies
        )
        if packets:
            self.append_datagram(
                replies,
                TunnelDatagram(wire.next_offset_words, packets),
                destination,
                confirmation=confirmation,
            )

    @staticmethod
    def commudp_aggregate_payload(
        records_newest_to_oldest: Sequence[bytes],
    ) -> bytes:
        """Encode ``FUN_009891f0`` reliable history newest-first."""
        records = tuple(
            bytes(record) + b"\x04"
            for record in records_newest_to_oldest
        )
        if not records:
            raise ValueError("CommUDP aggregate requires at least one record")
        if len(records) > 16:
            raise ValueError("CommUDP aggregate exceeds four-bit history count")

        payload = bytearray(records[0])
        for record in records[1:]:
            if len(record) > 0xFF:
                raise ValueError(
                    "CommUDP historical record exceeds one-byte length: "
                    f"{len(record)}"
                )
            payload.extend(record)
            payload.append(len(record))
        return bytes(payload)

    def append_active_record_batch(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
        records_oldest_to_newest: Sequence[bytes],
        *,
        confirmation: str | None = None,
    ) -> int:
        """Flush several new records as one native CommUDP packet."""
        records = tuple(bytes(item) for item in records_oldest_to_newest)
        if not records:
            raise ValueError("CommUDP record batch cannot be empty")
        if len(records) > 16:
            raise ValueError("CommUDP record batch exceeds four-bit count")

        wire = self._wires[destination]
        base = int(wire.next_server_sequence) & _SEQUENCE_MASK
        latest = (base + len(records) - 1) & _SEQUENCE_MASK
        sequence = ((len(records) - 1) << 28) | latest
        active = encode_active(
            sequence,
            int(wire.last_client_sequence) & _SEQUENCE_MASK,
            self.commudp_aggregate_payload(tuple(reversed(records))),
        )
        wire.next_server_sequence = (base + len(records)) & _SEQUENCE_MASK
        self.append_datagram(
            replies,
            TunnelDatagram(
                wire.next_offset_words,
                (TunnelPacket(1, active),),
            ),
            destination,
            confirmation=confirmation,
        )
        return latest

    def append_active_body(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
        body: bytes,
        *,
        confirmation: str | None = None,
        application_confirmation: bool = False,
    ) -> int:
        wire = self._wires[destination]
        sequence = self.take_server_sequence(wire)
        active = encode_active(
            sequence,
            int(wire.last_client_sequence) & _SEQUENCE_MASK,
            bytes(body),
        )
        self.append_datagram(
            replies,
            TunnelDatagram(
                wire.next_offset_words,
                (TunnelPacket(1, active),),
            ),
            destination,
            confirmation=confirmation,
            application_confirmation=application_confirmation,
        )
        return sequence

    def append_datagram(
        self,
        replies: list[tuple[bytes, Address]],
        datagram: TunnelDatagram,
        destination: Address,
        *,
        confirmation: str | None = None,
        application_confirmation: bool = False,
    ) -> None:
        wire = self._wires.setdefault(destination, EndpointWireState())
        binding = self._bindings.get(destination)
        if (
            binding is not None
            and bool(binding.participant.invite_remote_player_id)
            and not wire.session_confirmed
        ):
            for index, packet in enumerate(datagram.packets):
                parsed = parse_channel_one(packet)
                if not isinstance(parsed, CommUDPActive):
                    continue
                logical = game_manager_body(parsed.payload)
                kind = logical_type(logical)
                kind_name = (
                    kind.name
                    if isinstance(kind, OLMessageType)
                    else (
                        f"0x{int(kind):02x}"
                        if kind is not None
                        else "none"
                    )
                )
                self.log.info(
                    "Carbon GM release invite preconfirm outbound diagnostic: "
                    "gid=%s dst=%s:%d index=%d seq=%08x low=%07x flags=%x "
                    "ack=%08x kind=%s logical_len=%d logical=%s",
                    binding.game.gid,
                    destination[0],
                    destination[1],
                    index,
                    int(parsed.sequence),
                    int(parsed.sequence) & _SEQUENCE_MASK,
                    (int(parsed.sequence) >> 28) & 0xF,
                    int(parsed.acknowledgement),
                    kind_name,
                    len(logical),
                    logical.hex(),
                )
        encoded = (
            encode_clear_datagram(datagram)
            if wire.clear_tunnel else datagram.encode(wire.tunnel_key or self.key)
        )
        pending = self.join_ack_barrier.pending(destination)
        sequences = (
            tuple(
                int(parsed.sequence) & _SEQUENCE_MASK
                for packet in datagram.packets
                if isinstance((parsed := parse_channel_one(packet)), CommUDPActive)
            )
            if pending or confirmation is not None else ()
        )
        # Every newly built datagram follows the barrier, even an unsequenced
        # control/ACK or virtual gameplay sequence. Exact older retransmissions
        # come from ConfirmationManager directly and remain sendable.
        held = pending and self.join_ack_barrier.defer(
            destination, sequences, encoded,
            _DeferredDatagram(encoded, sequences, confirmation, application_confirmation),
            newly_built=True,
        )
        if held:
            if not isinstance(replies, ReplyBuffer):
                raise RuntimeError("join ACK barrier requires a ReplyBuffer")
            replies.append_held((encoded, destination))
        else:
            replies.append((encoded, destination))
        if not held and confirmation is not None and self.confirmations is not None:
            if sequences:
                self.confirmations.register(
                    destination,
                    (encoded,),
                    base_sequence=sequences[0],
                    final_sequence=sequences[-1],
                    label=confirmation,
                    application_confirmation=application_confirmation,
                )
        encrypted_length = cipher_region_size(datagram.packets)
        step = max(1, (encrypted_length + 3) // 4)
        # ProtoTunnel exposes only the low 16 bits in its UDP header. Its RC4
        # keystream remains continuous across that wrap.
        wire.next_offset_words = int(datagram.offset_words) + step

    def append_packet_batches(
        self,
        replies: list[tuple[bytes, Address]],
        packets: Sequence[TunnelPacket],
        destination: Address,
        *,
        confirmation: str | None = None,
    ) -> int:
        """Append ordered packets in native-sized ProtoTunnel datagrams."""
        batches: list[tuple[TunnelPacket, ...]] = []
        current: list[TunnelPacket] = []

        def fits_native(candidate: Sequence[TunnelPacket]) -> bool:
            identity_count = sum(
                int(packet.channel) == 7
                for packet in candidate
            )
            virtual_count = len(candidate) - identity_count
            wire_size = (
                2
                + 2 * len(candidate)
                + sum(len(packet.payload) for packet in candidate)
            )
            return (
                identity_count <= 1
                and virtual_count <= NATIVE_MAX_VIRTUAL_PACKETS
                and wire_size <= NATIVE_MAX_UDP_BYTES
            )

        for packet in packets:
            candidate = [*current, packet]
            if current and not fits_native(candidate):
                batches.append(tuple(current))
                current = [packet]
            else:
                current = candidate
            if not fits_native(current):
                raise ProtoTunnelError(
                    "single ProtoTunnel packet exceeds native datagram limits"
                )
        if current:
            batches.append(tuple(current))

        for batch in batches:
            wire = self._wires.setdefault(
                destination,
                EndpointWireState(),
            )
            self.append_datagram(
                replies,
                TunnelDatagram(wire.next_offset_words, batch),
                destination,
                confirmation=confirmation,
            )
        return len(batches)

    @staticmethod
    def take_server_sequence(wire: EndpointWireState) -> int:
        sequence = int(wire.next_server_sequence) & _SEQUENCE_MASK
        wire.next_server_sequence = (sequence + 1) & _SEQUENCE_MASK
        return sequence
