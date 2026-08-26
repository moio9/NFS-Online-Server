"""Need for Speed Carbon FESL/TCP frame codec.

Carbon uses a four-byte command, a big-endian transaction word and a
big-endian total length.  The payload is usually newline-separated key/value
text terminated by one NUL byte.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import struct
from typing import Mapping


HEADER_SIZE = 12
DEFAULT_MAX_FRAME_SIZE = 65_535
FESL_FRAGMENT_SIZE = 8_096


class FESLFrameError(ValueError):
    """Raised for a structurally invalid Carbon FESL frame."""


def _command_bytes(command: str) -> bytes:
    raw = str(command).encode("latin-1", errors="strict")
    if len(raw) != 4 or not all(0x20 <= byte <= 0x7E for byte in raw):
        raise FESLFrameError(f"invalid four-byte command: {command!r}")
    return raw


def encode_fields(fields: Mapping[str, object]) -> bytes:
    """Encode the capture-compatible `key=value\n...\0` body."""
    lines = [f"{key}={value}" for key, value in fields.items()]
    # Retail Carbon terminates every non-empty field list with LF and then NUL.
    # The LF matters to parsers which only commit the final key/value at the
    # end of a line (not merely at the end of the Aries frame).
    return (("\n".join(lines) + "\n\x00").encode("latin-1") if lines else b"\x00")


def decode_fields(payload: bytes) -> dict[str, str]:
    """Decode FESL or Theater fields while preserving key spelling."""
    text = bytes(payload).decode("latin-1", errors="replace").rstrip("\x00")
    fields: dict[str, str] = {}
    for raw_line in text.replace("\r", "\n").replace("\t", "\n").split("\n"):
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


@dataclass(frozen=True)
class FESLFrame:
    command: str
    transaction: int
    payload: bytes

    @property
    def fields(self) -> dict[str, str]:
        return decode_fields(self.payload)

    def encode(self) -> bytes:
        command = _command_bytes(self.command)
        payload = bytes(self.payload)
        return (
            command
            + struct.pack(">I", int(self.transaction) & 0xFFFFFFFF)
            + struct.pack(">I", HEADER_SIZE + len(payload))
            + payload
        )

    @classmethod
    def from_fields(
        cls,
        command: str,
        fields: Mapping[str, object],
        *,
        transaction: int = 0x80000000,
    ) -> "FESLFrame":
        _command_bytes(command)
        return cls(command, int(transaction) & 0xFFFFFFFF, encode_fields(fields))


def packetize_frame(
    frame: FESLFrame,
    *,
    fragment_size: int = FESL_FRAGMENT_SIZE,
) -> list[FESLFrame]:
    """Wrap a large logical FESL payload in retail-compatible fragments.

    Carbon's FESL transactor does not accept one arbitrarily large Aries
    packet.  Payloads over 8096 bytes are Base64 encoded as one logical body
    and carried by repeated ``decodedSize/size/data`` frames.  Transaction
    code ``0xB`` marks those packets as fragments; the low transaction token
    continues to identify the original request.
    """

    limit = int(fragment_size)
    if limit <= 0:
        raise ValueError("fragment_size must be positive")
    payload = bytes(frame.payload)
    if len(payload) <= limit:
        return [frame]

    # Packet.py in the retail-era emulator packetizes the field body before
    # the normal terminating NUL is added to an Aries packet.
    logical = payload[:-1] if payload.endswith(b"\x00") else payload
    encoded = base64.b64encode(logical).decode("ascii")
    transaction = (int(frame.transaction) & 0x0FFFFFFF) | 0xB0000000
    return [
        FESLFrame.from_fields(
            frame.command,
            {
                "decodedSize": str(len(logical)),
                "size": str(len(encoded)),
                "data": encoded[offset : offset + limit].replace("=", "%3d"),
            },
            transaction=transaction,
        )
        for offset in range(0, len(encoded), limit)
    ]


def decode_one(
    data: bytes | bytearray,
    *,
    max_frame_size: int = DEFAULT_MAX_FRAME_SIZE,
) -> tuple[FESLFrame, int] | None:
    """Decode one complete frame, returning `None` for an incomplete buffer."""
    view = bytes(data)
    if len(view) < HEADER_SIZE:
        return None
    try:
        command = view[:4].decode("latin-1")
        _command_bytes(command)
    except (UnicodeError, FESLFrameError) as exc:
        raise FESLFrameError("invalid frame command") from exc
    transaction, total_length = struct.unpack(">II", view[4:12])
    if total_length < HEADER_SIZE:
        raise FESLFrameError(f"frame length below header: {total_length}")
    if total_length > int(max_frame_size):
        raise FESLFrameError(f"frame exceeds limit: {total_length} > {max_frame_size}")
    if len(view) < total_length:
        return None
    return FESLFrame(command, transaction, view[HEADER_SIZE:total_length]), total_length


class FESLStreamDecoder:
    """Incremental decoder for TCP fragmentation and coalesced frames."""

    def __init__(self, *, max_frame_size: int = DEFAULT_MAX_FRAME_SIZE) -> None:
        self.max_frame_size = int(max_frame_size)
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[FESLFrame]:
        self._buffer.extend(data)
        frames: list[FESLFrame] = []
        while self._buffer:
            decoded = decode_one(self._buffer, max_frame_size=self.max_frame_size)
            if decoded is None:
                break
            frame, consumed = decoded
            del self._buffer[:consumed]
            frames.append(frame)
        return frames

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)
