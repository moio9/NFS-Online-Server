"""U2-derived classic EA authentication rewrite contract tests."""

from hashlib import md5
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from classic.accounts.credentials import CredentialStore
from classic.accounts.identity import IdentityStore
from classic.games.most_wanted.auth import create_auth_service as create_mw_auth_service
from classic.games.underground2.auth import create_auth_service as create_u2_auth_service
from classic.protocols.auth import (
    ERROR_DUPL,
    ERROR_LOGN,
    ERROR_PASS,
    ClassicActiveSessionRegistry,
    ClassicAuthContext,
)
from classic.protocols.frame import ClassicEAFrame
from classic.protocols.password import (
    decode_password_token,
    make_password_token,
    password_candidates,
    storage_password_candidate,
)


class ClassicPasswordCodecTests(unittest.TestCase):
    def test_u2_token_round_trip_and_candidate_expansion(self) -> None:
        token = make_password_token("secret", "Public Key")
        self.assertTrue(token.startswith("$"))
        self.assertEqual(decode_password_token(token, "Public Key"), "secret")
        candidates = password_candidates(
            {"PASS": token, "PSES": "Public Key"},
            token,
        )
        self.assertEqual(candidates[0], token)
        self.assertIn("secret", candidates)
        self.assertEqual(storage_password_candidate(candidates), "secret")

    def test_wrong_mask_does_not_decode_to_the_original_password(self) -> None:
        token = make_password_token("secret", "Public Key")
        self.assertNotEqual(decode_password_token(token, "wrong-mask"), "secret")


class ClassicFrameTests(unittest.TestCase):
    def test_signed_frame_matches_u2_md5_trailer_contract(self) -> None:
        frame = ClassicEAFrame.signed("auth", b"NAME=Driver\n\x00", 130)
        encoded = frame.encode()
        self.assertEqual(len(encoded), 142)
        self.assertEqual(encoded[:4], b"auth")
        self.assertEqual(int.from_bytes(encoded[8:12], "big"), 142)
        self.assertEqual(encoded[-8:], md5(encoded[:-8]).digest()[:8])
        decoded, remainder = ClassicEAFrame.decode_one(encoded)
        self.assertEqual(remainder, b"")
        self.assertEqual(decoded.fields()["NAME"], "Driver")


class SharedCredentialExtensionsTests(unittest.TestCase):
    def test_aliases_and_personas_persist_and_can_authenticate(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "accounts.json"
            store = CredentialStore(path, salt_factory=lambda size: b"\x11" * size)
            store.create_account(
                "AccountName",
                "secret",
                persona="Primary",
                email="driver@example.invalid",
                aliases=("Driver",),
                personas=("Primary", "Second"),
            )
            self.assertTrue(store.authenticate("driver@example.invalid", "secret").accepted)
            self.assertTrue(store.authenticate("Driver", "secret").accepted)
            account = CredentialStore(path).resolve_account("driver")
            self.assertIsNotNone(account)
            assert account is not None
            self.assertEqual(account.all_personas, ("Primary", "Second"))
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["version"], 3)
            self.assertEqual(
                persisted["accounts"]["AccountName"]["aliases"],
                ["Driver"],
            )

    def test_multiple_wire_candidates_consume_only_one_failure(self) -> None:
        now = [100.0]
        store = CredentialStore(
            failure_limit=2,
            lockout_seconds=60,
            clock=lambda: now[0],
            salt_factory=lambda size: b"\x12" * size,
        )
        store.create_account("Driver", "correct")
        first = store.authenticate_candidates("Driver", ("wire", "decoded-wrong", "classic"))
        self.assertEqual(first.reason, "bad_password")
        second = store.authenticate_candidates("Driver", ("wire2", "decoded-wrong2"))
        self.assertEqual(second.reason, "locked")


class MostWantedFirstLoginRegistrationTests(unittest.TestCase):
    def test_unknown_account_is_created_and_logged_in_when_enabled(self) -> None:
        credentials = CredentialStore(salt_factory=lambda size: b"\x20" * size)
        service = create_mw_auth_service(
            credentials,
            IdentityStore(token_factory=lambda: "mw-first-login-token."),
            auto_enroll=True,
        )
        context = ClassicAuthContext(
            connection_id="mw-first-login",
            client_ip="127.0.0.1",
            session_challenge="session-mw",
            mask="Public Key",
        )

        reply = service.login(
            {
                "NAME": "NewMWDriver",
                "PASS": make_password_token("new-secret", "Public Key"),
                "PSES": "Public Key",
            },
            context,
        )

        self.assertTrue(reply.accepted)
        self.assertEqual(reply.reason, "enrolled")
        self.assertEqual(context.persona, "NewMWDriver")
        self.assertTrue(credentials.authenticate("NewMWDriver", "new-secret").accepted)
        frame, remainder = ClassicEAFrame.decode_one(reply.frames[0])
        self.assertEqual(remainder, b"")
        self.assertEqual(frame.command, "auth")

    def test_unknown_account_still_returns_imst_when_disabled(self) -> None:
        credentials = CredentialStore()
        service = create_mw_auth_service(
            credentials,
            IdentityStore(token_factory=lambda: "unused."),
            auto_enroll=False,
        )
        context = ClassicAuthContext(
            connection_id="mw-no-first-login",
            session_challenge="session-mw",
            mask="Public Key",
        )
        reply = service.login(
            {
                "NAME": "Missing",
                "PASS": make_password_token("secret", "Public Key"),
                "PSES": "Public Key",
            },
            context,
        )
        self.assertFalse(reply.accepted)
        self.assertEqual(reply.reason, "unknown_account")

    def test_open_login_rejects_disabled_and_banned_accounts(self) -> None:
        for policy, reason in (
            ("disabled", "disabled"),
            ("banned", "banned"),
            ("blocked_email", "banned"),
        ):
            with self.subTest(policy=policy):
                credentials = CredentialStore(salt_factory=lambda size: b"\x22" * size)
                credentials.create_account(
                    "PolicyAccount",
                    "secret",
                    persona="PolicyDriver",
                    email=(
                        "blocked@example.test"
                        if policy == "blocked_email"
                        else ""
                    ),
                )
                if policy == "disabled":
                    credentials.set_enabled("PolicyAccount", False)
                elif policy == "banned":
                    credentials.set_banned("PolicyAccount", True)
                else:
                    credentials.set_email_blocked("blocked@example.test", True)
                service = create_mw_auth_service(
                    credentials,
                    IdentityStore(token_factory=lambda: "unused."),
                    verify_passwords=False,
                )
                context = ClassicAuthContext(connection_id=f"mw-open-{policy}")

                reply = service.login({"NAME": "PolicyAccount"}, context)

                self.assertFalse(reply.accepted)
                self.assertEqual(reply.reason, reason)
                self.assertIsNone(context.identity)
                self.assertEqual(context.persona, "")



class Underground2AuthServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.credentials = CredentialStore(salt_factory=lambda size: b"\x21" * size)
        self.credentials.create_account(
            "Acct",
            "correct",
            persona="Race",
            email="d@x.io",
            aliases=("Driver",),
            personas=("Race", "Alt"),
        )
        self.identities = IdentityStore(token_factory=lambda: "classic-session-token.")
        self.active = ClassicActiveSessionRegistry()
        self.service = create_u2_auth_service(
            self.credentials,
            self.identities,
            active_sessions=self.active,
        )

    def _context(self, connection_id: str) -> ClassicAuthContext:
        return ClassicAuthContext(
            connection_id=connection_id,
            client_ip="127.0.0.1",
            session_challenge="session-517",
            mask="Public Key",
        )

    def test_u2_login_accepts_obfuscated_password_and_emits_captured_shape(self) -> None:
        context = self._context("conn-1")
        reply = self.service.login(
            {
                "EMAIL": "d@x.io",
                "PASS": make_password_token("correct", "Public Key"),
                "PSES": "Public Key",
            },
            context,
        )
        self.assertTrue(reply.accepted)
        self.assertFalse(reply.close_connection)
        frame, remainder = ClassicEAFrame.decode_one(reply.frames[0])
        self.assertEqual(remainder, b"")
        self.assertEqual(frame.command, "auth")
        self.assertEqual(frame.reserved, 0)
        self.assertEqual(len(frame.payload), 130)
        fields = frame.fields()
        self.assertEqual(fields["MAIL"], "d@x.io")
        self.assertEqual(fields["NAME"], "Acct")
        self.assertEqual(fields["PERSONAS"], "Race,Alt")
        self.assertEqual(context.persona, "Race")
        self.assertEqual(len(context.lkey), 32)

    def test_login_rejects_unrepresentable_primary_persona_instead_of_truncating_it(self) -> None:
        long_persona = "P" * 200
        self.credentials.create_account(
            "LongAccount",
            "secret",
            persona=long_persona,
            email="",
        )
        context = self._context("conn-long-wire")
        reply = self.service.login(
            {
                "NAME": "LongAccount",
                "PASS": make_password_token("secret", "Public Key"),
                "PSES": "Public Key",
            },
            context,
        )
        self.assertFalse(reply.accepted)
        self.assertEqual(reply.reason, "wire_identity_too_long")
        self.assertIsNone(context.identity)
        self.assertEqual(context.persona, "")

    def test_account_without_email_emits_empty_mail_instead_of_placeholder(self) -> None:
        self.credentials.create_account(
            "NoMail",
            "secret",
            persona="NoMailDriver",
            email="",
        )
        context = self._context("conn-no-mail")
        reply = self.service.login(
            {
                "NAME": "NoMail",
                "PASS": make_password_token("secret", "Public Key"),
                "PSES": "Public Key",
            },
            context,
        )
        self.assertTrue(reply.accepted)
        frame, _ = ClassicEAFrame.decode_one(reply.frames[0])
        self.assertEqual(frame.fields()["MAIL"], "")
        self.assertEqual(frame.fields()["PERSONAS"], "NoMailDriver")

    def test_dispatch_routes_a_wire_auth_frame(self) -> None:
        context = self._context("conn-dispatch")
        request = ClassicEAFrame.from_fields(
            "auth",
            {
                "EMAIL": "d@x.io",
                "PASS": make_password_token("correct", "Public Key"),
                "PSES": "Public Key",
            },
        )
        reply = self.service.dispatch(request, context)
        self.assertTrue(reply.accepted)
        response, _ = ClassicEAFrame.decode_one(reply.frames[0])
        self.assertEqual(response.command, "auth")

    def test_wrong_password_maps_to_pass_ascii_error(self) -> None:
        context = self._context("conn-wrong")
        reply = self.service.login(
            {
                "EMAIL": "d@x.io",
                "PASS": make_password_token("wrong", "Public Key"),
                "PSES": "Public Key",
            },
            context,
        )
        self.assertFalse(reply.accepted)
        self.assertFalse(reply.close_connection)
        frame, _ = ClassicEAFrame.decode_one(reply.frames[0])
        self.assertEqual(frame.command, "auth")
        self.assertEqual(frame.reserved, ERROR_PASS)

    def test_duplicate_account_maps_to_logn_ascii_error(self) -> None:
        first = self._context("conn-first")
        second = self._context("conn-second")
        fields = {
            "EMAIL": "d@x.io",
            "PASS": make_password_token("correct", "Public Key"),
            "PSES": "Public Key",
        }
        self.assertTrue(self.service.login(fields, first).accepted)
        reply = self.service.login(fields, second)
        self.assertFalse(reply.accepted)
        frame, _ = ClassicEAFrame.decode_one(reply.frames[0])
        self.assertEqual(frame.reserved, ERROR_LOGN)

    def test_account_creation_decodes_token_before_pbkdf2_storage(self) -> None:
        context = self._context("conn-create")
        reply = self.service.create_account(
            {
                "EMAIL": "new@example.invalid",
                "NAME": "NewDriver",
                "PERS": "NewPersona",
                "PASS": make_password_token("new-secret", "Public Key"),
                "PSES": "Public Key",
            },
            context,
        )
        self.assertTrue(reply.accepted)
        frame, _ = ClassicEAFrame.decode_one(reply.frames[0])
        self.assertEqual(frame.command, "acct")
        self.assertEqual(frame.reserved, 0)
        self.assertTrue(self.credentials.authenticate("NewDriver", "new-secret").accepted)
        account = self.credentials.resolve_account("new@example.invalid")
        self.assertIsNotNone(account)
        assert account is not None
        self.assertEqual(account.persona, "NewPersona")

    def test_duplicate_account_creation_maps_to_dupl(self) -> None:
        context = self._context("conn-create-duplicate")
        reply = self.service.create_account(
            {
                "EMAIL": "d@x.io",
                "PASS": make_password_token("wrong", "Public Key"),
                "PSES": "Public Key",
            },
            context,
        )
        self.assertFalse(reply.accepted)
        frame, _ = ClassicEAFrame.decode_one(reply.frames[0])
        self.assertEqual(frame.reserved, ERROR_DUPL)

    def test_persona_create_select_and_delete_share_the_account_store(self) -> None:
        context = self._context("conn-persona")
        login = self.service.login(
            {
                "EMAIL": "d@x.io",
                "PASS": make_password_token("correct", "Public Key"),
                "PSES": "Public Key",
            },
            context,
        )
        self.assertTrue(login.accepted)
        selected = self.service.select_persona("cper", {"PERS": "ThirdPersona"}, context)
        self.assertTrue(selected.accepted)
        selected_frame, _ = ClassicEAFrame.decode_one(selected.frames[0])
        self.assertEqual(selected_frame.command, "cper")
        selected_fields = selected_frame.fields()
        self.assertEqual(selected_fields["PERS"], "ThirdPersona")
        self.assertNotIn("NA", selected_fields)
        self.assertIn(
            "ThirdPersona",
            self.credentials.resolve_account("Acct").all_personas,  # type: ignore[union-attr]
        )
        deleted = self.service.delete_persona({"PERS": "Alt"}, context)
        self.assertTrue(deleted.accepted)
        deleted_frame, _ = ClassicEAFrame.decode_one(deleted.frames[0])
        self.assertEqual(deleted_frame.fields()["RESULT"], "0")
        self.assertNotIn(
            "Alt",
            self.credentials.resolve_account("Acct").all_personas,  # type: ignore[union-attr]
        )

    def test_select_persona_rejects_unrepresentable_name_before_persisting_it(self) -> None:
        context = self._context("conn-long-persona")
        logged_in = self.service.login(
            {
                "EMAIL": "d@x.io",
                "PASS": make_password_token("correct", "Public Key"),
                "PSES": "Public Key",
            },
            context,
        )
        self.assertTrue(logged_in.accepted)
        original = context.persona
        requested = "Q" * 200
        reply = self.service.select_persona("cper", {"PERS": requested}, context)
        self.assertFalse(reply.accepted)
        self.assertEqual(reply.reason, "wire_identity_too_long")
        self.assertEqual(context.persona, original)
        account = self.credentials.resolve_account("Acct")
        self.assertNotIn(requested, account.all_personas)

    def test_created_personas_are_returned_by_auth_after_relogin(self) -> None:
        self.credentials.add_persona("Acct", "ThirdPersona")
        context = self._context("conn-relogin-personas")
        reply = self.service.login(
            {
                "EMAIL": "d@x.io",
                "PASS": make_password_token("correct", "Public Key"),
                "PSES": "Public Key",
            },
            context,
        )
        self.assertTrue(reply.accepted)
        frame, _ = ClassicEAFrame.decode_one(reply.frames[0])
        self.assertEqual(frame.fields()["PERSONAS"], "Race,Alt,ThirdPersona")

    def test_mw_adapter_reuses_the_same_u2_derived_auth_service(self) -> None:
        mw = create_mw_auth_service(
            self.credentials,
            self.identities,
            active_sessions=ClassicActiveSessionRegistry(),
        )
        context = self._context("mw-conn")
        reply = mw.login(
            {
                "USER": "Driver",
                "PASS": make_password_token("correct", "Public Key"),
                "MASK": "Public Key",
            },
            context,
        )
        self.assertTrue(reply.accepted)
        self.assertEqual(mw.profile.game_id, "most_wanted")


if __name__ == "__main__":
    unittest.main()
