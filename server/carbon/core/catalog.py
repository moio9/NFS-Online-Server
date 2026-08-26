"""Explicit game/service topology; shared code never guesses a game protocol."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet


class GameId(str, Enum):
    CARBON = "carbon"
    MOST_WANTED = "most_wanted"
    UNDERGROUND2 = "underground2"


class EAService(str, Enum):
    BOOTSTRAP = "bootstrap"
    LOBBY = "lobby"
    FESL = "fesl"
    MESSENGER = "messenger"
    THEATER = "theater"
    REBROADCASTER = "rebroadcaster"
    GAME_MANAGER = "game_manager"
    RACE_RELAY = "race_relay"
    WEB = "web"


@dataclass(frozen=True)
class GameProfile:
    game: GameId
    services: FrozenSet[EAService]

    def supports(self, service: EAService) -> bool:
        return service in self.services


PROFILES = {
    GameId.CARBON: GameProfile(
        GameId.CARBON,
        frozenset(
            {
                EAService.FESL,
                EAService.MESSENGER,
                EAService.THEATER,
                EAService.REBROADCASTER,
                EAService.GAME_MANAGER,
                EAService.RACE_RELAY,
                EAService.WEB,
            }
        ),
    ),
    GameId.MOST_WANTED: GameProfile(
        GameId.MOST_WANTED,
        frozenset({EAService.BOOTSTRAP, EAService.LOBBY, EAService.MESSENGER, EAService.RACE_RELAY}),
    ),
    GameId.UNDERGROUND2: GameProfile(
        GameId.UNDERGROUND2,
        frozenset({EAService.BOOTSTRAP, EAService.LOBBY, EAService.MESSENGER, EAService.RACE_RELAY}),
    ),
}
