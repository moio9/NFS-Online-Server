"""In-memory Carbon Messenger state received from COnline over loopback IPC."""

from __future__ import annotations

from dataclasses import dataclass
import time
from threading import RLock
from typing import Callable, Mapping

from common.ipc import AuthenticatedJSONIPCServer
from classic.core.config import Endpoint


@dataclass(frozen=True)
class CarbonIPCIdentity:
    account_name: str
    persona: str
    profile_id: int
    user_id: int
    wire_player_id: int = 0


@dataclass(frozen=True)
class CarbonIPCForcedLogoff:
    identity: CarbonIPCIdentity
    reason: str
    theater_ready: bool = False


class CarbonMessengerIPCState:
    def __init__(
        self,
        *,
        max_age_seconds: float = 5.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.max_age_seconds = max(0.25, float(max_age_seconds))
        self._clock = clock or time.time
        self._lock = RLock()
        self._active = False
        self._instance_id = ""
        self._updated_at = 0.0
        self._sessions: dict[str, CarbonIPCIdentity] = {}
        self._forced_logoffs: dict[str, CarbonIPCForcedLogoff] = {}
        self._known: dict[str, CarbonIPCIdentity] = {}
        self._rooms: dict[str, dict[str, object]] = {}

    @staticmethod
    def _identity(raw: object) -> CarbonIPCIdentity | None:
        if not isinstance(raw, Mapping):
            return None
        persona = str(raw.get("persona", "") or "").strip()
        if not persona:
            return None
        try:
            return CarbonIPCIdentity(
                str(raw.get("account_name", persona) or persona).strip() or persona,
                persona,
                int(raw.get("profile_id", 0) or 0),
                int(raw.get("user_id", 0) or 0),
                int(raw.get("wire_player_id", 0) or 0),
            )
        except (TypeError, ValueError):
            return None

    def apply(self, payload: Mapping[str, object]) -> None:
        if str(payload.get("game", "")).casefold() != "carbon":
            return
        kind = str(payload.get("kind", "snapshot")).casefold()
        instance_id = str(payload.get("instance_id", ""))
        with self._lock:
            if kind == "shutdown":
                if not self._instance_id or self._instance_id == instance_id:
                    self._active = False
                    self._sessions = {}
                    self._forced_logoffs = {}
                    self._rooms = {}
                    self._updated_at = float(self._clock())
                return
            sessions: dict[str, CarbonIPCIdentity] = {}
            raw_sessions = payload.get("sessions", {})
            if isinstance(raw_sessions, Mapping):
                for token, value in raw_sessions.items():
                    identity = self._identity(value)
                    if identity is not None and str(token):
                        sessions[str(token)] = identity
            forced_logoffs: dict[str, CarbonIPCForcedLogoff] = {}
            raw_forced_logoffs = payload.get("forced_logoffs", {})
            if isinstance(raw_forced_logoffs, Mapping):
                for token, value in raw_forced_logoffs.items():
                    identity = self._identity(value)
                    reason = (
                        str(value.get("reason", "") or "").strip().upper()
                        if isinstance(value, Mapping)
                        else ""
                    )
                    if identity is not None and str(token) and reason:
                        forced_logoffs[str(token)] = CarbonIPCForcedLogoff(
                            identity,
                            reason,
                            bool(value.get("theater_ready", False)),
                        )
            known: dict[str, CarbonIPCIdentity] = {}
            raw_known = payload.get("known_identities", [])
            if isinstance(raw_known, list):
                for value in raw_known:
                    identity = self._identity(value)
                    if identity is not None:
                        known[identity.persona.casefold()] = identity
            for identity in sessions.values():
                known[identity.persona.casefold()] = identity
            rooms: dict[str, dict[str, object]] = {}
            raw_rooms = payload.get("rooms", {})
            if isinstance(raw_rooms, Mapping):
                for key, value in raw_rooms.items():
                    if isinstance(value, Mapping):
                        rooms[str(key).casefold()] = dict(value)
            self._active = True
            self._instance_id = instance_id
            self._updated_at = float(self._clock())
            self._sessions = sessions
            self._forced_logoffs = forced_logoffs
            self._known = known
            self._rooms = rooms

    def _usable_locked(self) -> bool:
        return self._active and float(self._clock()) - self._updated_at <= self.max_age_seconds

    def available(self) -> bool:
        with self._lock:
            return self._usable_locked()

    def resolve_session(self, token: str) -> CarbonIPCIdentity | None:
        with self._lock:
            if not self._usable_locked():
                return None
            return self._sessions.get(str(token or ""))

    def forced_logoff(self, token: str) -> CarbonIPCForcedLogoff | None:
        with self._lock:
            if not self._usable_locked():
                return None
            return self._forced_logoffs.get(str(token or ""))

    def known_identities(self) -> tuple[CarbonIPCIdentity, ...]:
        with self._lock:
            if not self._usable_locked():
                return ()
            return tuple(self._known[key] for key in sorted(self._known))

    def identity_for_persona(self, persona: object) -> CarbonIPCIdentity | None:
        with self._lock:
            if not self._usable_locked():
                return None
            return self._known.get(str(persona or "").strip().casefold())

    @staticmethod
    def wire_player_id(identity: CarbonIPCIdentity) -> int:
        return int(identity.wire_player_id)

    def session_id_for_persona(self, persona: object) -> str:
        with self._lock:
            if not self._usable_locked():
                return ""
            room = self._rooms.get(str(persona or "").strip().casefold(), {})
            return str(room.get("session_id", "") or "").strip()

    def invite_join_complete(self, persona: object, session_id: object) -> bool:
        with self._lock:
            if not self._usable_locked():
                return False
            room = self._rooms.get(str(persona or "").strip().casefold(), {})
            expected = str(session_id or "").strip()
            return bool(
                expected
                and str(room.get("session_id", "") or "").strip() == expected
                and room.get("invite_join_complete", False)
            )

    def is_inviteable(self, identity: CarbonIPCIdentity) -> bool:
        with self._lock:
            if not self._usable_locked():
                return False
            return bool(self._rooms.get(identity.persona.casefold(), {}).get("inviteable", False))

    def invite_details(self, identity: CarbonIPCIdentity) -> dict[str, str]:
        with self._lock:
            if not self._usable_locked():
                return {}
            details = self._rooms.get(identity.persona.casefold(), {}).get("details", {})
            if not isinstance(details, Mapping):
                return {}
            return {
                str(key): str(value)
                for key, value in details.items()
                if str(key) and str(value)
            }


class CarbonMessengerIPCReceiver:
    def __init__(
        self,
        endpoint: Endpoint,
        *,
        secret: str,
        state: CarbonMessengerIPCState,
    ) -> None:
        self.state = state
        self.server = AuthenticatedJSONIPCServer(
            endpoint,
            secret=secret,
            on_message=state.apply,
            name="carbon-messenger-ipc",
        )

    @property
    def bound_endpoint(self) -> Endpoint:
        return self.server.bound_endpoint

    def start(self) -> Endpoint:
        return self.server.start()

    def stop(self) -> None:
        self.server.stop()
