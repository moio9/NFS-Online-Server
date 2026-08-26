"""Minimal RC4 stream transform used by Carbon ProtoTunnel captures.

ProtoTunnel stores an absolute RC4 stream offset in four-byte words.  A busy
Carbon race crosses the 16-bit wire-offset wrap quickly, so rebuilding RC4 and
discarding every byte from position zero for each virtual packet becomes
quadratic in the lifetime of the connection.  Keep a small, deterministic
checkpoint window per key so sequential encode/decode calls resume near their
requested absolute position while producing the exact same wire bytes.
"""

from __future__ import annotations

from collections import OrderedDict
from threading import RLock


_CHECKPOINTS_PER_KEY = 256
_CHECKPOINT_KEY_LIMIT = 32
_checkpoint_lock = RLock()
_checkpoints: OrderedDict[
    bytes,
    OrderedDict[int, tuple[tuple[int, ...], int, int]],
] = OrderedDict()


def _initial_state(key: bytes) -> tuple[list[int], int, int]:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 0xFF
        state[i], state[j] = state[j], state[i]
    return state, 0, 0


def _remember_state(
    key: bytes,
    position: int,
    state: list[int],
    i: int,
    j: int,
) -> None:
    snapshot = (tuple(state), int(i), int(j))
    with _checkpoint_lock:
        positions = _checkpoints.setdefault(key, OrderedDict())
        _checkpoints.move_to_end(key)
        positions[position] = snapshot
        positions.move_to_end(position)
        while len(positions) > _CHECKPOINTS_PER_KEY:
            positions.popitem(last=False)
        while len(_checkpoints) > _CHECKPOINT_KEY_LIMIT:
            _checkpoints.popitem(last=False)


def _state_at(key: bytes, position: int) -> tuple[list[int], int, int]:
    with _checkpoint_lock:
        positions = _checkpoints.get(key)
        if positions is not None:
            _checkpoints.move_to_end(key)
        exact = positions.get(position) if positions is not None else None
        if exact is not None:
            positions.move_to_end(position)
            state, i, j = exact
            return list(state), i, j

        nearest_position = -1
        nearest: tuple[tuple[int, ...], int, int] | None = None
        if positions is not None:
            for candidate_position, candidate in positions.items():
                if nearest_position < candidate_position <= position:
                    nearest_position = candidate_position
                    nearest = candidate

    if nearest is None:
        state, i, j = _initial_state(key)
        current_position = 0
    else:
        snapshot, i, j = nearest
        state = list(snapshot)
        current_position = nearest_position

    for _ in range(position - current_position):
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]

    _remember_state(key, position, state, i, j)
    return state, i, j


def rc4_xor(data: bytes, key: bytes, *, skip: int = 0) -> bytes:
    raw_key = bytes(key)
    if not raw_key:
        return bytes(data)
    if skip < 0:
        raise ValueError("RC4 skip must be non-negative")

    state, i, j = _state_at(raw_key, skip)
    output = bytearray(len(data))
    for position, value in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        stream_byte = state[(state[i] + state[j]) & 0xFF]
        output[position] = value ^ stream_byte
    _remember_state(raw_key, skip + len(data), state, i, j)
    return bytes(output)
