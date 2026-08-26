"""12-byte classic EA Nation frame codec used by NFS U2 and MW."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import md5
from collections.abc import Mapping, Sequence
import struct


_HEADER_SIZE = 12


def _valid_command(command: str) -> str:
    text = str(command or "")
    raw = text.encode("latin-1", errors="strict")
    if len(raw) != 4 or any(value < 32 or value > 126 for value in raw):
        raise ValueError(f"classic EA command must be four printable bytes: {command!r}")
    return text


@dataclass(frozen=True)
class ClassicEAFrame:
    command: str
    payload: bytes = b""
    reserved: int = 0

    def __post_init__(self) -> None:
        _valid_command(self.command)
        if not 0 <= int(self.reserved) <= 0xFFFFFFFF:
            raise ValueError("reserved value must fit in an unsigned 32-bit integer")

    @property
    def total_length(self) -> int:
        return _HEADER_SIZE + len(self.payload)

    def encode(self) -> bytes:
        return (
            self.command.encode("latin-1")
            + struct.pack(">I", int(self.reserved) & 0xFFFFFFFF)
            + struct.pack(">I", self.total_length)
            + bytes(self.payload)
        )

    @classmethod
    def decode_one(cls, data: bytes) -> tuple["ClassicEAFrame", bytes]:
        raw = bytes(data)
        if len(raw) < _HEADER_SIZE:
            raise ValueError("incomplete classic EA frame header")
        command_raw = raw[:4]
        if any(value < 32 or value > 126 for value in command_raw):
            raise ValueError("invalid classic EA command bytes")
        reserved, total_length = struct.unpack(">II", raw[4:12])
        if total_length < _HEADER_SIZE:
            raise ValueError(f"invalid classic EA frame length: {total_length}")
        if total_length > len(raw):
            raise ValueError("incomplete classic EA frame payload")
        frame = cls(
            command_raw.decode("latin-1"),
            raw[_HEADER_SIZE:total_length],
            reserved,
        )
        return frame, raw[total_length:]

    @classmethod
    def from_fields(
        cls,
        command: str,
        fields: Mapping[str, object] | Sequence[tuple[str, object]],
        *,
        reserved: int = 0,
        separator: str = "\n",
        final_separator: bool = True,
    ) -> "ClassicEAFrame":
        items = fields.items() if isinstance(fields, Mapping) else fields
        lines = [f"{key}={value}" for key, value in items]
        text = separator.join(lines)
        if final_separator and lines:
            text += separator
        return cls(command, text.encode("utf-8") + b"\x00", reserved)

    @classmethod
    def signed(
        cls,
        command: str,
        payload_prefix: bytes,
        total_payload_length: int,
        *,
        reserved: int = 0,
    ) -> "ClassicEAFrame":
        """Build the U2/MW padded frame with an eight-byte MD5 trailer."""
        if int(total_payload_length) < 8:
            raise ValueError("signed payload length must reserve eight signature bytes")
        body_capacity = int(total_payload_length) - 8
        prefix = bytes(payload_prefix)
        if len(prefix) > body_capacity:
            raise ValueError(
                f"signed payload prefix too large: {len(prefix)} > {body_capacity}"
            )
        body = prefix + (b"\x00" * (body_capacity - len(prefix))) + (b"\x00" * 8)
        unsigned = cls(command, body, reserved).encode()
        signature = md5(unsigned[:-8]).digest()[:8]
        return cls(command, body[:-8] + signature, reserved)

    @classmethod
    def short(cls, tag: str) -> bytes:
        raw = str(tag or "").encode("latin-1", errors="strict")
        if len(raw) != 8 or any(value < 32 or value > 126 for value in raw):
            raise ValueError("classic EA short tag must be eight printable bytes")
        return raw + struct.pack(">I", _HEADER_SIZE)

    def fields(self) -> dict[str, str]:
        """Parse key/value text before the first NUL or binary trailer."""
        body = self.payload.split(b"\x00", 1)[0]
        text = body.decode("latin-1", errors="replace").replace("\r", "\n").replace("\t", "\n")
        result: dict[str, str] = {}
        for line in text.split("\n"):
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            name = key.strip().upper()
            if name:
                result[name] = value.strip()
        return result
