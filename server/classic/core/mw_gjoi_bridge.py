"""Small in-process bridge for stock MW's uppercase GJOI callback socket."""
from __future__ import annotations
from dataclasses import dataclass
from threading import RLock

@dataclass(frozen=True)
class MWGJOICallbackState:
    game_wire: bytes
    user_wire: bytes
    compact_game_wire: bytes

_lock = RLock()
_states: dict[int, MWGJOICallbackState] = {}

def publish_mw_gjoi_callback(
    wire_user_id: int,
    game_wire: bytes,
    user_wire: bytes,
    compact_game_wire: bytes,
) -> None:
    if int(wire_user_id or 0) <= 0:
        return
    state = MWGJOICallbackState(
        bytes(game_wire),
        bytes(user_wire),
        bytes(compact_game_wire),
    )
    with _lock:
        _states[int(wire_user_id)] = state

def resolve_mw_gjoi_callback(wire_user_id: int) -> MWGJOICallbackState | None:
    with _lock:
        return _states.get(int(wire_user_id or 0))
