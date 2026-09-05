"""Carbon-compatible adapters over the shared SQLite account database."""

from __future__ import annotations

import secrets
from threading import RLock
from typing import Callable

from carbon.accounts.credentials import (
    AuthenticationResult,
    CredentialAccount,
    CredentialAccountExistsError,
)
from carbon.accounts.identity import Identity, MAX_FORCED_LOGOFF_MARKERS
from common.accounts import (
    MAX_CARBON_WIRE_PLAYER_ID,
    SQLiteAccountDatabase,
    SharedAccountExistsError,
    SharedAuthenticationResult,
    SharedPersonaExistsError,
)


class SQLiteCredentialStore:
    """Drop-in credential API used by Carbon FESL account transactions."""

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
            email=record.email,
            dob_day=record.dob_day,
            dob_month=record.dob_month,
            dob_year=record.dob_year,
            country_code=record.country_code,
            zip_code=record.zip_code,
            ea_mail_flag=record.ea_mail_flag,
            third_party_mail_flag=record.third_party_mail_flag,
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

    def create_account(
        self,
        account_name: str,
        password: str,
        *,
        persona: str | None = None,
        email: str = "",
        dob_day: str = "",
        dob_month: str = "",
        dob_year: str = "",
        country_code: str = "",
        zip_code: str = "",
        ea_mail_flag: str = "",
        third_party_mail_flag: str = "",
        replace: bool = False,
    ) -> CredentialAccount:
        try:
            record = self.database.create_account(
                account_name,
                password,
                persona=persona,
                email=email,
                replace=replace,
                dob_day=dob_day,
                dob_month=dob_month,
                dob_year=dob_year,
                country_code=country_code,
                zip_code=zip_code,
                ea_mail_flag=ea_mail_flag,
                third_party_mail_flag=third_party_mail_flag,
            )
        except (SharedAccountExistsError, SharedPersonaExistsError) as exc:
            raise CredentialAccountExistsError(str(exc)) from exc
        return self._account(record)

    def set_password(self, account_name: str, password: str) -> CredentialAccount:
        return self._account(self.database.set_password(account_name, password))

    def set_enabled(self, account_name: str, enabled: bool) -> CredentialAccount:
        return self._account(self.database.set_enabled(account_name, enabled))

    def accounts(self) -> tuple[CredentialAccount, ...]:
        return tuple(self._account(record) for record in self.database.accounts())

    def screen_name_available(self, value: object) -> bool:
        return self.database.screen_name_available(value)

    def complete_missing_profile(
        self,
        account_name: str,
        password: str,
        *,
        email: str = "",
        dob_day: str = "",
        dob_month: str = "",
        dob_year: str = "",
        country_code: str = "",
        zip_code: str = "",
        ea_mail_flag: str = "",
        third_party_mail_flag: str = "",
    ) -> AuthenticationResult:
        try:
            result = self.database.complete_missing_profile(
                account_name,
                password,
                email=email,
                dob_day=dob_day,
                dob_month=dob_month,
                dob_year=dob_year,
                country_code=country_code,
                zip_code=zip_code,
                ea_mail_flag=ea_mail_flag,
                third_party_mail_flag=third_party_mail_flag,
            )
        except SharedAccountExistsError as exc:
            raise CredentialAccountExistsError(str(exc)) from exc
        return self._result(result)

    def authenticate(self, account_name: str, password: str) -> AuthenticationResult:
        return self._result(self.database.authenticate(account_name, password))


class SQLiteIdentityStore:
    """Persistent Carbon profile/wire IDs with local FESL session tokens."""

    def __init__(
        self,
        database: SQLiteAccountDatabase,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.database = database
        self._lock = RLock()
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(20)[:27] + ".")
        self._sessions: dict[str, Identity] = {}
        self._forced_logoffs: dict[str, tuple[Identity, str]] = {}
        self._forced_logoff_theater_ready: set[str] = set()
        self._wire_ids: dict[str, int] = {}

    @staticmethod
    def profile_id_for(persona: str) -> int:
        return SQLiteAccountDatabase._profile_candidate(persona)

    def login(
        self,
        account_name: str,
        persona: str | None = None,
        *,
        forced_logoff_reason: str = "",
    ) -> tuple[Identity, str]:
        record = self.database.identity(
            account_name,
            persona,
            require_carbon_wire_id=True,
        )
        identity = Identity(
            record.account_name,
            record.persona,
            record.profile_id,
            record.user_id,
        )
        token = self._token_factory()
        if not token:
            raise ValueError("session token factory returned an empty token")
        with self._lock:
            reason = str(forced_logoff_reason or "").strip().upper()
            if reason:
                previous_token = next(
                    (
                        current_token
                        for current_token, current_identity in self._sessions.items()
                        if current_identity.account_name.casefold() == identity.account_name.casefold()
                    ),
                    "",
                )
                if not previous_token:
                    raise ValueError("no established session available for forced logoff")
                if token in self._sessions or token in self._forced_logoffs:
                    raise ValueError("session token factory returned an existing token")
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
            assert record.carbon_wire_player_id is not None
            self._wire_ids[record.persona.casefold()] = record.carbon_wire_player_id
        return identity, token

    def wire_player_id(self, identity_or_persona: Identity | str) -> int:
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
        # Resolve globally by persona.  Carbon persona names are unique in the
        # shared database, so no account identifier is required here.
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM personas WHERE display_name_key=?",
                (key,),
            ).fetchone()
            if row is None:
                raise KeyError(persona)
            wire_id = row["carbon_wire_player_id"]
            if wire_id is None:
                wire_id = self.database._allocate_wire_id(connection, str(row["display_name"]))
                connection.execute(
                    "UPDATE personas SET carbon_wire_player_id=?, updated_at=? WHERE persona_id=?",
                    (wire_id, self.database._clock(), int(row["persona_id"])),
                )
        value = int(wire_id)
        if not 1 <= value <= MAX_CARBON_WIRE_PLAYER_ID:
            raise ValueError(f"invalid persisted Carbon wire player id: {value}")
        with self._lock:
            self._wire_ids[key] = value
        return value

    def active_sessions(self) -> tuple[tuple[str, Identity, int], ...]:
        with self._lock:
            rows = tuple(self._sessions.items())
        return tuple((token, identity, self.wire_player_id(identity)) for token, identity in rows)

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
            rows = tuple(self._forced_logoffs.items())
        return tuple(
            (token, identity, reason, self.wire_player_id(identity))
            for token, (identity, reason) in rows
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
