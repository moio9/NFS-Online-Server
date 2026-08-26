"""Small, state-free FESL binary primitives shared by protocol codecs."""

from __future__ import annotations

def sint8(value: int) -> bytes:
    """Encode the FESL signed-byte representation used by Carbon."""
    return bytes([((int(value) & 0xFF) + 0x80) & 0xFF])


def sint16(value: int) -> bytes:
    """Encode a FESL signed 16-bit integer, big-endian."""
    return (((int(value) & 0xFFFF) + 0x8000) & 0xFFFF).to_bytes(2, "big")


def sint32(value: int) -> bytes:
    """Encode a FESL signed 32-bit integer, big-endian."""
    return (((int(value) & 0xFFFFFFFF) + 0x80000000) & 0xFFFFFFFF).to_bytes(4, "big")


def sint64(value: int) -> bytes:
    """Encode a FESL signed 64-bit integer, big-endian."""
    return (
        ((int(value) & 0xFFFFFFFFFFFFFFFF) + 0x8000000000000000)
        & 0xFFFFFFFFFFFFFFFF
    ).to_bytes(8, "big")


def fesl_string(value: str, max_len: int = 63) -> bytes:
    """Encode an ASCII FESL string with its signed 32-bit length prefix."""
    raw = str(value or "").encode("ascii", errors="ignore")[:max_len]
    return sint32(len(raw)) + raw
