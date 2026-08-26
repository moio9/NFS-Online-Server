"""Most Wanted authentication adapter using the shared U2-derived core.

Only the profile/wire differences belong here.  The listener and full MW login
flow will be connected after U2 capture-contract tests are green.
"""

from __future__ import annotations

from classic.accounts.credentials import CredentialStore
from classic.accounts.identity import IdentityStore
from classic.protocols.auth import (
    ClassicActiveSessionRegistry,
    ClassicAuthProfile,
    ClassicAuthService,
)


MOST_WANTED_AUTH_PROFILE = ClassicAuthProfile(
    game_id="most_wanted",
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
        profile=MOST_WANTED_AUTH_PROFILE,
        active_sessions=active_sessions,
        verify_passwords=verify_passwords,
        auto_enroll=auto_enroll,
        allow_create=allow_create,
        allow_password_reset=allow_password_reset,
    )
