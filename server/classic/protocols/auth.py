"""Shared U2/MW authentication domain and classic wire replies."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import md5
from threading import RLock
from collections.abc import Mapping

from classic.accounts.credentials import (
    AuthenticationResult,
    CredentialAccount,
    CredentialStore,
)
from classic.accounts.identity import Identity, IdentityStore

from .frame import ClassicEAFrame
from .password import password_candidates, storage_password_candidate


ERROR_IMST = int.from_bytes(b"imst", "big")
ERROR_LOGN = int.from_bytes(b"logn", "big")
ERROR_PASS = int.from_bytes(b"pass", "big")
ERROR_DUPL = int.from_bytes(b"dupl", "big")


@dataclass(frozen=True)
class ClassicAuthProfile:
    game_id: str
    auth_payload_length: int = 130
    persona_payload_length: int = 116
    tos_value: int = 3
    last_login: str = "2005.12.8 15:51:38"
    persona_last_login: str = "2006.12.8 15:51:58"
    persona_previous_login: str = "2006.12.8 16:51:40"
    birth_date: str = "20030520"
    fallback_email: str = ""
    public_address: str = "127.0.0.1"
    fixed_mask: str = ""


@dataclass
class ClassicAuthContext:
    connection_id: str
    client_ip: str = ""
    session_challenge: str = ""
    mask: str = ""
    account: CredentialAccount | None = None
    identity: Identity | None = None
    session_token: str = ""
    lkey: str = ""
    persona: str = ""


@dataclass(frozen=True)
class ClassicAuthReply:
    accepted: bool
    reason: str
    frames: tuple[bytes, ...]
    close_connection: bool = False


class ClassicActiveSessionRegistry:
    """Atomic duplicate-account and duplicate-persona ownership."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._accounts: dict[str, str] = {}
        self._personas: dict[str, str] = {}
        self._claims: dict[str, tuple[str, str]] = {}

    @staticmethod
    def _key(value: object) -> str:
        return str(value or "").strip().casefold()

    def claim(self, connection_id: str, account_name: str, persona: str) -> str | None:
        connection = str(connection_id or "").strip()
        account_key = self._key(account_name)
        persona_key = self._key(persona)
        if not connection or not account_key or not persona_key:
            return "invalid_claim"
        with self._lock:
            account_owner = self._accounts.get(account_key)
            if account_owner not in (None, connection):
                return "account_in_use"
            persona_owner = self._personas.get(persona_key)
            if persona_owner not in (None, connection):
                return "persona_in_use"
            self.release(connection)
            self._accounts[account_key] = connection
            self._personas[persona_key] = connection
            self._claims[connection] = (account_key, persona_key)
            return None

    def switch_persona(self, connection_id: str, persona: str) -> str | None:
        connection = str(connection_id or "").strip()
        persona_key = self._key(persona)
        if not connection or not persona_key:
            return "invalid_claim"
        with self._lock:
            claim = self._claims.get(connection)
            if claim is None:
                return "not_authenticated"
            owner = self._personas.get(persona_key)
            if owner not in (None, connection):
                return "persona_in_use"
            account_key, old_persona = claim
            if self._personas.get(old_persona) == connection:
                self._personas.pop(old_persona, None)
            self._personas[persona_key] = connection
            self._claims[connection] = (account_key, persona_key)
            return None

    def release(self, connection_id: str) -> None:
        connection = str(connection_id or "").strip()
        if not connection:
            return
        with self._lock:
            claim = self._claims.pop(connection, None)
            if claim is None:
                return
            account_key, persona_key = claim
            if self._accounts.get(account_key) == connection:
                self._accounts.pop(account_key, None)
            if self._personas.get(persona_key) == connection:
                self._personas.pop(persona_key, None)


class ClassicAuthService:
    """Translate U2/MW auth commands into the shared credential backend."""

    IDENTIFIER_FIELDS = (
        "EMAIL",
        "MAIL",
        "PMAIL",
        "U2_OLX_MAIL",
        "USER",
        "USERNAME",
        "LOGIN",
        "NAME",
    )
    PASSWORD_FIELDS = ("PASSWORD", "PASS", "PWORD", "PWD")

    def __init__(
        self,
        credentials: CredentialStore,
        identities: IdentityStore,
        *,
        profile: ClassicAuthProfile,
        active_sessions: ClassicActiveSessionRegistry | None = None,
        verify_passwords: bool = True,
        auto_enroll: bool = False,
        allow_create: bool = True,
        allow_password_reset: bool = False,
    ) -> None:
        self.credentials = credentials
        self.identities = identities
        self.profile = profile
        self.active_sessions = active_sessions or ClassicActiveSessionRegistry()
        self.verify_passwords = bool(verify_passwords)
        self.auto_enroll = bool(auto_enroll)
        self.allow_create = bool(allow_create)
        self.allow_password_reset = bool(allow_password_reset)

    @staticmethod
    def _upper_fields(fields: Mapping[str, object]) -> dict[str, str]:
        return {
            str(key or "").strip().upper(): str(value or "").strip()
            for key, value in fields.items()
        }

    @staticmethod
    def _first(fields: Mapping[str, str], names: tuple[str, ...]) -> str:
        for name in names:
            value = str(fields.get(name, "") or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _persona_value(fields: Mapping[str, str], fallback: str) -> str:
        for key in ("PERS", "PERSONA", "DISPLAY", "DISPLAYNAME", "NAME"):
            value = str(fields.get(key, "") or "").strip()
            if value:
                return value
        return fallback

    @staticmethod
    def _auth_error(reason: str) -> int:
        if reason in {"missing_password", "bad_password"}:
            return ERROR_PASS
        if reason == "account_in_use":
            return ERROR_LOGN
        return ERROR_IMST

    @staticmethod
    def _create_error(reason: str) -> int:
        if reason == "missing_password":
            return ERROR_PASS
        if reason in {"account_exists", "persona_in_use"}:
            return ERROR_DUPL
        return ERROR_IMST

    @staticmethod
    def _reject(command: str, error: int) -> bytes:
        return ClassicEAFrame.signed(
            command,
            b"\x00",
            9,
            reserved=error,
        ).encode()

    def account_policy_frame(self, action: str) -> bytes:
        """Return the stock signed auth rejection used for a live policy change."""

        policy = str(action or "").strip().casefold()
        if policy not in {"ban", "disable", "kick"}:
            raise ValueError(f"unsupported restrictive account policy: {action!r}")
        return self._reject("auth", ERROR_IMST)

    def _resolve_account(self, account_name: str) -> CredentialAccount | None:
        return self.credentials.resolve_account(account_name)

    def _auth_payload(self, account: CredentialAccount, display_name: str) -> bytes:
        primary_persona = account.persona or display_name
        configured_personas = tuple(account.all_personas)
        preferred_email = account.email or self.profile.fallback_email

        def persona_values(primary: str) -> tuple[str, ...]:
            values: list[str] = []
            for value in (primary, *configured_personas):
                cleaned = str(value or "").strip()
                if cleaned and cleaned.casefold() not in {item.casefold() for item in values}:
                    values.append(cleaned)
            return tuple(values) or (primary,)

        def build(mail: str, personas: tuple[str, ...], name: str) -> bytes:
            text = "\n".join(
                (
                    f"MAIL={mail}",
                    f"LAST={self.profile.last_login}",
                    f"BORN={self.profile.birth_date}",
                    f"PERSONAS={','.join(personas)}",
                    f"TOS={self.profile.tos_value}",
                    f"NAME={name}",
                    "SPAM=N",
                    f"ADDR={self.profile.public_address}",
                )
            ) + "\n"
            return text.encode("utf-8") + b"\x00"

        capacity = self.profile.auth_payload_length - 8
        personas = persona_values(primary_persona)
        name = display_name
        mail = preferred_email
        payload = build(mail, personas, name)

        # The captured U2 auth payload has only 122 bytes before its MD5
        # trailer.  PERSONAS is the client's persisted persona-list handoff,
        # while MAIL and NAME are only account display metadata.  Compact the
        # latter first so newly created personas remain visible after relogin.
        if len(payload) > capacity:
            mail = ""
            payload = build(mail, personas, name)

        if len(payload) > capacity:
            name = ""
            payload = build(mail, personas, name)

        # Keep as many complete persona identifiers as the captured fixed-size
        # frame permits.  Never cut a secondary persona in half: the client
        # sends these values back verbatim in pers/cper.
        if len(payload) > capacity:
            fitting: list[str] = []
            for persona in personas:
                candidate = (*fitting, persona)
                if len(build(mail, candidate, name)) > capacity:
                    break
                fitting.append(persona)
            personas = tuple(fitting) or (personas[0],)
            payload = build(mail, personas, name)

        if len(payload) > capacity:
            raise ValueError(
                "classic auth payload cannot represent the primary persona without truncation"
            )
        return payload

    def _persona_payload(self, context: ClassicAuthContext, persona: str) -> bytes:
        display_name = context.account.account_name if context.account else persona
        required = (
            f"LKEY={context.lkey}",
            f"PERS={persona}",
        )
        optional = (
            f"LAST={self.profile.persona_last_login}",
            f"PLAST={self.profile.persona_previous_login}",
            f"NAME={display_name}",
        )
        capacity = self.profile.persona_payload_length - 8

        # The retail frame has a fixed payload budget.  Preserve LKEY/PERS
        # exactly and remove only trailing display metadata when it cannot fit;
        # the previous implementation sliced raw bytes and could emit a broken
        # terminal field such as ``NA\0``.
        for optional_count in range(len(optional), -1, -1):
            text = "\n".join((*required, *optional[:optional_count])) + "\n"
            payload = text.encode("utf-8") + b"\x00"
            if len(payload) <= capacity:
                return payload
        raise ValueError(
            "classic persona payload cannot represent LKEY/PERS without truncation"
        )

    def dispatch(
        self,
        frame: ClassicEAFrame,
        context: ClassicAuthContext,
    ) -> ClassicAuthReply:
        """Dispatch one classic auth/persona frame for a game listener."""
        # ``acct`` persists an account and seeds account/persona metadata on
        # this context, but the connection does not own a session lease until
        # the following ``auth`` succeeds.  Touch only fully authenticated
        # contexts so the stock ``acct -> auth`` sequence can claim its first
        # lease instead of being mistaken for a revoked session.
        if context.identity is not None and hasattr(self.active_sessions, "touch"):
            if not self.active_sessions.touch(context.connection_id):
                self.release(context)
                return ClassicAuthReply(False, "session_revoked", (), True)
        command = frame.command.casefold()
        fields = frame.fields()
        if command == "auth":
            return self.login(fields, context)
        if command == "acct":
            return self.create_account(fields, context)
        if command in {"pers", "cper"}:
            return self.select_persona(command, fields, context)
        if command == "dper":
            return self.delete_persona(fields, context)
        return ClassicAuthReply(False, "unsupported_command", ())

    def login(
        self,
        fields: Mapping[str, object],
        context: ClassicAuthContext,
    ) -> ClassicAuthReply:
        values = self._upper_fields(fields)
        if context.session_challenge:
            values.setdefault("SESS", context.session_challenge)
        if context.mask:
            values.setdefault("MASK", context.mask)
            values.setdefault("CHALLENGE", context.mask)
        identifier = self._first(values, self.IDENTIFIER_FIELDS)
        supplied = self._first(values, self.PASSWORD_FIELDS)
        candidates = password_candidates(
            values,
            supplied,
            fixed_mask=context.mask or self.profile.fixed_mask,
        )

        if self.verify_passwords:
            result = self.credentials.authenticate_candidates(identifier, candidates)
            if (
                not result.accepted
                and result.reason == "unknown_account"
                and self.auto_enroll
                and identifier
                and candidates
            ):
                persona = self._persona_value(values, identifier)
                try:
                    account = self.credentials.create_account(
                        identifier,
                        storage_password_candidate(candidates),
                        persona=persona,
                        email=identifier if "@" in identifier else values.get("EMAIL", ""),
                        aliases=(values.get("NAME", ""), values.get("USER", "")),
                        personas=(persona,),
                    )
                except ValueError:
                    result = AuthenticationResult(False, "unknown_account")
                else:
                    result = AuthenticationResult(True, "enrolled", account.account_name, account.persona)
        else:
            if not identifier:
                result = AuthenticationResult(False, "missing_name")
            else:
                account = self._resolve_account(identifier)
                if account is None:
                    if self.auto_enroll:
                        try:
                            account = self.credentials.create_account(
                                identifier,
                                storage_password_candidate(candidates) or "classic-open-login",
                                persona=self._persona_value(values, identifier),
                            )
                        except ValueError:
                            account = None
                    if account is None:
                        result = AuthenticationResult(
                            True,
                            "open",
                            identifier,
                            self._persona_value(values, identifier),
                        )
                    else:
                        result = AuthenticationResult(True, "open", account.account_name, account.persona)
                else:
                    if account.banned:
                        result = AuthenticationResult(False, "banned")
                    elif self.credentials.is_email_blocked(account.email):
                        result = AuthenticationResult(False, "banned")
                    elif not account.enabled:
                        result = AuthenticationResult(False, "disabled")
                    else:
                        result = AuthenticationResult(
                            True,
                            "open",
                            account.account_name,
                            account.persona,
                        )

        if not result.accepted:
            return ClassicAuthReply(
                False,
                result.reason,
                (self._reject("auth", self._auth_error(result.reason)),),
                # The stock client needs the socket to remain alive long
                # enough to consume and render the signed auth error.
                close_connection=False,
            )

        account = self._resolve_account(result.account_name or identifier)
        if account is None:
            # Open-mode ephemeral identity; no persistent account policy.
            account = CredentialAccount(
                result.account_name or identifier,
                result.persona or identifier,
                "",
                personas=(result.persona or identifier,),
            )
        persona = account.persona or result.persona or account.account_name
        try:
            payload = self._auth_payload(account, account.account_name)
        except ValueError:
            return ClassicAuthReply(
                False,
                "wire_identity_too_long",
                (self._reject("auth", ERROR_IMST),),
                close_connection=False,
            )
        conflict = self.active_sessions.claim(
            context.connection_id,
            account.account_name,
            persona,
        )
        if conflict is not None:
            return ClassicAuthReply(
                False,
                conflict,
                (self._reject("auth", self._auth_error(conflict)),),
                close_connection=False,
            )

        identity, token = self.identities.login(account.account_name, persona)
        context.account = account
        context.identity = identity
        context.session_token = token
        context.lkey = md5(token.encode("utf-8")).hexdigest()
        context.persona = persona
        frame = ClassicEAFrame.signed(
            "auth",
            payload,
            self.profile.auth_payload_length,
        ).encode()
        return ClassicAuthReply(True, result.reason, (frame,))

    def create_account(
        self,
        fields: Mapping[str, object],
        context: ClassicAuthContext,
    ) -> ClassicAuthReply:
        values = self._upper_fields(fields)
        identifier = self._first(values, self.IDENTIFIER_FIELDS)
        supplied = self._first(values, self.PASSWORD_FIELDS)
        candidates = password_candidates(
            values,
            supplied,
            fixed_mask=context.mask or self.profile.fixed_mask,
        )
        if not self.allow_create:
            return ClassicAuthReply(False, "create_disabled", (self._reject("acct", ERROR_IMST),))
        if not identifier:
            return ClassicAuthReply(False, "missing_name", (self._reject("acct", ERROR_IMST),))
        if not candidates:
            return ClassicAuthReply(False, "missing_password", (self._reject("acct", ERROR_PASS),))

        existing = self._resolve_account(identifier)
        if existing is not None:
            if existing.banned or self.credentials.is_email_blocked(existing.email):
                return ClassicAuthReply(
                    False,
                    "banned",
                    (self._reject("acct", ERROR_IMST),),
                )
            if not existing.enabled:
                return ClassicAuthReply(
                    False,
                    "disabled",
                    (self._reject("acct", ERROR_IMST),),
                )
            password_matches = any(
                self.credentials.verify_password(candidate, existing.password_hash)
                for candidate in candidates
            )
            if password_matches:
                context.account = existing
                context.persona = existing.persona
                return ClassicAuthReply(True, "ok", (self._reject("acct", 0),))
            if not self.allow_password_reset:
                return ClassicAuthReply(False, "account_exists", (self._reject("acct", ERROR_DUPL),))
            self.credentials.set_password(identifier, storage_password_candidate(candidates))
            account = self._resolve_account(identifier)
            assert account is not None
            context.account = account
            context.persona = account.persona
            return ClassicAuthReply(True, "updated", (self._reject("acct", 0),))

        persona = self._persona_value(values, identifier)
        email = values.get("EMAIL", "") or (identifier if "@" in identifier else "")
        aliases = tuple(
            value
            for value in (values.get("NAME", ""), values.get("USER", ""), values.get("USERNAME", ""))
            if value
        )
        try:
            account = self.credentials.create_account(
                identifier,
                storage_password_candidate(candidates),
                persona=persona,
                email=email,
                aliases=aliases,
                personas=(persona,),
            )
        except ValueError as exc:
            reason = "account_exists" if "exists" in str(exc).casefold() else "save_failed"
            return ClassicAuthReply(False, reason, (self._reject("acct", self._create_error(reason)),))
        context.account = account
        context.persona = account.persona
        return ClassicAuthReply(True, "created", (self._reject("acct", 0),))

    def select_persona(
        self,
        command: str,
        fields: Mapping[str, object],
        context: ClassicAuthContext,
    ) -> ClassicAuthReply:
        cmd = "cper" if str(command or "").casefold() == "cper" else "pers"
        if context.account is None or context.identity is None or not context.session_token:
            return ClassicAuthReply(False, "not_authenticated", (self._reject(cmd, ERROR_IMST),), True)
        current_account = self._resolve_account(context.account.account_name)
        if current_account is None:
            return ClassicAuthReply(False, "not_authenticated", (self._reject(cmd, ERROR_IMST),), True)
        if current_account.banned or self.credentials.is_email_blocked(current_account.email):
            return ClassicAuthReply(False, "banned", (self._reject(cmd, ERROR_IMST),), True)
        if not current_account.enabled:
            return ClassicAuthReply(False, "disabled", (self._reject(cmd, ERROR_IMST),), True)
        context.account = current_account
        values = self._upper_fields(fields)
        requested = self._persona_value(values, context.persona or context.account.persona)
        if not requested:
            return ClassicAuthReply(False, "missing_persona", (self._reject(cmd, ERROR_IMST),))
        try:
            self._persona_payload(context, requested)
        except ValueError:
            return ClassicAuthReply(
                False,
                "wire_identity_too_long",
                (self._reject(cmd, ERROR_IMST),),
            )
        if requested.casefold() not in {value.casefold() for value in context.account.all_personas}:
            try:
                context.account = self.credentials.add_persona(
                    context.account.account_name,
                    requested,
                )
            except ValueError:
                if cmd == "cper":
                    return ClassicAuthReply(False, "persona_in_use", (self._reject("cper", ERROR_DUPL),))
                return ClassicAuthReply(False, "persona_in_use", (ClassicEAFrame.short("userbadc"),), True)
        conflict = self.active_sessions.switch_persona(context.connection_id, requested)
        if conflict is not None:
            if cmd == "cper":
                return ClassicAuthReply(False, conflict, (self._reject("cper", ERROR_DUPL),))
            return ClassicAuthReply(False, conflict, (ClassicEAFrame.short("userbadc"),), True)
        context.persona = requested
        old_token = context.session_token
        context.identity, context.session_token = self.identities.login(
            context.account.account_name,
            requested,
        )
        context.lkey = md5(context.session_token.encode("utf-8")).hexdigest()
        if old_token:
            self.identities.revoke_session(old_token)
        payload = self._persona_payload(context, requested)
        frame = ClassicEAFrame.signed(
            cmd,
            payload,
            self.profile.persona_payload_length,
        ).encode()
        return ClassicAuthReply(True, "ok", (frame,))

    def delete_persona(
        self,
        fields: Mapping[str, object],
        context: ClassicAuthContext,
    ) -> ClassicAuthReply:
        values = self._upper_fields(fields)
        requested = values.get("PERS", "").strip()
        ok = False
        close_connection = False
        if (
            context.account is not None
            and context.identity is not None
            and context.session_token
            and requested
        ):
            current_account = self._resolve_account(context.account.account_name)
            if (
                current_account is None
                or current_account.banned
                or not current_account.enabled
                or self.credentials.is_email_blocked(current_account.email)
            ):
                payload = (
                    f"PERS={requested}\nRESULT=1\n".encode("utf-8") + b"\x00"
                )
                frame = ClassicEAFrame.signed(
                    "dper",
                    payload,
                    self.profile.persona_payload_length,
                ).encode()
                return ClassicAuthReply(
                    False,
                    "not_authenticated",
                    (frame,),
                    True,
                )
            context.account = current_account
            try:
                context.account = self.credentials.remove_persona(
                    context.account.account_name,
                    requested,
                )
            except (KeyError, ValueError):
                ok = False
            else:
                ok = True
                if context.persona.casefold() == requested.casefold():
                    replacement = context.account.persona
                    conflict = self.active_sessions.claim(
                        context.connection_id,
                        context.account.account_name,
                        replacement,
                    )
                    if conflict is None:
                        old_token = context.session_token
                        context.identity, context.session_token = self.identities.login(
                            context.account.account_name,
                            replacement,
                        )
                        context.lkey = md5(
                            context.session_token.encode("utf-8")
                        ).hexdigest()
                        context.persona = replacement
                        if old_token:
                            self.identities.revoke_session(old_token)
                    else:
                        self.release(context)
                        close_connection = True
        payload = f"PERS={requested}\nRESULT={'0' if ok else '1'}\n".encode("utf-8") + b"\x00"
        frame = ClassicEAFrame.signed(
            "dper",
            payload,
            self.profile.persona_payload_length,
        ).encode()
        return ClassicAuthReply(
            ok,
            "ok" if ok else "persona_delete_failed",
            (frame,),
            close_connection,
        )

    def release(self, context: ClassicAuthContext) -> None:
        self.active_sessions.release(context.connection_id)
        if context.session_token:
            self.identities.revoke_session(context.session_token)
        context.account = None
        context.identity = None
        context.session_token = ""
        context.lkey = ""
        context.persona = ""
