"""Authoritative Carbon GameManager bootstrap planning.

This layer converts Theater participants plus their bound UDP endpoints into
wire records.  It deliberately has no socket or ProtoTunnel state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from carbon.gamemanager.player_codec import PlayerWireData
from carbon.theater.directory import CarbonParticipant


Address = tuple[str, int]


@dataclass(frozen=True)
class BoundParticipant:
    participant: CarbonParticipant
    external_address: Address


def _usable_port(value: int, fallback: int) -> int:
    port = int(value or 0)
    if 0 < port <= 0xFFFF:
        return port
    return int(fallback) if 0 < int(fallback) <= 0xFFFF else 0


def _usable_ip(value: str, fallback: str) -> str:
    candidate = str(value or "").strip()
    return candidate if candidate and candidate != "0.0.0.0" else str(fallback)


def player_wire_data(
    bound: BoundParticipant,
    *,
    local_player_id: int,
    force_state: int | None = None,
) -> PlayerWireData:
    participant = bound.participant
    external_ip, external_port = bound.external_address
    pid = int(participant.player_id)
    state = int(force_state) if force_state is not None else (3 if pid == int(local_player_id) else 6)
    internal_ip = _usable_ip(participant.internal_ip, external_ip)
    internal_port = _usable_port(participant.internal_port, external_port)
    return PlayerWireData(
        player_id=pid,
        name=participant.identity.persona,
        # Stock Carbon captures and the working V619 path use the GameManager
        # player id in this signed-int64 identity field.
        profile_id=pid,
        state=state,
        internal_ip=internal_ip,
        internal_port=internal_port,
        external_ip=str(external_ip),
        external_port=_usable_port(external_port, internal_port),
    )


def local_first(
    participants: Iterable[BoundParticipant],
    *,
    local_player_id: int,
) -> tuple[BoundParticipant, ...]:
    """Return one de-duplicated local-first roster in stable PID order."""
    by_pid: dict[int, BoundParticipant] = {}
    for item in participants:
        pid = int(item.participant.player_id)
        if pid > 0:
            by_pid.setdefault(pid, item)
    local = by_pid.pop(int(local_player_id), None)
    ordered = [local] if local is not None else []
    ordered.extend(by_pid[pid] for pid in sorted(by_pid))
    return tuple(item for item in ordered if item is not None)
