"""Compatibility exports from the shared common social package."""

from common.social import (
    ControlSender,
    LobbyIdentity,
    Presence,
    RelationResult,
    SocialRow,
    SocialService,
    canonical_persona,
    persona_key,
    stable_persona_id,
)

__all__ = [name for name in globals() if not name.startswith("_")]
