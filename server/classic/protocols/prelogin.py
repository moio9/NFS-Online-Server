"""Shared U2/MW pre-login service composition root."""

from __future__ import annotations

import logging

from classic.core.config import Endpoint
from classic.ea.directory import GameSession, SessionDirectory, SessionState
from classic.ea.ranking import ClassicRankingStore
from classic.lobby.connection_registry import ClassicConnectionRegistryMixin
from classic.lobby.constants import (
    U2_ACTIVE_SYSFLAG,
    U2_PARTITION_COUNT,
    U2_PARTITION_INDEX,
    U2_PASSWORD_SYSFLAG,
    U2_READY_FLAG,
)
from classic.lobby.endpoints import ClassicEndpointMixin
from classic.lobby.enforcement import ClassicAccountEnforcementMixin
from classic.lobby.feedback import ClassicFeedbackMixin
from classic.lobby.game_commands import ClassicGameCommandMixin
from classic.lobby.game_search import ClassicGameSearchMixin
from classic.lobby.handshake import ClassicHandshakeMixin
from classic.lobby.lifecycle import ClassicLifecycleMixin
from classic.lobby.messages import ClassicMessageMixin
from classic.lobby.models import (
    ClassicPreloginContext,
    ClassicPreloginProfile,
    ClassicPreloginReply,
    ClassicUserset,
)
from classic.lobby.mw_sessions import ClassicMWSessionMixin
from classic.lobby.presence import ClassicPresenceMixin
from classic.lobby.ranking import ClassicRankingMixin
from classic.lobby.router import ClassicRouterMixin
from classic.lobby.selection import ClassicSelectionMixin
from classic.lobby.snapshots import ClassicSnapshotMixin
from classic.lobby.u2_rooms import ClassicU2RoomMixin
from classic.lobby.usersets import ClassicUsersetMixin
from classic.lobby.wire import ClassicWireMixin
from classic.ea.social import SocialService

from .auth import ClassicAuthContext, ClassicAuthReply, ClassicAuthService
from .frame import ClassicEAFrame
from .stream import ClassicEAShortFrame


log = logging.getLogger(__name__)


class ClassicPreloginService(
    ClassicAccountEnforcementMixin,
    ClassicRouterMixin,
    ClassicHandshakeMixin,
    ClassicLifecycleMixin,
    ClassicGameCommandMixin,
    ClassicFeedbackMixin,
    ClassicMessageMixin,
    ClassicPresenceMixin,
    ClassicSelectionMixin,
    ClassicU2RoomMixin,
    ClassicSnapshotMixin,
    ClassicGameSearchMixin,
    ClassicMWSessionMixin,
    ClassicUsersetMixin,
    ClassicRankingMixin,
    ClassicWireMixin,
    ClassicConnectionRegistryMixin,
    ClassicEndpointMixin,
):
    """Compose the shared Classic lobby domains around the auth service."""

    def __init__(
        self,
        auth: ClassicAuthService,
        *,
        profile: ClassicPreloginProfile,
        control_endpoint: Endpoint,
        web_endpoint: Endpoint | None = None,
        sessions: SessionDirectory | None = None,
        ranking: ClassicRankingStore | None = None,
        social: SocialService | None = None,
    ) -> None:
        self.auth = auth
        self.profile = profile
        self.sessions = sessions or SessionDirectory()
        self.ranking = ranking or ClassicRankingStore()
        self.social = social
        self._init_endpoints(control_endpoint, web_endpoint)
        self._init_connection_registry()
        self._init_mw_usersets()
        self._init_mw_sessions()
        self._init_lifecycle()

    @property
    def _is_most_wanted(self) -> bool:
        return self.profile.game_id == "most_wanted"

    @property
    def _is_underground2(self) -> bool:
        return self.profile.game_id == "underground2"
