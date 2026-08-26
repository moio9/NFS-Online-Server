"""Game and service catalogue for the shared U2/MW server."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet


class GameId(str, Enum):
    MOST_WANTED = "most_wanted"
    UNDERGROUND2 = "underground2"


class EAService(str, Enum):
    BOOTSTRAP = "bootstrap"
    LOBBY = "lobby"
    MESSENGER = "messenger"
    RACE_RELAY = "race_relay"
    WEB = "web"


@dataclass(frozen=True)
class GameProfile:
    game: GameId
    services: FrozenSet[EAService]

    def supports(self, service: EAService) -> bool:
        return service in self.services


_CLASSIC_SERVICES = frozenset(
    {
        EAService.BOOTSTRAP,
        EAService.LOBBY,
        EAService.MESSENGER,
        EAService.RACE_RELAY,
        EAService.WEB,
    }
)

PROFILES = {
    GameId.MOST_WANTED: GameProfile(GameId.MOST_WANTED, _CLASSIC_SERVICES),
    GameId.UNDERGROUND2: GameProfile(GameId.UNDERGROUND2, _CLASSIC_SERVICES),
}
