"""Incremental decoder for classic EA Nation TCP frames."""

from __future__ import annotations

from dataclasses import dataclass
import struct

from .frame import ClassicEAFrame


_HEADER_SIZE = 12


class ClassicEAFrameError(ValueError):
    """Raised when a classic TCP stream contains an invalid frame."""


@dataclass(frozen=True)
class ClassicEAShortFrame:
    """Eight-byte status tag followed by the normal 12-byte total length."""

    tag: str

    def encode(self) -> bytes:
        return ClassicEAFrame.short(self.tag)


ClassicEAPacket = ClassicEAFrame | ClassicEAShortFrame


class ClassicEAStreamDecoder:
    """Decode fragmented/coalesced U2/MW frames without protocol guessing."""

    def __init__(self, *, max_frame_size: int = 65_535) -> None:
        if int(max_frame_size) < _HEADER_SIZE:
            raise ValueError("max_frame_size must be at least 12")
        self.max_frame_size = int(max_frame_size)
        self._buffer = bytearray()

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)

    @staticmethod
    def _printable(raw: bytes) -> bool:
        return bool(raw) and all(32 <= value <= 126 for value in raw)

    def feed(self, data: bytes) -> tuple[ClassicEAPacket, ...]:
        if data:
            self._buffer.extend(data)
        packets: list[ClassicEAPacket] = []
        while True:
            if len(self._buffer) < _HEADER_SIZE:
                break

            total_length = struct.unpack(">I", self._buffer[8:12])[0]
            if total_length < _HEADER_SIZE:
                raise ClassicEAFrameError(
                    f"invalid classic EA frame length: {total_length}"
                )
            if total_length > self.max_frame_size:
                raise ClassicEAFrameError(
                    f"classic EA frame exceeds limit: {total_length} > {self.max_frame_size}"
                )
            if len(self._buffer) < total_length:
                break

            wire = bytes(self._buffer[:total_length])
            del self._buffer[:total_length]

            if total_length == _HEADER_SIZE and self._printable(wire[:8]):
                packets.append(ClassicEAShortFrame(wire[:8].decode("latin-1")))
                continue

            if not self._printable(wire[:4]):
                raise ClassicEAFrameError(
                    f"invalid classic EA command bytes: {wire[:4].hex()}"
                )
            try:
                frame, trailing = ClassicEAFrame.decode_one(wire)
            except ValueError as exc:
                raise ClassicEAFrameError(str(exc)) from exc
            if trailing:
                raise ClassicEAFrameError("decoder consumed a partial classic EA frame")
            packets.append(frame)
        return tuple(packets)
