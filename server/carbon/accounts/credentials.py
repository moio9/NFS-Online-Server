"""Durable password authentication for Carbon FESL accounts.

The live Carbon adapter historically accepted any ``acct/Login`` request.  This
module adds a deliberately separate credential store so account passwords never
share a file with the much larger progression/stat payload.  Open login remains
the default at the application layer; this store is consulted only when
``AUTH_MODE=password`` is configured.
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
from typing import Callable


PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 210_000


@dataclass(frozen=True)
class CredentialAccount:
    account_name: str
    persona: str
    password_hash: str
    enabled: bool = True
    email: str = ""
    dob_day: str = ""
    dob_month: str = ""
    dob_year: str = ""
    country_code: str = ""
    zip_code: str = ""
    ea_mail_flag: str = ""
    third_party_mail_flag: str = ""


@dataclass(frozen=True)
class AuthenticationResult:
    accepted: bool
    reason: str
    account_name: str = ""
    persona: str = ""
    retry_after_seconds: int = 0


class CredentialAccountExistsError(ValueError):
    """Raised when registration tries to reuse an existing account name."""


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
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}
        # Unknown-account checks run one real PBKDF2 operation so the response
        # time does not trivially reveal whether an account exists.
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
    def _clean_display(value: object) -> str:
        return str(value or "").strip()

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

    @staticmethod
    def _account_from_value(raw_name: object, value: object) -> tuple[CredentialAccount | None, bool]:
        if not isinstance(value, dict):
            return None, False
        account_name = str(
            value.get("account_name")
            or value.get("name")
            or value.get("username")
            or value.get("email")
            or raw_name
            or ""
        ).strip()
        if not account_name:
            return None, False
        persona = str(
            value.get("persona")
            or value.get("display_name")
            or account_name
        ).strip() or account_name
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
                encoded = CredentialStore.encode_password(str(plaintext))
                migrated = True
        if not encoded:
            return None, migrated
        enabled_raw = value.get("enabled", not bool(value.get("disabled", False)))
        if isinstance(enabled_raw, str):
            enabled = enabled_raw.strip().casefold() not in {"0", "false", "no", "off"}
        else:
            enabled = bool(enabled_raw)
        return CredentialAccount(
            account_name=account_name,
            persona=persona,
            password_hash=encoded,
            enabled=enabled,
            email=str(value.get("email", "") or "").strip(),
            dob_day=str(value.get("dob_day", value.get("DOBDay", "")) or "").strip(),
            dob_month=str(
                value.get("dob_month", value.get("DOBMonth", "")) or ""
            ).strip(),
            dob_year=str(value.get("dob_year", value.get("DOBYear", "")) or "").strip(),
            country_code=str(
                value.get("country_code", value.get("countryCode", "")) or ""
            ).strip(),
            zip_code=str(value.get("zip_code", value.get("zipCode", "")) or "").strip(),
            ea_mail_flag=str(
                value.get("ea_mail_flag", value.get("eaMailFlag", "")) or ""
            ).strip(),
            third_party_mail_flag=str(
                value.get(
                    "third_party_mail_flag",
                    value.get("thirdPartyMailFlag", ""),
                )
                or ""
            ).strip(),
        ), migrated

    def _load(self) -> None:
        assert self.path is not None
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"invalid Carbon credential data {self.path}: {exc}") from exc

        values: list[tuple[object, object]] = []
        if isinstance(raw, dict) and isinstance(raw.get("accounts"), dict):
            values.extend(raw["accounts"].items())
        elif isinstance(raw, dict) and isinstance(raw.get("users"), list):
            values.extend((index, item) for index, item in enumerate(raw["users"]))
        elif isinstance(raw, list):
            values.extend((index, item) for index, item in enumerate(raw))
        elif raw not in ({}, None):
            raise ValueError(
                f"invalid Carbon credential data {self.path}: expected accounts object or users list"
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
            "version": 2,
            "accounts": {
                account.account_name: {
                    "country_code": account.country_code,
                    "dob_day": account.dob_day,
                    "dob_month": account.dob_month,
                    "dob_year": account.dob_year,
                    "ea_mail_flag": account.ea_mail_flag,
                    "email": account.email,
                    "enabled": account.enabled,
                    "password_pbkdf2": account.password_hash,
                    "persona": account.persona,
                    "third_party_mail_flag": account.third_party_mail_flag,
                    "zip_code": account.zip_code,
                }
                for _key, account in sorted(self._accounts.items())
            },
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.path)

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
        name = self._clean_display(account_name)
        if not name:
            raise ValueError("account name is required")
        secret = str(password or "")
        if not secret:
            raise ValueError("password is required")
        display = self._clean_display(persona) or name
        key = self.normalize_account_name(name)
        with self._lock:
            if key in self._accounts and not replace:
                raise CredentialAccountExistsError(
                    f"account already exists: {name}"
                )
            account = CredentialAccount(
                account_name=name,
                persona=display,
                password_hash=self.encode_password(
                    secret,
                    salt_factory=self._salt_factory,
                ),
                enabled=True,
                email=self._clean_display(email),
                dob_day=self._clean_display(dob_day),
                dob_month=self._clean_display(dob_month),
                dob_year=self._clean_display(dob_year),
                country_code=self._clean_display(country_code),
                zip_code=self._clean_display(zip_code),
                ea_mail_flag=self._clean_display(ea_mail_flag),
                third_party_mail_flag=self._clean_display(
                    third_party_mail_flag
                ),
            )
            self._accounts[key] = account
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)
            self._save_locked()
            return account

    def set_password(self, account_name: str, password: str) -> CredentialAccount:
        key = self.normalize_account_name(account_name)
        secret = str(password or "")
        if not key:
            raise ValueError("account name is required")
        if not secret:
            raise ValueError("password is required")
        with self._lock:
            current = self._accounts.get(key)
            if current is None:
                raise KeyError(str(account_name))
            updated = replace(
                current,
                password_hash=self.encode_password(
                    secret,
                    salt_factory=self._salt_factory,
                ),
            )
            self._accounts[key] = updated
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)
            self._save_locked()
            return updated

    def set_enabled(self, account_name: str, enabled: bool) -> CredentialAccount:
        key = self.normalize_account_name(account_name)
        if not key:
            raise ValueError("account name is required")
        with self._lock:
            current = self._accounts.get(key)
            if current is None:
                raise KeyError(str(account_name))
            updated = replace(current, enabled=bool(enabled))
            self._accounts[key] = updated
            if enabled:
                self._failures.pop(key, None)
                self._locked_until.pop(key, None)
            self._save_locked()
            return updated

    def accounts(self) -> tuple[CredentialAccount, ...]:
        with self._lock:
            return tuple(sorted(self._accounts.values(), key=lambda item: item.account_name.casefold()))

    def screen_name_available(self, value: object) -> bool:
        key = self.normalize_account_name(value)
        if not key:
            return False
        with self._lock:
            for account in self._accounts.values():
                if key in {
                    self.normalize_account_name(account.account_name),
                    self.normalize_account_name(account.persona),
                    self.normalize_account_name(account.email),
                }:
                    return False
        return True

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
        name = self._clean_display(account_name)
        secret = str(password or "")
        if not name:
            return AuthenticationResult(False, "missing_name")
        if not secret:
            return AuthenticationResult(False, "missing_password")
        key = self.normalize_account_name(name)
        now = float(self._clock())
        with self._lock:
            locked = self._locked_result(key, now)
            if locked is not None:
                return locked
            account = self._accounts.get(key)
            if account is None:
                self.verify_password(secret, self._dummy_password_hash)
                return AuthenticationResult(False, "unknown_account")
            if not account.enabled:
                return AuthenticationResult(False, "disabled")
            if not self.verify_password(secret, account.password_hash):
                locked = self._note_failure_locked(key, now)
                return locked or AuthenticationResult(False, "bad_password")

            incoming_email = self._clean_display(email)
            if incoming_email and not account.email:
                email_key = self.normalize_account_name(incoming_email)
                for other_key, other in self._accounts.items():
                    if other_key != key and self.normalize_account_name(other.email) == email_key:
                        raise CredentialAccountExistsError(
                            f"email already exists: {incoming_email}"
                        )
            updated = replace(
                account,
                email=account.email or incoming_email,
                dob_day=account.dob_day or self._clean_display(dob_day),
                dob_month=account.dob_month or self._clean_display(dob_month),
                dob_year=account.dob_year or self._clean_display(dob_year),
                country_code=account.country_code or self._clean_display(country_code),
                zip_code=account.zip_code or self._clean_display(zip_code),
                ea_mail_flag=account.ea_mail_flag or self._clean_display(ea_mail_flag),
                third_party_mail_flag=(
                    account.third_party_mail_flag
                    or self._clean_display(third_party_mail_flag)
                ),
            )
            if updated != account:
                self._accounts[key] = updated
                self._save_locked()
                reason = "profile_completed"
            else:
                reason = "ok"
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)
            return AuthenticationResult(True, reason, updated.account_name, updated.persona)

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

    def authenticate(self, account_name: str, password: str) -> AuthenticationResult:
        name = self._clean_display(account_name)
        secret = str(password or "")
        if not name:
            return AuthenticationResult(False, "missing_name")
        if not secret:
            return AuthenticationResult(False, "missing_password")
        key = self.normalize_account_name(name)
        now = float(self._clock())
        with self._lock:
            locked = self._locked_result(key, now)
            if locked is not None:
                return locked
            account = self._accounts.get(key)
            if account is None:
                if self.auto_enroll:
                    account = CredentialAccount(
                        name,
                        name,
                        self.encode_password(secret, salt_factory=self._salt_factory),
                        True,
                    )
                    self._accounts[key] = account
                    self._save_locked()
                    return AuthenticationResult(True, "enrolled", account.account_name, account.persona)
                self.verify_password(secret, self._dummy_password_hash)
                return AuthenticationResult(False, "unknown_account")
            password_valid = self.verify_password(secret, account.password_hash)
            if not account.enabled:
                return AuthenticationResult(False, "disabled")
            if not password_valid:
                locked = self._note_failure_locked(key, now)
                return locked or AuthenticationResult(False, "bad_password")
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)
            return AuthenticationResult(True, "ok", account.account_name, account.persona)
