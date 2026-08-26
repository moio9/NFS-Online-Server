"""Local outbound ProtoTunnel/CommUDP publication for Carbon endpoints.

``EndpointPublisher`` is a server-side abstraction, not a recovered EA class.
It owns only destination-local reliable sequence and RC4 offset advancement;
room policy, footer selection and cross-flow ordering remain in the service.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
import logging

from carbon.gamemanager.protocol import OLMessageType
from carbon.gamemanager.race_session import logical_type
from carbon.gamemanager.session_codec import encode_active
from carbon.rebroadcaster.confirmations import ConfirmationManager
from carbon.rebroadcaster.state import Address, EndpointWireState
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


class EndpointPublisher:
    """Encode ordered endpoint replies and advance their transport state."""

    def __init__(
        self,
        key: bytes,
        wires: MutableMapping[Address, EndpointWireState],
        bindings: Mapping[Address, CarbonTicketResolution],
        *,
        confirmations: ConfirmationManager | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.key = bytes(key)
        if not self.key:
            raise ValueError("Carbon outbound EKEY cannot be empty")
        self._wires = wires
        self._bindings = bindings
        self.confirmations = confirmations
        self.log = logger or logging.getLogger(__name__)

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
        encoded = datagram.encode(wire.tunnel_key or self.key)
        replies.append((encoded, destination))
        if confirmation is not None and self.confirmations is not None:
            sequences = tuple(
                int(parsed.sequence) & _SEQUENCE_MASK
                for packet in datagram.packets
                if isinstance(
                    (parsed := parse_channel_one(packet)),
                    CommUDPActive,
                )
            )
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
