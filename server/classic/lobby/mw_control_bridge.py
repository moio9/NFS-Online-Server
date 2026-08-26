"""Small in-process bridge between MW lobby AUX state and Messenger AUXI callbacks."""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True)
class MWControlProjection:
    wire_id: int
    game_id: int
    persona: str
    aux: str
    address: str
    client_ip: str


_lock = RLock()
_by_wire: dict[int, MWControlProjection] = {}
_by_persona: dict[str, MWControlProjection] = {}
_by_client: dict[str, MWControlProjection] = {}


def update_mw_control_projection(
    *,
    wire_id: int,
    game_id: int,
    persona: str,
    aux: str,
    address: str,
    client_ip: str,
) -> None:
    projection = MWControlProjection(
        max(0, int(wire_id)),
        max(0, int(game_id)),
        str(persona or "Player"),
        str(aux or ""),
        str(address or client_ip or "0.0.0.0"),
        str(client_ip or ""),
    )
    with _lock:
        if projection.wire_id:
            _by_wire[projection.wire_id] = projection
        if projection.persona:
            _by_persona[projection.persona.casefold()] = projection
        if projection.client_ip:
            _by_client[projection.client_ip] = projection


def resolve_mw_control_projection(
    *,
    wire_id: int = 0,
    persona: str = "",
    client_ip: str = "",
) -> MWControlProjection | None:
    with _lock:
        if int(wire_id or 0) in _by_wire:
            return _by_wire[int(wire_id)]
        key = str(persona or "").casefold()
        if key and key in _by_persona:
            return _by_persona[key]
        return _by_client.get(str(client_ip or ""))
