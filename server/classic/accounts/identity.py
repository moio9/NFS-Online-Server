"""Shared identity/session store for Underground 2 and Most Wanted."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import md5
import secrets
from threading import RLock
from typing import Callable


@dataclass(frozen=True)
class Identity:
    account_name: str
    persona: str
    profile_id: int
    user_id: int
    account_id: int | None = None
    persona_id: int | None = None


class IdentityStore:
    def __init__(self, token_factory: Callable[[], str] | None = None) -> None:
        self._lock = RLock()
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(20)[:27] + ".")
        self._identities: dict[str, Identity] = {}
        self._sessions: dict[str, Identity] = {}

    @staticmethod
    def profile_id_for(persona: str) -> int:
        normalized = str(persona or "Player").strip().casefold()
        raw = int.from_bytes(md5(normalized.encode("utf-8")).digest()[:4], "big")
        return 100_000_000 + (raw % 900_000_000)

    def login(self, account_name: str, persona: str | None = None) -> tuple[Identity, str]:
        account = str(account_name or "Player").strip() or "Player"
        display = str(persona or account).strip() or account
        key = f"{account.casefold()}\x00{display.casefold()}"
        with self._lock:
            identity = self._identities.get(key)
            if identity is None:
                profile_id = self.profile_id_for(display)
                identity = Identity(account, display, profile_id, profile_id)
                self._identities[key] = identity
            token = self._token_factory()
            if not token:
                raise ValueError("session token factory returned an empty token")
            self._sessions[token] = identity
            return identity, token

    def resolve_session(self, token: str) -> Identity | None:
        with self._lock:
            return self._sessions.get(str(token or ""))

    def revoke_session(self, token: str) -> bool:
        with self._lock:
            return self._sessions.pop(str(token or ""), None) is not None
