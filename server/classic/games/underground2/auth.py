"""Underground 2 authentication adapter built on shared classic EA services."""

from __future__ import annotations

from classic.accounts.credentials import CredentialStore
from classic.accounts.identity import IdentityStore
from classic.protocols.auth import (
    ClassicActiveSessionRegistry,
    ClassicAuthProfile,
    ClassicAuthService,
)


UNDERGROUND2_AUTH_PROFILE = ClassicAuthProfile(
    game_id="underground2",
    auth_payload_length=130,
    persona_payload_length=116,
    tos_value=3,
    last_login="2005.12.8 15:51:38",
    persona_last_login="2006.12.8 15:51:58",
    persona_previous_login="2006.12.8 16:51:40",
    birth_date="20030520",
    fallback_email="",
)


def create_auth_service(
    credentials: CredentialStore,
    identities: IdentityStore,
    *,
    active_sessions: ClassicActiveSessionRegistry | None = None,
    verify_passwords: bool = True,
    auto_enroll: bool = False,
    allow_create: bool = True,
    allow_password_reset: bool = False,
) -> ClassicAuthService:
    return ClassicAuthService(
        credentials,
        identities,
        profile=UNDERGROUND2_AUTH_PROFILE,
        active_sessions=active_sessions,
        verify_passwords=verify_passwords,
        auto_enroll=auto_enroll,
        allow_create=allow_create,
        allow_password_reset=allow_password_reset,
    )
