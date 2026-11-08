"""Password-backed Xbox wire authentication through a trusted local bridge.

The Xbox gamertag/XUID is client-controlled and is never treated as proof of
ownership. An upstream bridge must supply the ordinary Classic account name
and password token over a protected connection.
"""
from __future__ import annotations

from classic.games.most_wanted.auth import MOST_WANTED_AUTH_PROFILE
from classic.protocols.auth import ClassicAuthReply, ClassicAuthService
from classic.protocols.frame import ClassicEAFrame


class XboxPasswordAuth(ClassicAuthService):
    def login(self, fields, context):
        gamertag = str(fields.get("GTAG", "")).strip()
        if not gamertag or len(gamertag) > 15 or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-"
            for char in gamertag
        ):
            return ClassicAuthReply(False, "invalid_gamertag", (self._reject("auth", 1),))
        result = super().login(fields, context)
        if not result.accepted:
            return result
        persona = context.persona
        wire = ClassicEAFrame.from_fields("auth", {
            "NAME": persona, "ADDR": context.client_ip, "PERSONAS": persona,
            "LOC": "enUS", "MAIL": "", "SPAM": "NN", "TOS": 1,
            "SINCE": "2005.11.15-00:00:00", "LAST": "2005.11.15-00:00:00",
        }).encode()
        return ClassicAuthReply(True, result.reason, (wire,))

    def select_persona(self, command, fields, context):
        # Never trust GTAG as an instruction to add a persona to an account.
        result = super().select_persona(command, {"PERS": context.persona}, context)
        if not result.accepted:
            return result
        wire = ClassicEAFrame.from_fields("pers", {
            "PERS": context.persona, "LKEY": context.lkey, "LOC": "enUS",
            "A": context.client_ip, "LA": context.client_ip, "IDLE": 600000,
        }).encode()
        return ClassicAuthReply(True, result.reason, (wire,))


def create_auth_service(credentials, identities, *, active_sessions=None, **_kwargs):
    return XboxPasswordAuth(
        credentials, identities, profile=MOST_WANTED_AUTH_PROFILE,
        active_sessions=active_sessions, verify_passwords=True,
        auto_enroll=False, allow_create=False,
    )
