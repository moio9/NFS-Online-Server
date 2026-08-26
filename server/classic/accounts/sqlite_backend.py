"""Classic-compatible adapters over the shared SQLite account database."""

from __future__ import annotations

import secrets
from threading import RLock
from typing import Callable, Iterable

from classic.accounts.credentials import AuthenticationResult, CredentialAccount
from classic.accounts.identity import Identity
from common.accounts import (
    SQLiteAccountDatabase,
    SharedAccountExistsError,
    SharedAuthenticationResult,
    SharedPersonaExistsError,
)


class SQLiteCredentialStore:
    """Drop-in credential API used by the existing U2/MW auth service."""

    def __init__(self, database: SQLiteAccountDatabase) -> None:
        self.database = database
        self.path = database.path
        self.auto_enroll = database.auto_enroll
        self.failure_limit = database.failure_limit
        self.lockout_seconds = database.lockout_seconds

    @staticmethod
    def normalize_account_name(value: object) -> str:
        return SQLiteAccountDatabase.normalize(value)

    @staticmethod
    def normalize_email(value: object) -> str:
        return SQLiteAccountDatabase.normalize(value)

    @staticmethod
    def encode_password(password: str, **kwargs) -> str:
        return SQLiteAccountDatabase.encode_password(password, **kwargs)

    @staticmethod
    def verify_password(password: str, encoded: str) -> bool:
        return SQLiteAccountDatabase.verify_password(password, encoded)

    @staticmethod
    def _account(record) -> CredentialAccount:
        return CredentialAccount(
            account_name=record.account_name,
            persona=record.primary_persona,
            password_hash=record.password_hash,
            enabled=record.enabled,
            banned=record.banned,
            email=record.email,
            aliases=record.aliases,
            personas=record.personas,
        )

    @staticmethod
    def _result(result: SharedAuthenticationResult) -> AuthenticationResult:
        return AuthenticationResult(
            result.accepted,
            result.reason,
            result.account_name,
            result.persona,
            result.retry_after_seconds,
        )

    def resolve_account(self, identifier: object) -> CredentialAccount | None:
        record = self.database.resolve_account(identifier)
        return self._account(record) if record is not None else None

    def create_account(
        self,
        account_name: str,
        password: str,
        *,
        persona: str | None = None,
        email: str = "",
        aliases: Iterable[str] = (),
        personas: Iterable[str] = (),
        replace: bool = False,
    ) -> CredentialAccount:
        try:
            record = self.database.create_account(
                account_name,
                password,
                persona=persona,
                email=email,
                aliases=aliases,
                personas=personas,
                replace=replace,
            )
        except SharedPersonaExistsError as exc:
            raise ValueError(f"persona already exists: {persona or account_name}") from exc
        except SharedAccountExistsError as exc:
            raise ValueError(f"account identifier already exists: {account_name}") from exc
        return self._account(record)

    def set_password(self, account_name: str, password: str) -> CredentialAccount:
        return self._account(self.database.set_password(account_name, password))

    def set_enabled(self, account_name: str, enabled: bool) -> CredentialAccount:
        return self._account(self.database.set_enabled(account_name, enabled))

    def set_banned(self, account_name: str, banned: bool) -> CredentialAccount:
        return self._account(self.database.set_banned(account_name, banned))

    def add_alias(self, account_name: str, alias: str) -> CredentialAccount:
        return self._account(self.database.add_alias(account_name, alias))

    def add_persona(self, account_name: str, persona: str) -> CredentialAccount:
        try:
            return self._account(self.database.add_persona(account_name, persona))
        except SharedPersonaExistsError as exc:
            raise ValueError(str(exc)) from exc

    def remove_persona(self, account_name: str, persona: str) -> CredentialAccount:
        return self._account(self.database.remove_persona(account_name, persona))

    def set_email_blocked(self, email: str, blocked: bool) -> str:
        return self.database.set_email_blocked(email, blocked)

    def is_email_blocked(self, email: str) -> bool:
        return self.database.is_email_blocked(email)

    def blocked_emails(self) -> tuple[str, ...]:
        return self.database.blocked_emails()

    def accounts(self) -> tuple[CredentialAccount, ...]:
        return tuple(self._account(record) for record in self.database.accounts())

    def authenticate_candidates(
        self,
        account_name: str,
        passwords: Iterable[str],
        *,
        allow_passwordless: bool = False,
    ) -> AuthenticationResult:
        return self._result(
            self.database.authenticate_candidates(
                account_name,
                passwords,
                allow_passwordless=allow_passwordless,
            )
        )

    def authenticate(self, account_name: str, password: str) -> AuthenticationResult:
        return self._result(self.database.authenticate(account_name, password))


class SQLiteIdentityStore:
    """Persistent persona IDs with process-local session tokens."""

    def __init__(
        self,
        database: SQLiteAccountDatabase,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.database = database
        self._lock = RLock()
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(20)[:27] + ".")
        self._sessions: dict[str, Identity] = {}

    @staticmethod
    def profile_id_for(persona: str) -> int:
        return SQLiteAccountDatabase._profile_candidate(persona)

    def login(self, account_name: str, persona: str | None = None) -> tuple[Identity, str]:
        record = self.database.identity(account_name, persona)
        identity = Identity(
            record.account_name,
            record.persona,
            record.profile_id,
            record.user_id,
            account_id=record.account_id,
            persona_id=record.persona_id,
        )
        token = self._token_factory()
        if not token:
            raise ValueError("session token factory returned an empty token")
        with self._lock:
            self._sessions[token] = identity
        return identity, token

    def resolve_session(self, token: str) -> Identity | None:
        with self._lock:
            return self._sessions.get(str(token or ""))

    def revoke_session(self, token: str) -> bool:
        with self._lock:
            return self._sessions.pop(str(token or ""), None) is not None
