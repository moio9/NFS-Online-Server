"""Shared EA service contracts, deliberately independent of game wire formats."""

from .directory import GameSession, Room, SessionDirectory, SessionState, Visibility
from .social import (
    LobbyIdentity,
    Presence,
    RelationResult,
    SocialRow,
    SocialService,
    canonical_persona,
    persona_key,
    stable_persona_id,
)
from .text import encode_message, parse_message

__all__ = [
    "GameSession",
    "LobbyIdentity",
    "Presence",
    "RelationResult",
    "Room",
    "SessionDirectory",
    "SessionState",
    "SocialRow",
    "SocialService",
    "Visibility",
    "canonical_persona",
    "encode_message",
    "parse_message",
    "persona_key",
    "stable_persona_id",
]
