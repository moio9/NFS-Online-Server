"""Legacy defaults for callers using SocialService without game adapters.

New adapters should install their own policies. The Carbon default preserves
the historical direct-call API for existing integrations.
"""
from common.social import GameSessionPolicy

_DEFAULT = GameSessionPolicy()
_LEGACY = {"carbon": GameSessionPolicy(record_recent_players=True, publish_leave=False)}


def legacy_session_policy(game_id: str) -> GameSessionPolicy:
    return _LEGACY.get(game_id, _DEFAULT)
