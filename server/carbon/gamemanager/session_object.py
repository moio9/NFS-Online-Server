"""Carbon non-GM 0x1e race-session object helpers.

A local car/session object is uploaded in three logical chunks at offsets
0x000, 0x1e4 and 0x3c8. Object ids are receiver-local, so rebroadcasting must
rewrite the id and the room-player slot while preserving the real car payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

from carbon.gamemanager.protocol import OLMessageType, logical_message


SESSION_OBJECT_PREFIX = logical_message(OLMessageType.BIG_MESSAGE)
SESSION_OBJECT_CHUNK_OFFSETS = (0, 0x1E4, 0x3C8)
# Compatibility alias for pre-V693 callers.
CAPTURE_OFFSETS = SESSION_OBJECT_CHUNK_OFFSETS


class SessionObjectError(ValueError):
    pass


@dataclass(frozen=True)
class SessionObjectBlock:
    object_id: int
    total_size: int
    offset: int
    raw: bytes


def _parse_session_object_at(body: bytes, start: int) -> tuple[SessionObjectBlock, int] | None:
    """Parse one complete 0x1e chunk beginning at *start*."""
    header_end = start + 23
    if start < 0 or header_end > len(body):
        return None
    if body[start:start + len(SESSION_OBJECT_PREFIX)] != SESSION_OBJECT_PREFIX:
        return None

    object_id = int.from_bytes(body[start + 5:start + 9], "big")
    total_size = int.from_bytes(body[start + 9:start + 13], "big")
    offset = int.from_bytes(body[start + 13:start + 17], "big")
    maximum_chunk_size = int.from_bytes(body[start + 17:start + 21], "big")
    chunk_size = int.from_bytes(body[start + 21:start + 23], "big")
    end = header_end + chunk_size
    if (
        object_id <= 0
        or total_size <= 0
        or offset not in SESSION_OBJECT_CHUNK_OFFSETS
        or maximum_chunk_size <= 0
        or chunk_size <= 0
        or chunk_size > maximum_chunk_size
        or offset >= total_size
        or offset + chunk_size > total_size
        or end > len(body)
    ):
        return None
    return (
        SessionObjectBlock(object_id, total_size, offset, body[start:end]),
        end,
    )


def iter_session_object_blocks(raw: bytes) -> Iterator[SessionObjectBlock]:
    """Yield complete 0x1e chunks, including chunks embedded in compounds."""
    body = bytes(raw)
    cursor = 0
    while cursor < len(body):
        start = body.find(SESSION_OBJECT_PREFIX, cursor)
        if start < 0:
            return
        parsed = _parse_session_object_at(body, start)
        if parsed is None:
            cursor = start + 1
            continue
        block, cursor = parsed
        yield block


def parse_session_object_block(raw: bytes) -> SessionObjectBlock | None:
    body = bytes(raw)
    if not body.startswith(SESSION_OBJECT_PREFIX):
        return None
    parsed = _parse_session_object_at(body, 0)
    return parsed[0] if parsed is not None else None


def unique_blocks(blocks: Iterable[bytes]) -> tuple[SessionObjectBlock, ...]:
    by_offset: dict[int, SessionObjectBlock] = {}
    for raw in blocks:
        parsed = parse_session_object_block(raw)
        if parsed is not None:
            by_offset.setdefault(parsed.offset, parsed)
    return tuple(by_offset[offset] for offset in sorted(by_offset))


def is_session_object_complete(blocks: Iterable[bytes]) -> bool:
    return {item.offset for item in unique_blocks(blocks)} >= set(SESSION_OBJECT_CHUNK_OFFSETS)


def is_capture_complete(blocks: Iterable[bytes]) -> bool:
    """Compatibility alias; use :func:`is_session_object_complete`."""
    return is_session_object_complete(blocks)


def first_block_identity(blocks: Iterable[bytes]) -> tuple[int, int, str]:
    """Return source object id, player id and embedded persona when available."""
    first = next((item.raw for item in unique_blocks(blocks) if item.offset == 0), b"")
    if not first:
        return 0, 0, ""
    player_id = int.from_bytes(first[31:35], "big") if len(first) >= 35 else 0
    name = ""
    marker = first.find(b"\x22\x01\x2b\x18", 23)
    if marker >= 0 and marker + 6 <= len(first):
        size = first[marker + 5]
        start = marker + 6
        end = start + size
        if end <= len(first):
            name = first[start:end].decode("ascii", errors="ignore")
    return int.from_bytes(first[5:9], "big"), player_id, name


def rewrite_for_receiver(
    blocks: Iterable[bytes],
    *,
    remote_object_id: int,
    remote_slot: int,
) -> tuple[bytes, ...]:
    object_id = int(remote_object_id)
    slot = int(remote_slot)
    if not 1 <= object_id <= 0xFFFFFFFF:
        raise SessionObjectError(f"invalid remote object id: {remote_object_id}")
    if not 0 <= slot <= 0xFFFFFFFF:
        raise SessionObjectError(f"invalid remote slot: {remote_slot}")

    parsed = unique_blocks(blocks)
    if not {item.offset for item in parsed} >= set(SESSION_OBJECT_CHUNK_OFFSETS):
        raise SessionObjectError(
            "incomplete 0x1e object; expected offsets 0, 0x1e4 and 0x3c8"
        )
    rewritten: list[bytes] = []
    for item in parsed:
        body = bytearray(item.raw)
        body[5:9] = object_id.to_bytes(4, "big")
        if item.offset == 0:
            if len(body) < 43:
                raise SessionObjectError("truncated offset-zero 0x1e block")
            # Type-4 stores the receiver-local player slot twice.  The source
            # publishes these fields as ``0 / -1`` until it is allocated.
            # Retail rewrites both to the destination slot: invite-join
            # frames 766 -> 777 and Challenge frames 2484 -> 2493 change
            # ``0 / ffffffff`` to ``1 / 1`` for the joining guest.  Leaving
            # the first copy at zero creates an internally inconsistent
            # remote object and the guest never completes its native
            # ClockSyncStart (0x02) transition.
            body[35:39] = slot.to_bytes(4, "big")
            body[39:43] = slot.to_bytes(4, "big")
        rewritten.append(bytes(body))
    return tuple(rewritten)
