"""Capture-compatible Carbon ProtoTunnel datagram codec.

Carbon encrypts the descriptor table and only payloads belonging to channels
configured as encrypted.  The lobby configuration observed in the native
rebroadcaster uses channels 1, 2 and 7 as encrypted while channel 0 remains
clear.  RC4 stream accounting includes encrypted descriptors and encrypted
payload bytes, rounded up to a four-byte word boundary; clear payload bytes do
not consume keystream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AbstractSet

from carbon.transport.rc4 import rc4_xor


class ProtoTunnelError(ValueError):
    pass


DEFAULT_ENCRYPTED_CHANNELS = frozenset({1, 2, 7})
NATIVE_MAX_VIRTUAL_PACKETS = 8
NATIVE_MAX_UDP_BYTES = 1000
NATIVE_MAX_VIRTUAL_PAYLOAD = 0x3E3


@dataclass(frozen=True)
class TunnelPacket:
    channel: int
    payload: bytes


@dataclass(frozen=True)
class TunnelDatagram:
    offset_words: int
    packets: tuple[TunnelPacket, ...]

    def encode(self, key: bytes) -> bytes:
        # The header contains only the low 16 bits, while a race can keep the
        # RC4 stream running beyond that first wrap.
        return encode_datagram(
            self.packets,
            key,
            offset_words=self.offset_words & 0xFFFF,
            stream_offset_words=self.offset_words,
        )


def cipher_region_size(
    packets: tuple[TunnelPacket, ...] | list[TunnelPacket],
    *,
    encrypted_channels: AbstractSet[int] = DEFAULT_ENCRYPTED_CHANNELS,
) -> int:
    """Return the bytes that advance Carbon's ProtoTunnel RC4 stream."""

    return (
        2 * len(packets)
        + sum(
            len(packet.payload)
            for packet in packets
            if int(packet.channel) in encrypted_channels
        )
    )


def _decode_candidate(
    payload: bytes,
    key: bytes,
    *,
    cipher_offset_bytes: int,
    header_count: int,
    encrypted_channels: AbstractSet[int],
) -> tuple[TunnelPacket, ...] | None:
    headers_size = header_count * 2
    if 2 + headers_size >= len(payload):
        return None

    encrypted_headers = payload[2 : 2 + headers_size]
    clear_headers = rc4_xor(encrypted_headers, key, skip=cipher_offset_bytes)

    headers: list[tuple[int, int]] = []
    payload_size = 0
    for index in range(header_count):
        first = clear_headers[index * 2]
        second = clear_headers[index * 2 + 1]
        length = (first << 4) | (second >> 4)
        channel = second & 0x0F
        if length <= 0:
            return None
        headers.append((channel, length))
        payload_size += length

    bodies_start = 2 + headers_size
    if bodies_start + payload_size != len(payload):
        return None

    cursor = bodies_start
    rc4_consumed = headers_size
    packets: list[TunnelPacket] = []
    for channel, length in headers:
        raw_body = payload[cursor : cursor + length]
        cursor += length
        if channel in encrypted_channels:
            body = rc4_xor(
                raw_body,
                key,
                skip=cipher_offset_bytes + rc4_consumed,
            )
            rc4_consumed += length
        else:
            body = bytes(raw_body)
        packets.append(TunnelPacket(channel, body))
    return tuple(packets)


def decode_datagram(
    payload: bytes,
    key: bytes,
    *,
    stream_offset_words: int | None = None,
    encrypted_channels: AbstractSet[int] = DEFAULT_ENCRYPTED_CHANNELS,
) -> TunnelDatagram:
    """Decode one ProtoTunnel datagram.

    The packet header carries only the low 16 bits of the RC4 stream position.
    Long-running Carbon races therefore wrap that field while keeping the
    cipher stream running. Callers retaining the per-endpoint stream position
    can provide its reconstructed unbounded value.
    """
    if len(payload) < 4:
        raise ProtoTunnelError("truncated ProtoTunnel datagram")
    offset_words = int.from_bytes(payload[:2], "big")
    cipher_offset_words = offset_words if stream_offset_words is None else int(stream_offset_words)
    if cipher_offset_words < 0:
        raise ProtoTunnelError("negative ProtoTunnel stream offset")

    if len(payload) > NATIVE_MAX_UDP_BYTES:
        raise ProtoTunnelError("ProtoTunnel datagram exceeds native receive buffer")

    # The native virtual queue holds eight packets.  A pending internal
    # channel-7 identity may be prepended as a ninth descriptor.
    max_headers = min(NATIVE_MAX_VIRTUAL_PACKETS + 1, (len(payload) - 2) // 2)
    cipher_offset_bytes = cipher_offset_words * 4
    for header_count in range(1, max_headers + 1):
        packets = _decode_candidate(
            payload,
            key,
            cipher_offset_bytes=cipher_offset_bytes,
            header_count=header_count,
            encrypted_channels=encrypted_channels,
        )
        if packets:
            return TunnelDatagram(offset_words, packets)
    raise ProtoTunnelError("invalid ProtoTunnel subpacket table")


def encode_datagram(
    packets: tuple[TunnelPacket, ...] | list[TunnelPacket],
    key: bytes,
    *,
    offset_words: int = 0,
    stream_offset_words: int | None = None,
    encrypted_channels: AbstractSet[int] = DEFAULT_ENCRYPTED_CHANNELS,
) -> bytes:
    if not 0 <= int(offset_words) <= 0xFFFF:
        raise ProtoTunnelError("offset_words out of range")
    cipher_offset_words = int(offset_words) if stream_offset_words is None else int(stream_offset_words)
    if cipher_offset_words < 0:
        raise ProtoTunnelError("negative ProtoTunnel stream offset")
    if not packets:
        raise ProtoTunnelError("cannot encode an empty ProtoTunnel datagram")

    normalized: list[TunnelPacket] = []
    for packet in packets:
        channel = int(packet.channel)
        body = bytes(packet.payload)
        if not 0 <= channel <= 0x0F:
            raise ProtoTunnelError(f"channel out of range: {channel}")
        if not 0 < len(body) <= NATIVE_MAX_VIRTUAL_PAYLOAD:
            raise ProtoTunnelError(f"subpacket length out of range: {len(body)}")
        if channel == 7 and len(body) != 6:
            raise ProtoTunnelError("native channel-7 identity must be six bytes")
        normalized.append(TunnelPacket(channel, body))

    identity_count = sum(packet.channel == 7 for packet in normalized)
    if identity_count > 1:
        raise ProtoTunnelError("multiple channel-7 identities in one datagram")
    if len(normalized) - identity_count > NATIVE_MAX_VIRTUAL_PACKETS:
        raise ProtoTunnelError("native ProtoTunnel queue exceeds eight virtual packets")

    # FUN_0047AF50 performs a stable encrypted/clear partition, while
    # FUN_0047B010 prepends the pending internal channel-7 identity.
    ordered = (
        [packet for packet in normalized if packet.channel == 7]
        + [
            packet
            for packet in normalized
            if packet.channel != 7 and packet.channel in encrypted_channels
        ]
        + [
            packet
            for packet in normalized
            if packet.channel not in encrypted_channels
        ]
    )
    wire_size = 2 + (2 * len(ordered)) + sum(len(packet.payload) for packet in ordered)
    if wire_size > NATIVE_MAX_UDP_BYTES:
        raise ProtoTunnelError("ProtoTunnel datagram exceeds native 1000-byte buffer")

    headers = bytearray()
    for packet in ordered:
        body = packet.payload
        headers.append(len(body) >> 4)
        headers.append(((len(body) & 0x0F) << 4) | packet.channel)

    prefix = int(offset_words).to_bytes(2, "big")
    cipher_offset_bytes = cipher_offset_words * 4
    encrypted_headers = rc4_xor(bytes(headers), key, skip=cipher_offset_bytes)
    rc4_consumed = len(headers)
    bodies = bytearray()
    for packet in ordered:
        if packet.channel in encrypted_channels:
            bodies.extend(
                rc4_xor(
                    packet.payload,
                    key,
                    skip=cipher_offset_bytes + rc4_consumed,
                )
            )
            rc4_consumed += len(packet.payload)
        else:
            bodies.extend(packet.payload)
    return prefix + encrypted_headers + bytes(bodies)
