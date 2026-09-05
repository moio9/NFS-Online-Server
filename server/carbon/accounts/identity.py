"""Shared identity/session store used by game-specific login adapters."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import md5
import secrets
from threading import RLock
from typing import Callable


# Although the wire field is 16-bit, all audited retail Carbon sessions use
# positive signed-short player identifiers.  Keeping the high bit clear also
# avoids client code paths that promote the value through a signed ``short``.
MAX_CARBON_WIRE_PLAYER_ID = 0x7FFF
MAX_FORCED_LOGOFF_MARKERS = 256


@dataclass(frozen=True)
class Identity:
    account_name: str
    persona: str
    profile_id: int
    user_id: int


class IdentityStore:
    def __init__(self, token_factory: Callable[[], str] | None = None) -> None:
        self._lock = RLock()
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(20)[:27] + ".")
        self._identities: dict[str, Identity] = {}
        self._sessions: dict[str, Identity] = {}
        self._forced_logoffs: dict[str, tuple[Identity, str]] = {}
        self._forced_logoff_theater_ready: set[str] = set()
        # Carbon exposes a separate positive 15-bit player id on Messenger/Theater/GM.
        # Invite join passes the inviter's Messenger UID back in EGAM UID/R-UID,
        # so that UID must equal the inviter's GameManager player id.
        self._wire_ids: dict[str, int] = {}
        self._wire_id_owners: dict[int, str] = {}

    @staticmethod
    def profile_id_for(persona: str) -> int:
        """Stable Carbon-compatible profile id derived from the persona."""
        normalized = str(persona or "Player").strip().lower()
        raw = int.from_bytes(md5(normalized.encode("utf-8")).digest()[:4], "big")
        return 100_000_000 + (raw % 900_000_000)

    def login(
        self,
        account_name: str,
        persona: str | None = None,
        *,
        forced_logoff_reason: str = "",
    ) -> tuple[Identity, str]:
        account = str(account_name or "Player").strip() or "Player"
        display = str(persona or account).strip() or account
        key = account.casefold()
        with self._lock:
            identity = self._identities.get(key)
            if identity is None or identity.persona != display:
                profile_id = self.profile_id_for(display)
                identity = Identity(account, display, profile_id, profile_id)
                self._identities[key] = identity
            token = self._token_factory()
            if not token:
                raise ValueError("session token factory returned an empty token")
            reason = str(forced_logoff_reason or "").strip().upper()
            if reason:
                previous_token = next(
                    (
                        current_token
                        for current_token, current_identity in self._sessions.items()
                        if current_identity.account_name.casefold() == account.casefold()
                    ),
                    "",
                )
                if not previous_token:
                    raise ValueError("no established session available for forced logoff")
                if token in self._sessions or token in self._forced_logoffs:
                    raise ValueError("session token factory returned an existing token")
                # Retail ADMN/DUPL logs off the already-established session.
                # The newcomer becomes the active session and continues its
                # normal Messenger/Theater bootstrap.
                previous_identity = self._sessions.pop(previous_token)
                self._forced_logoffs[previous_token] = (previous_identity, reason)
                self._forced_logoff_theater_ready.discard(previous_token)
                self._sessions[token] = identity
                while len(self._forced_logoffs) > MAX_FORCED_LOGOFF_MARKERS:
                    expired_token = next(iter(self._forced_logoffs))
                    self._forced_logoffs.pop(expired_token)
                    self._forced_logoff_theater_ready.discard(expired_token)
            else:
                self._forced_logoffs.pop(token, None)
                self._forced_logoff_theater_ready.discard(token)
                self._sessions[token] = identity
            return identity, token


    def wire_player_id(self, identity_or_persona: Identity | str) -> int:
        """Return one stable, collision-safe Carbon wire player id.

        Retail uses the same value in Messenger ``ROST.UID``, Theater
        ``EGEG.PID`` and GameManager roster/join messages.  Account/profile ids
        are a different namespace and may be wider than 16 bits.
        """
        persona = (
            identity_or_persona.persona
            if isinstance(identity_or_persona, Identity)
            else str(identity_or_persona or "Player")
        )
        key = persona.strip().casefold() or "player"
        with self._lock:
            existing = self._wire_ids.get(key)
            if existing is not None:
                return existing
            raw = md5(key.encode("utf-8")).digest()
            candidate = 1 + (
                int.from_bytes(raw[4:6], "big") % MAX_CARBON_WIRE_PLAYER_ID
            )
            while candidate in self._wire_id_owners and self._wire_id_owners[candidate] != key:
                candidate = (
                    1 if candidate >= MAX_CARBON_WIRE_PLAYER_ID else candidate + 1
                )
            self._wire_ids[key] = candidate
            self._wire_id_owners[candidate] = key
            return candidate

    def active_sessions(self) -> tuple[tuple[str, Identity, int], ...]:
        """Return an immutable snapshot for the external Messenger bridge."""
        with self._lock:
            return tuple(
                (token, identity, self.wire_player_id(identity))
                for token, identity in self._sessions.items()
            )

    def active_session_token(self, account_name: str) -> str:
        key = str(account_name or "").strip().casefold()
        with self._lock:
            return next(
                (
                    token
                    for token, identity in self._sessions.items()
                    if identity.account_name.casefold() == key
                ),
                "",
            )

    def forced_logoffs(self) -> tuple[tuple[str, Identity, str, int], ...]:
        """Return displaced session tokens retained for Messenger ``ADMN`` delivery."""

        with self._lock:
            return tuple(
                (token, identity, reason, self.wire_player_id(identity))
                for token, (identity, reason) in self._forced_logoffs.items()
            )

    def forced_logoff_reason(self, token: str) -> str:
        """Return the native Messenger reason reserved for a rejected token."""

        with self._lock:
            forced = self._forced_logoffs.get(str(token or ""))
            return forced[1] if forced is not None else ""

    def resolve_forced_logoff(self, token: str) -> tuple[Identity, str] | None:
        """Resolve a rejected token for read-only client bootstrap services."""

        with self._lock:
            return self._forced_logoffs.get(str(token or ""))

    def mark_forced_logoff_theater_ready(self, token: str) -> bool:
        """Publish that the rejected client received its Theater GLST reply."""

        key = str(token or "")
        with self._lock:
            if key not in self._forced_logoffs:
                return False
            self._forced_logoff_theater_ready.add(key)
            return True

    def forced_logoff_theater_ready(self, token: str) -> bool:
        with self._lock:
            return str(token or "") in self._forced_logoff_theater_ready

    def resolve_session(self, token: str) -> Identity | None:
        with self._lock:
            return self._sessions.get(str(token or ""))

    def revoke_session(self, token: str) -> bool:
        with self._lock:
            return self._sessions.pop(str(token or ""), None) is not None
