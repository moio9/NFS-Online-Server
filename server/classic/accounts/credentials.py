"""Shared durable credentials for all game-specific login adapters.

Passwords are stored separately from game progression data and encoded with
PBKDF2-HMAC-SHA256.  The store intentionally owns only account policy:
credentials, aliases, personas, enabled/banned state and temporary lockouts.
The Underground 2 and Most Wanted wire adapters translate their own fields
and error codes into this common API.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, replace
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from threading import RLock
from typing import Callable, Iterable


PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 210_000


@dataclass(frozen=True)
class CredentialAccount:
    account_name: str
    persona: str
    password_hash: str
    enabled: bool = True
    banned: bool = False
    email: str = ""
    aliases: tuple[str, ...] = ()
    personas: tuple[str, ...] = ()

    @property
    def all_personas(self) -> tuple[str, ...]:
        """Return primary persona first, followed by unique alternates."""
        values: list[str] = []
        for value in (self.persona, *self.personas):
            text = str(value or "").strip()
            if text and text.casefold() not in {item.casefold() for item in values}:
                values.append(text)
        return tuple(values)

    @property
    def login_identifiers(self) -> tuple[str, ...]:
        """Return all values accepted as an account identifier."""
        values: list[str] = []
        for value in (self.account_name, self.email, *self.aliases):
            text = str(value or "").strip()
            if text and text.casefold() not in {item.casefold() for item in values}:
                values.append(text)
        return tuple(values)


@dataclass(frozen=True)
class AuthenticationResult:
    accepted: bool
    reason: str
    account_name: str = ""
    persona: str = ""
    retry_after_seconds: int = 0


class CredentialStore:
    """Thread-safe, atomic JSON credential store with in-memory lockouts."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        auto_enroll: bool = False,
        failure_limit: int = 5,
        lockout_seconds: float = 300.0,
        clock: Callable[[], float] | None = None,
        salt_factory: Callable[[int], bytes] | None = None,
    ) -> None:
        if int(failure_limit) < 0:
            raise ValueError("failure_limit must be zero or positive")
        if float(lockout_seconds) < 0:
            raise ValueError("lockout_seconds must be zero or positive")
        self.path = Path(path) if path else None
        self.auto_enroll = bool(auto_enroll)
        self.failure_limit = int(failure_limit)
        self.lockout_seconds = float(lockout_seconds)
        self._clock = clock or time.monotonic
        self._salt_factory = salt_factory or os.urandom
        self._lock = RLock()
        self._accounts: dict[str, CredentialAccount] = {}
        self._blocked_emails: set[str] = set()
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}
        # Unknown-account checks perform one real PBKDF2 operation so response
        # timing does not trivially reveal whether an account exists.
        self._dummy_password_hash = self.encode_password(
            "invalid-account-password",
            salt=b"\x00" * 16,
        )
        if self.path is not None:
            self._load()

    @staticmethod
    def normalize_account_name(value: object) -> str:
        return str(value or "").strip().casefold()

    @staticmethod
    def normalize_email(value: object) -> str:
        return str(value or "").strip().casefold()

    @staticmethod
    def _clean_display(value: object) -> str:
        return str(value or "").strip()

    @classmethod
    def _clean_values(cls, values: object) -> tuple[str, ...]:
        if values is None:
            return ()
        if isinstance(values, str):
            source: Iterable[object] = values.replace(";", ",").split(",")
        elif isinstance(values, (list, tuple, set, frozenset)):
            source = values
        else:
            source = (values,)
        result: list[str] = []
        seen: set[str] = set()
        for value in source:
            text = cls._clean_display(value)
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                result.append(text)
        return tuple(result)

    @classmethod
    def encode_password(
        cls,
        password: str,
        *,
        iterations: int = PBKDF2_ITERATIONS,
        salt: bytes | None = None,
        salt_factory: Callable[[int], bytes] | None = None,
    ) -> str:
        if int(iterations) <= 0:
            raise ValueError("PBKDF2 iterations must be positive")
        secret = str(password or "").encode("utf-8")
        actual_salt = salt if salt is not None else (salt_factory or os.urandom)(16)
        if not actual_salt:
            raise ValueError("password salt must not be empty")
        digest = hashlib.pbkdf2_hmac("sha256", secret, actual_salt, int(iterations))
        return "$".join(
            (
                PBKDF2_ALGORITHM,
                str(int(iterations)),
                base64.b64encode(actual_salt).decode("ascii"),
                base64.b64encode(digest).decode("ascii"),
            )
        )

    @staticmethod
    def verify_password(password: str, encoded: str) -> bool:
        try:
            algorithm, iteration_text, salt_text, digest_text = str(encoded or "").split("$", 3)
            if algorithm != PBKDF2_ALGORITHM:
                return False
            iterations = int(iteration_text)
            if iterations <= 0 or iterations > 10_000_000:
                return False
            salt = base64.b64decode(salt_text.encode("ascii"), validate=True)
            expected = base64.b64decode(digest_text.encode("ascii"), validate=True)
            actual = hashlib.pbkdf2_hmac(
                "sha256",
                str(password or "").encode("utf-8"),
                salt,
                iterations,
            )
            return len(actual) == len(expected) and hmac.compare_digest(actual, expected)
        except (TypeError, ValueError, UnicodeError, binascii.Error):
            return False

    @classmethod
    def _account_from_value(
        cls,
        raw_name: object,
        value: object,
    ) -> tuple[CredentialAccount | None, bool]:
        if not isinstance(value, dict):
            return None, False
        account_name = str(
            value.get("account_name")
            or value.get("name")
            or value.get("username")
            or (raw_name if isinstance(raw_name, str) else "")
            or value.get("email")
            or ""
        ).strip()
        if not account_name:
            return None, False

        raw_personas = cls._clean_values(
            value.get("personas")
            or value.get("persona_names")
            or ()
        )
        persona = str(
            value.get("persona")
            or value.get("display_name")
            or (raw_personas[0] if raw_personas else "")
            or account_name
        ).strip() or account_name
        personas = cls._clean_values((persona, *raw_personas))
        aliases = cls._clean_values(
            value.get("aliases")
            or value.get("logins")
            or value.get("usernames")
            or ()
        )

        encoded = str(
            value.get("password_pbkdf2")
            or value.get("pass_wire_pbkdf2")
            or value.get("password_hash")
            or ""
        ).strip()
        migrated = False
        if not encoded:
            plaintext = value.get("password")
            if plaintext is None:
                plaintext = value.get("pass_wire")
            if plaintext is not None:
                encoded = cls.encode_password(str(plaintext))
                migrated = True
        if not encoded:
            return None, migrated

        enabled_raw = value.get("enabled", not bool(value.get("disabled", False)))
        if isinstance(enabled_raw, str):
            enabled = enabled_raw.strip().casefold() not in {"0", "false", "no", "off"}
        else:
            enabled = bool(enabled_raw)
        banned_raw = value.get("banned", False)
        if isinstance(banned_raw, str):
            banned = banned_raw.strip().casefold() in {"1", "true", "yes", "on"}
        else:
            banned = bool(banned_raw)
        email = str(value.get("email") or value.get("mail") or "").strip()
        return CredentialAccount(
            account_name=account_name,
            persona=persona,
            password_hash=encoded,
            enabled=enabled,
            banned=banned,
            email=email,
            aliases=aliases,
            personas=personas,
        ), migrated

    def _load(self) -> None:
        assert self.path is not None
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"invalid credential data {self.path}: {exc}") from exc

        values: list[tuple[object, object]] = []
        if isinstance(raw, dict) and isinstance(raw.get("accounts"), dict):
            values.extend(raw["accounts"].items())
            blocked_emails = raw.get("blocked_emails", [])
            if isinstance(blocked_emails, list):
                self._blocked_emails.update(
                    normalized
                    for item in blocked_emails
                    if (normalized := self.normalize_email(item))
                )
        elif isinstance(raw, dict) and isinstance(raw.get("users"), list):
            values.extend((index, item) for index, item in enumerate(raw["users"]))
        elif isinstance(raw, list):
            values.extend((index, item) for index, item in enumerate(raw))
        elif raw not in ({}, None):
            raise ValueError(
                f"invalid credential data {self.path}: expected accounts object or users list"
            )

        migrated = False
        for raw_name, value in values:
            account, account_migrated = self._account_from_value(raw_name, value)
            migrated = migrated or account_migrated
            if account is None:
                continue
            key = self.normalize_account_name(account.account_name)
            if key:
                self._accounts[key] = account
        if migrated:
            self._save_locked()

    def _save_locked(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 3,
            "blocked_emails": sorted(self._blocked_emails),
            "accounts": {
                account.account_name: {
                    "aliases": list(account.aliases),
                    "banned": account.banned,
                    "email": account.email,
                    "enabled": account.enabled,
                    "password_pbkdf2": account.password_hash,
                    "persona": account.persona,
                    "personas": list(account.all_personas),
                }
                for _key, account in sorted(self._accounts.items())
            },
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def _find_account_key_locked(self, identifier: object) -> str | None:
        wanted = self.normalize_account_name(identifier)
        if not wanted:
            return None
        if wanted in self._accounts:
            return wanted
        for key, account in self._accounts.items():
            if wanted in {
                self.normalize_account_name(value)
                for value in account.login_identifiers
            }:
                return key
        return None

    def _identifier_owner_locked(self, identifier: object) -> str | None:
        return self._find_account_key_locked(identifier)

    def resolve_account(self, identifier: object) -> CredentialAccount | None:
        with self._lock:
            key = self._find_account_key_locked(identifier)
            return self._accounts.get(key) if key is not None else None

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
        name = self._clean_display(account_name)
        if not name:
            raise ValueError("account name is required")
        secret = str(password or "")
        if not secret:
            raise ValueError("password is required")
        display = self._clean_display(persona) or name
        email_address = self._clean_display(email)
        alias_values = self._clean_values(aliases)
        persona_values = self._clean_values((display, *self._clean_values(personas)))
        key = self.normalize_account_name(name)
        with self._lock:
            current_key = self._find_account_key_locked(name)
            if current_key is not None and (not replace or current_key != key):
                raise ValueError(f"account already exists: {name}")
            for identifier in (email_address, *alias_values):
                owner = self._identifier_owner_locked(identifier)
                if owner is not None and owner != key:
                    raise ValueError(f"account identifier already exists: {identifier}")
            if self.normalize_email(email_address) in self._blocked_emails:
                raise ValueError(f"email is blocked: {email_address}")
            account = CredentialAccount(
                account_name=name,
                persona=display,
                password_hash=self.encode_password(secret, salt_factory=self._salt_factory),
                enabled=True,
                banned=False,
                email=email_address,
                aliases=alias_values,
                personas=persona_values,
            )
            self._accounts[key] = account
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)
            self._save_locked()
            return account

    def _require_account_locked(self, identifier: object) -> tuple[str, CredentialAccount]:
        key = self._find_account_key_locked(identifier)
        current = self._accounts.get(key) if key is not None else None
        if key is None or current is None:
            raise KeyError(str(identifier))
        return key, current

    def set_password(self, account_name: str, password: str) -> CredentialAccount:
        secret = str(password or "")
        if not self.normalize_account_name(account_name):
            raise ValueError("account name is required")
        if not secret:
            raise ValueError("password is required")
        with self._lock:
            key, current = self._require_account_locked(account_name)
            updated = replace(
                current,
                password_hash=self.encode_password(secret, salt_factory=self._salt_factory),
            )
            self._accounts[key] = updated
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)
            self._save_locked()
            return updated

    def set_enabled(self, account_name: str, enabled: bool) -> CredentialAccount:
        if not self.normalize_account_name(account_name):
            raise ValueError("account name is required")
        with self._lock:
            key, current = self._require_account_locked(account_name)
            updated = replace(current, enabled=bool(enabled))
            self._accounts[key] = updated
            if enabled:
                self._failures.pop(key, None)
                self._locked_until.pop(key, None)
            self._save_locked()
            return updated

    def set_banned(self, account_name: str, banned: bool) -> CredentialAccount:
        if not self.normalize_account_name(account_name):
            raise ValueError("account name is required")
        with self._lock:
            key, current = self._require_account_locked(account_name)
            updated = replace(current, banned=bool(banned))
            self._accounts[key] = updated
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)
            self._save_locked()
            return updated

    def add_alias(self, account_name: str, alias: str) -> CredentialAccount:
        value = self._clean_display(alias)
        if not value:
            raise ValueError("alias is required")
        with self._lock:
            key, current = self._require_account_locked(account_name)
            owner = self._identifier_owner_locked(value)
            if owner is not None and owner != key:
                raise ValueError(f"account identifier already exists: {value}")
            aliases = self._clean_values((*current.aliases, value))
            updated = replace(current, aliases=aliases)
            self._accounts[key] = updated
            self._save_locked()
            return updated

    def add_persona(self, account_name: str, persona: str) -> CredentialAccount:
        value = self._clean_display(persona)
        if not value:
            raise ValueError("persona is required")
        with self._lock:
            key, current = self._require_account_locked(account_name)
            personas = self._clean_values((*current.all_personas, value))
            updated = replace(current, personas=personas)
            self._accounts[key] = updated
            self._save_locked()
            return updated

    def remove_persona(self, account_name: str, persona: str) -> CredentialAccount:
        value = self._clean_display(persona)
        if not value:
            raise ValueError("persona is required")
        with self._lock:
            key, current = self._require_account_locked(account_name)
            wanted = value.casefold()
            remaining = tuple(item for item in current.all_personas if item.casefold() != wanted)
            if len(remaining) == len(current.all_personas):
                raise KeyError(value)
            if not remaining:
                raise ValueError("an account must retain at least one persona")
            primary = current.persona
            if primary.casefold() == wanted:
                primary = remaining[0]
            updated = replace(current, persona=primary, personas=remaining)
            self._accounts[key] = updated
            self._save_locked()
            return updated

    def set_email_blocked(self, email: str, blocked: bool) -> str:
        normalized = self.normalize_email(email)
        if not normalized or "@" not in normalized:
            raise ValueError("valid email address is required")
        with self._lock:
            if blocked:
                self._blocked_emails.add(normalized)
            else:
                self._blocked_emails.discard(normalized)
            self._save_locked()
        return normalized

    def is_email_blocked(self, email: str) -> bool:
        normalized = self.normalize_email(email)
        with self._lock:
            return bool(normalized and normalized in self._blocked_emails)

    def blocked_emails(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._blocked_emails))

    def accounts(self) -> tuple[CredentialAccount, ...]:
        with self._lock:
            return tuple(sorted(self._accounts.values(), key=lambda item: item.account_name.casefold()))

    def _locked_result(self, key: str, now: float) -> AuthenticationResult | None:
        deadline = self._locked_until.get(key, 0.0)
        if deadline <= now:
            self._locked_until.pop(key, None)
            return None
        return AuthenticationResult(
            False,
            "locked",
            retry_after_seconds=max(1, int(deadline - now + 0.999)),
        )

    def _note_failure_locked(self, key: str, now: float) -> AuthenticationResult | None:
        if self.failure_limit <= 0 or self.lockout_seconds <= 0:
            return None
        failures = self._failures.get(key, 0) + 1
        self._failures[key] = failures
        if failures < self.failure_limit:
            return None
        self._failures.pop(key, None)
        deadline = now + self.lockout_seconds
        self._locked_until[key] = deadline
        return AuthenticationResult(
            False,
            "locked",
            retry_after_seconds=max(1, int(self.lockout_seconds + 0.999)),
        )

    def authenticate_candidates(
        self,
        account_name: str,
        passwords: Iterable[str],
        *,
        allow_passwordless: bool = False,
    ) -> AuthenticationResult:
        """Authenticate one identifier against several wire/plain candidates.

        Classic U2/MW clients may send a reversible ``$hex`` token.  Their
        adapter supplies both the wire token and every safely decoded candidate
        here so only one failed-attempt counter is consumed per login request.
        """
        name = self._clean_display(account_name)
        if not name:
            return AuthenticationResult(False, "missing_name")
        candidate_values: list[str] = []
        for value in passwords:
            candidate = str(value or "")
            if candidate and candidate not in candidate_values:
                candidate_values.append(candidate)
        candidates = tuple(candidate_values)
        if not candidates and not allow_passwordless:
            return AuthenticationResult(False, "missing_password")

        now = float(self._clock())
        with self._lock:
            account_key = self._find_account_key_locked(name)
            rate_key = account_key or self.normalize_account_name(name)
            locked = self._locked_result(rate_key, now)
            if locked is not None:
                return locked
            account = self._accounts.get(account_key) if account_key is not None else None
            if account is None:
                if self.auto_enroll and candidates:
                    account = CredentialAccount(
                        account_name=name,
                        persona=name,
                        password_hash=self.encode_password(candidates[0], salt_factory=self._salt_factory),
                        enabled=True,
                        banned=False,
                        email="",
                        aliases=(),
                        personas=(name,),
                    )
                    key = self.normalize_account_name(name)
                    self._accounts[key] = account
                    self._save_locked()
                    return AuthenticationResult(True, "enrolled", account.account_name, account.persona)
                self.verify_password(candidates[0] if candidates else "", self._dummy_password_hash)
                locked = self._note_failure_locked(rate_key, now)
                return locked or AuthenticationResult(False, "unknown_account")

            password_valid = allow_passwordless or any(
                self.verify_password(candidate, account.password_hash)
                for candidate in candidates
            )
            if account.banned or self.normalize_email(account.email) in self._blocked_emails:
                return AuthenticationResult(False, "banned")
            if not account.enabled:
                return AuthenticationResult(False, "disabled")
            if not password_valid:
                locked = self._note_failure_locked(rate_key, now)
                return locked or AuthenticationResult(False, "bad_password")
            self._failures.pop(rate_key, None)
            self._locked_until.pop(rate_key, None)
            return AuthenticationResult(True, "ok", account.account_name, account.persona)

    def authenticate(self, account_name: str, password: str) -> AuthenticationResult:
        return self.authenticate_candidates(account_name, (str(password or ""),))
