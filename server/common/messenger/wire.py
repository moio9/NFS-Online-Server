"""Shared EA Messenger frame encoding and incremental decoding."""
from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Callable

HEADER_SIZE = 12
WireSender = Callable[[bytes], bool]


def decode_messenger_fields(payload: bytes) -> dict[str, str]:
    text = bytes(payload).decode("latin-1", errors="replace").rstrip("\x00")
    result: dict[str, str] = {}
    for raw_line in text.replace("\r", "\n").replace("\t", "\n").split("\n"):
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        name = key.strip().upper()
        if name:
            result[name] = value.strip()
    return result


@dataclass(frozen=True)
class EAMessengerFrame:
    command: str
    word: int
    payload: bytes

    @property
    def transaction(self) -> int:
        """Carbon name for the shared four-byte header word."""
        return int(self.word) & 0xFFFFFFFF

    @property
    def fields(self) -> dict[str, str]:
        return decode_messenger_fields(self.payload)

    @classmethod
    def from_fields(
        cls,
        command: str,
        fields: dict[str, object],
        *,
        transaction: int = 0x80000000,
        trailing_newline: bool = False,
    ) -> "EAMessengerFrame":
        lines = [f"{key}={value}" for key, value in fields.items()]
        terminator = "\n\x00" if trailing_newline else "\x00"
        payload = (("\n".join(lines) + terminator).encode("latin-1") if lines else b"\x00")
        return cls(str(command), int(transaction) & 0xFFFFFFFF, payload)

    def encode(self) -> bytes:
        raw_command = self.command.encode("latin-1", errors="strict")
        if len(raw_command) != 4 or any(value < 0x20 or value > 0x7E for value in raw_command):
            raise ValueError(f"invalid EA Messenger command: {self.command!r}")
        payload = bytes(self.payload)
        return (
            raw_command
            + struct.pack(">I", int(self.word) & 0xFFFFFFFF)
            + struct.pack(">I", HEADER_SIZE + len(payload))
            + payload
        )


class EAMessengerStreamDecoder:
    def __init__(self, *, max_frame_size: int = 65_535) -> None:
        if int(max_frame_size) < HEADER_SIZE:
            raise ValueError("max_frame_size must be at least 12")
        self.max_frame_size = int(max_frame_size)
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[EAMessengerFrame]:
        self._buffer.extend(data)
        frames: list[EAMessengerFrame] = []
        while len(self._buffer) >= HEADER_SIZE:
            header = bytes(self._buffer[:HEADER_SIZE])
            command_raw = header[:4]
            if any(value < 0x20 or value > 0x7E for value in command_raw):
                raise ValueError(f"invalid EA Messenger command bytes: {command_raw.hex()}")
            word, total_length = struct.unpack(">II", header[4:12])
            if total_length < HEADER_SIZE or total_length > self.max_frame_size:
                raise ValueError(f"invalid EA Messenger frame length: {total_length}")
            if len(self._buffer) < total_length:
                break
            wire = bytes(self._buffer[:total_length])
            del self._buffer[:total_length]
            frames.append(
                EAMessengerFrame(
                    command_raw.decode("latin-1"),
                    word,
                    wire[HEADER_SIZE:],
                )
            )
        return frames
