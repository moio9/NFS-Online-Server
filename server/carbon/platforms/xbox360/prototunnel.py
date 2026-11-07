"""Experimental clear ProtoTunnel wire format observed in Xbox lab tests.

This codec provides neither authentication nor confidentiality. Its callers
must retain the existing loopback restriction and authenticated-ticket checks.
It does not establish how a retail Xbox secures traffic on the network.
"""

from __future__ import annotations

from carbon.transport.prototunnel import (
    DEFAULT_ENCRYPTED_CHANNELS, NATIVE_MAX_UDP_BYTES, NATIVE_MAX_VIRTUAL_PACKETS,
    NATIVE_MAX_VIRTUAL_PAYLOAD, ProtoTunnelError, TunnelDatagram, TunnelPacket,
)

_CLEAR_CHANNELS = frozenset({0, 1, 2, 3, 7})


def decode_clear_datagram(payload: bytes) -> TunnelDatagram:
    """Decode the experimental Xbox clear-mode table with native size limits.

    Clear transport provides no confidentiality or authentication. The caller
    must explicitly constrain where it is accepted and authenticate the ticket.
    Reject ambiguous descriptor tables instead of guessing their boundaries.
    """
    raw = bytes(payload)
    if len(raw) < 4 or len(raw) > NATIVE_MAX_UDP_BYTES:
        raise ProtoTunnelError("invalid clear ProtoTunnel datagram size")
    candidates: list[tuple[TunnelPacket, ...]] = []
    max_headers = min(NATIVE_MAX_VIRTUAL_PACKETS + 1, (len(raw) - 2) // 2)
    for count in range(1, max_headers + 1):
        headers = []
        for i in range(count):
            first, second = raw[2 + 2 * i : 4 + 2 * i]
            length, channel = (first << 4) | (second >> 4), second & 15
            if (
                not 0 < length <= NATIVE_MAX_VIRTUAL_PAYLOAD
                or channel not in _CLEAR_CHANNELS
            ):
                break
            headers.append((channel, length))
        if (
            len(headers) != count
            or 2 + count * 2 + sum(n for _, n in headers) != len(raw)
        ):
            continue
        identities = [n for channel, n in headers if channel == 7]
        if (
            len(identities) > 1
            or (identities and identities[0] != 6)
            or count - len(identities) > NATIVE_MAX_VIRTUAL_PACKETS
        ):
            continue
        position = 2 + count * 2
        packets = []
        for channel, length in headers:
            packets.append(TunnelPacket(channel, raw[position : position + length]))
            position += length
        candidates.append(tuple(packets))
    if len(candidates) != 1:
        raise ProtoTunnelError("invalid or ambiguous clear ProtoTunnel table")
    return TunnelDatagram(int.from_bytes(raw[:2], "big"), candidates[0])


def encode_clear_datagram(datagram: TunnelDatagram) -> bytes:
    """Encode the same channel ordering used by the encrypted transport."""
    packets = tuple(datagram.packets)
    identities = [packet for packet in packets if packet.channel == 7]
    if (
        not packets
        or len(identities) > 1
        or len(packets) - len(identities) > NATIVE_MAX_VIRTUAL_PACKETS
        or any(
            packet.channel not in _CLEAR_CHANNELS
            or not 0 < len(packet.payload) <= NATIVE_MAX_VIRTUAL_PAYLOAD
            for packet in packets
        )
        or any(len(packet.payload) != 6 for packet in identities)
    ):
        raise ProtoTunnelError("invalid clear ProtoTunnel packets")
    ordered = (
        identities
        + [
            p for p in packets
            if p.channel != 7 and p.channel in DEFAULT_ENCRYPTED_CHANNELS
        ]
        + [p for p in packets if p.channel not in DEFAULT_ENCRYPTED_CHANNELS]
    )
    size = 2 + 2 * len(ordered) + sum(len(p.payload) for p in ordered)
    if size > NATIVE_MAX_UDP_BYTES:
        raise ProtoTunnelError("clear ProtoTunnel datagram exceeds native receive buffer")
    headers = bytes(
        value
        for p in ordered
        for value in (len(p.payload) >> 4, ((len(p.payload) & 15) << 4) | p.channel)
    )
    return (
        (int(datagram.offset_words) & 0xFFFF).to_bytes(2, "big")
        + headers
        + b"".join(p.payload for p in ordered)
    )
