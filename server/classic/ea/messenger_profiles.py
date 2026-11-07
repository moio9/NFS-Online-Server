"""Game-specific Messenger profiles for Underground 2 and Most Wanted."""
from __future__ import annotations

from dataclasses import dataclass
from classic.core.catalog import GameId


@dataclass(frozen=True)
class EAMessengerProfile:
    game: GameId
    title: str
    resource: str
    default_show: str
    default_status: str
    default_product: str = ""
    default_attr: str = ""


MESSENGER_PROFILES: dict[GameId, EAMessengerProfile] = {
    GameId.MOST_WANTED: EAMessengerProfile(
        game=GameId.MOST_WANTED,
        title="Need for Speed Most Wanted [PC]",
        resource="eagames/NFS-2006",
        default_show="PASS",
        default_status="EX%3d0%0aP%3dnfs6%0a",
        default_product="is playing Most Wanted",
        default_attr="D",
    ),
    GameId.UNDERGROUND2: EAMessengerProfile(
        game=GameId.UNDERGROUND2,
        title="Need for Speed Underground 2 [PC]",
        resource="eagames/NFS-2005",
        default_show="PASS",
        default_status="EX%3d0%0aP%3dnfs5%0a",
        default_product="is playing Underground 2",
        default_attr="D",
    ),
}


def messenger_profile(game: GameId) -> EAMessengerProfile:
    return MESSENGER_PROFILES[game]
