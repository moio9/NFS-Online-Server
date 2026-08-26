"""Carbon GameManager session/bootstrap wire helpers.

The player body itself lives in :mod:`player_codec`.  This module owns the
capture-shaped HostHello and CommUDP active wrappers used before the race
session-object exchange begins.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct

from carbon.gamemanager.protocol import (
    GMMessageType,
    NGL_FOOTER_FLAG,
    NGL_FOOTER_WITH_TRAILER,
    gm_message_tag,
)


# Capture-shaped HostHello payloads after the semantic 0x0182 tag.  Capacity,
# expected-player count and the no-inline-player flag are understood; the
# remaining middleware flags are preserved but not yet fully named.
_CLIENT_HOSTED_HELLO_PAYLOAD = bytes.fromhex(
    "800480808100000000010180000000000000000000000800010004"
)
_DEDICATED_HELLO_PAYLOAD = bytes.fromhex(
    "800480808100000000000082000000000000000000000800010004"
)


class SessionCodecError(ValueError):
    pass


@dataclass(frozen=True)
class ActiveMessage:
    sequence: int
    acknowledgement: int
    body: bytes

    def encode(self) -> bytes:
        return encode_active(self.sequence, self.acknowledgement, self.body)


def _u32(value: int, label: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 0xFFFFFFFF:
        raise SessionCodecError(f"{label} out of range: {value}")
    return parsed


def encode_active(sequence: int, acknowledgement: int, body: bytes) -> bytes:
    """Wrap one NGL/GameManager body in a CommUDP active payload."""
    sequence = _u32(sequence, "sequence")
    acknowledgement = _u32(acknowledgement, "acknowledgement")
    payload = bytes(body)
    if not payload:
        raise SessionCodecError("active body cannot be empty")
    return struct.pack(">II", sequence, acknowledgement) + payload


def encode_empty_active_ack(
    sequence: int,
    acknowledgement: int,
    *,
    footer: bytes,
) -> bytes:
    """Encode the stock empty active ACK used immediately after ticket bind.

    The final byte has bit 0x40 set, so the preceding twelve bytes are the NGL
    footer rather than a GameManager message.
    """
    raw_footer = bytes(footer)
    if len(raw_footer) != 12:
        raise SessionCodecError(f"NGL footer must be 12 bytes, got {len(raw_footer)}")
    return encode_active(sequence, acknowledgement, raw_footer + NGL_FOOTER_FLAG)


def encode_host_hello(
    expected_players: int,
    *,
    capacity: int = 8,
    footer: bytes,
    server_hosted: bool = False,
) -> bytes:
    """Encode capture-shaped GameManager 0x0182 HostHello.

    Players are not inlined.  Separate 0x0183 records follow in authoritative
    player-id order.  ``footer`` is shared with the immediately preceding empty
    active ACK, matching the official invite/create capture.
    """
    expected = int(expected_players)
    maximum = int(capacity)
    if not 1 <= maximum <= 8:
        raise SessionCodecError(f"capacity must be 1..8, got {capacity}")
    if not 1 <= expected <= maximum:
        raise SessionCodecError(
            f"expected_players must be 1..capacity ({maximum}), got {expected_players}"
        )
    raw_footer = bytes(footer)
    if len(raw_footer) != 12:
        raise SessionCodecError(f"NGL footer must be 12 bytes, got {len(raw_footer)}")

    # create&invitejoin.pcapng frame 440/758.  Byte 27 is the no-inline-player
    # flag and byte 28 is the captured 0x04 terminator before the timing footer.
    payload = _DEDICATED_HELLO_PAYLOAD if server_hosted else _CLIENT_HOSTED_HELLO_PAYLOAD
    body = bytearray(gm_message_tag(GMMessageType.HOST_HELLO) + payload)
    body[23:25] = maximum.to_bytes(2, "big")
    body[25:27] = expected.to_bytes(2, "big")
    body[27] = 0
    return bytes(body) + raw_footer + NGL_FOOTER_WITH_TRAILER
