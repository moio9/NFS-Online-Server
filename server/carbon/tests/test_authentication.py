"""Credential persistence and Carbon FESL password-login tests."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from carbon.accounts.credentials import CredentialStore
from carbon.accounts.identity import IdentityStore
from carbon.fesl.frame import FESLFrame
from carbon.fesl.service import (
    CarbonEndpoints,
    CarbonFESLService,
    FESLConnection,
)


class CredentialStoreTests(unittest.TestCase):
    def test_password_hash_round_trip_uses_pbkdf2(self) -> None:
        encoded = CredentialStore.encode_password(
            "carbon-secret",
            salt=b"\x01" * 16,
            iterations=1_000,
        )
        self.assertTrue(encoded.startswith("pbkdf2_sha256$1000$"))
        self.assertTrue(CredentialStore.verify_password("carbon-secret", encoded))
        self.assertFalse(CredentialStore.verify_password("wrong", encoded))

    def test_account_is_persisted_without_plaintext_password(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "auth.json"
            store = CredentialStore(path, salt_factory=lambda size: b"\x02" * size)
            store.create_account("Driver", "secret", persona="CarbonDriver")
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn('"password": "secret"', raw)
            self.assertIn("password_pbkdf2", raw)

            reloaded = CredentialStore(path)
            result = reloaded.authenticate("driver", "secret")
            self.assertTrue(result.accepted)
            self.assertEqual(result.account_name, "Driver")
            self.assertEqual(result.persona, "CarbonDriver")

    def test_old_plaintext_entry_is_migrated_on_load(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "auth.json"
            path.write_text(
                json.dumps(
                    {
                        "accounts": {
                            "Driver": {
                                "persona": "Driver",
                                "password": "old-secret",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            store = CredentialStore(path)
            self.assertTrue(store.authenticate("Driver", "old-secret").accepted)
            migrated = path.read_text(encoding="utf-8")
            self.assertNotIn('"password"', migrated)
            self.assertIn("password_pbkdf2", migrated)

    def test_auto_enroll_creates_first_account(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "auth.json"
            store = CredentialStore(path, auto_enroll=True)
            first = store.authenticate("NewDriver", "secret")
            self.assertTrue(first.accepted)
            self.assertEqual(first.reason, "enrolled")
            self.assertTrue(CredentialStore(path).authenticate("NewDriver", "secret").accepted)

    def test_repeated_failures_temporarily_lock_account(self) -> None:
        now = [100.0]
        store = CredentialStore(
            failure_limit=2,
            lockout_seconds=10,
            clock=lambda: now[0],
            salt_factory=lambda size: b"\x03" * size,
        )
        store.create_account("Driver", "correct")
        self.assertEqual(store.authenticate("Driver", "wrong").reason, "bad_password")
        locked = store.authenticate("Driver", "wrong")
        self.assertEqual(locked.reason, "locked")
        self.assertGreaterEqual(locked.retry_after_seconds, 10)
        self.assertEqual(store.authenticate("Driver", "correct").reason, "locked")
        now[0] = 111.0
        self.assertTrue(store.authenticate("Driver", "correct").accepted)

    def test_disabled_account_is_rejected(self) -> None:
        store = CredentialStore(salt_factory=lambda size: b"\x04" * size)
        store.create_account("Driver", "secret")
        store.set_enabled("Driver", False)
        self.assertEqual(store.authenticate("Driver", "secret").reason, "disabled")


class CarbonPasswordLoginTests(unittest.TestCase):
    def _service(self, store: CredentialStore) -> CarbonFESLService:
        return CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
            IdentityStore(token_factory=lambda: "auth-session."),
            credentials=store,
            authentication_mode="password",
        )

    def test_valid_password_creates_normal_carbon_session(self) -> None:
        store = CredentialStore(salt_factory=lambda size: b"\x05" * size)
        store.create_account("AccountName", "secret", persona="RacePersona")
        service = self._service(store)
        connection = FESLConnection()

        reply = service.dispatch(
            FESLFrame.from_fields(
                "acct",
                {"TXN": "Login", "name": "accountname", "password": "secret"},
                transaction=8,
            ),
            connection,
        )[0]
        self.assertEqual(reply.transaction, 8)
        self.assertEqual(reply.fields["lkey"], "auth-session.")
        self.assertEqual(reply.fields["displayName"], "RacePersona")
        self.assertIsNotNone(connection.identity)
        self.assertEqual(connection.identity.account_name, "AccountName")
        self.assertEqual(connection.identity.persona, "RacePersona")

    def test_unknown_account_uses_confirmed_fesl_error_101(self) -> None:
        service = self._service(CredentialStore())
        connection = FESLConnection()
        reply = service.dispatch(
            FESLFrame.from_fields(
                "acct",
                {"TXN": "Login", "name": "Unknown", "password": "secret"},
                transaction=9,
            ),
            connection,
        )[0]
        self.assertEqual(reply.fields["TXN"], "Login")
        self.assertEqual(reply.fields["errorCode"], "101")
        self.assertNotIn("lkey", reply.fields)
        self.assertIsNone(connection.identity)

    def test_bad_password_uses_structured_fesl_error_122(self) -> None:
        store = CredentialStore(salt_factory=lambda size: b"\x06" * size)
        store.create_account("Driver", "correct")
        service = self._service(store)
        connection = FESLConnection()
        reply = service.dispatch(
            FESLFrame.from_fields(
                "acct",
                {"TXN": "Login", "name": "Driver", "password": "wrong"},
                transaction=10,
            ),
            connection,
        )[0]
        self.assertEqual(reply.fields["errorCode"], "122")
        self.assertEqual(reply.fields["errorContainer.[]"], "1")
        self.assertEqual(reply.fields["errorContainer.0.fieldName"], "password")
        self.assertEqual(reply.fields["errorContainer.0.fieldError"], "122")
        self.assertIsNone(connection.identity)

    def test_disabled_account_uses_confirmed_fesl_error_102(self) -> None:
        store = CredentialStore(salt_factory=lambda size: b"\x07" * size)
        store.create_account("Driver", "secret")
        store.set_enabled("Driver", False)
        service = self._service(store)
        connection = FESLConnection()

        reply = service.dispatch(
            FESLFrame.from_fields(
                "acct",
                {"TXN": "Login", "name": "Driver", "password": "secret"},
                transaction=13,
            ),
            connection,
        )[0]

        self.assertEqual(reply.fields["errorCode"], "102")
        self.assertNotIn("errorContainer.0.fieldName", reply.fields)
        self.assertIsNone(connection.identity)

    def test_register_game_accepts_cdkey_without_persisting_it(self) -> None:
        store = CredentialStore(salt_factory=lambda size: b"\x08" * size)
        service = self._service(store)
        connection = FESLConnection()
        secret_code = "AAAA-BBBB-CCCC-DDDD"

        reply = service.dispatch(
            FESLFrame.from_fields(
                "acct",
                {
                    "TXN": "RegisterGame",
                    "code": secret_code,
                    "game": "NFS-2007",
                    "name": "NewDriver",
                    "password": "secret",
                    "platform": "PC",
                },
                transaction=0x80000003,
            ),
            connection,
        )[0]

        self.assertEqual(reply.transaction, 0x80000003)
        self.assertEqual(reply.fields, {"TXN": "RegisterGame"})
        self.assertEqual(connection.registered_game, "NFS-2007")
        self.assertEqual(connection.registered_platform, "PC")
        self.assertEqual(store.accounts(), ())
        self.assertNotIn(secret_code, repr(connection))

    def test_register_game_is_a_compatibility_gate_even_with_blank_code(self) -> None:
        service = self._service(CredentialStore())
        reply = service.dispatch(
            FESLFrame.from_fields(
                "acct",
                {
                    "TXN": "RegisterGame",
                    "code": "",
                    "game": "NFS-2007",
                    "platform": "PC",
                },
                transaction=0x80000003,
            ),
            FESLConnection(),
        )[0]
        self.assertEqual(reply.fields, {"TXN": "RegisterGame"})

    def test_add_account_persists_hashed_credentials_then_allows_login(self) -> None:
        with TemporaryDirectory() as temporary:
            credential_path = Path(temporary) / "auth.json"
            store = CredentialStore(
                credential_path,
                salt_factory=lambda size: b"\x08" * size,
            )
            service = self._service(store)
            connection = FESLConnection()

            created = service.dispatch(
                FESLFrame.from_fields(
                    "acct",
                    {
                        "TXN": "AddAccount",
                        "name": "NewDriver",
                        "password": "secret",
                        "email": "driver@example.invalid",
                        "DOBDay": "1",
                        "DOBMonth": "1",
                        "DOBYear": "1990",
                        "countryCode": "RO",
                        "zipCode": "400001",
                        "eaMailFlag": "1",
                        "thirdPartyMailFlag": "0",
                    },
                    transaction=0x80000004,
                ),
                connection,
            )[0]

            self.assertEqual(created.transaction, 0x80000004)
            self.assertEqual(created.fields, {"TXN": "NuAddAccount"})
            self.assertIsNone(connection.identity)
            self.assertNotIn("secret", credential_path.read_text(encoding="utf-8"))
            registered = CredentialStore(credential_path).accounts()[0]
            self.assertEqual(registered.email, "driver@example.invalid")
            self.assertEqual(
                (registered.dob_day, registered.dob_month, registered.dob_year),
                ("1", "1", "1990"),
            )
            self.assertEqual(registered.country_code, "RO")
            self.assertEqual(registered.zip_code, "400001")
            self.assertEqual(registered.ea_mail_flag, "1")
            self.assertEqual(registered.third_party_mail_flag, "0")

            login = service.dispatch(
                FESLFrame.from_fields(
                    "acct",
                    {
                        "TXN": "Login",
                        "name": "NewDriver",
                        "password": "secret",
                    },
                    transaction=0x80000005,
                ),
                connection,
            )[0]
            self.assertEqual(login.fields["displayName"], "NewDriver")
            self.assertIsNotNone(connection.identity)

            store.set_password("NewDriver", "changed")
            store.set_enabled("NewDriver", False)
            preserved = store.accounts()[0]
            self.assertEqual(preserved.email, "driver@example.invalid")
            self.assertEqual(preserved.country_code, "RO")
            self.assertEqual(preserved.zip_code, "400001")

    def test_add_account_rejects_duplicate_name_with_error_160(self) -> None:
        store = CredentialStore(salt_factory=lambda size: b"\x09" * size)
        store.create_account("Driver", "first")
        service = self._service(store)

        reply = service.dispatch(
            FESLFrame.from_fields(
                "acct",
                {
                    "TXN": "AddAccount",
                    "name": "driver",
                    "password": "second",
                },
                transaction=0x80000004,
            ),
            FESLConnection(),
        )[0]

        self.assertEqual(reply.fields["TXN"], "NuAddAccount")
        self.assertEqual(reply.fields["errorCode"], "160")
        self.assertEqual(reply.fields["errorContainer.[]"], "0")
        self.assertTrue(store.authenticate("Driver", "first").accepted)

    def test_suggest_screen_names_returns_available_alternatives(self) -> None:
        store = CredentialStore(salt_factory=lambda size: b"\x0a" * size)
        store.create_account("Driver", "secret")
        store.create_account("Driver1", "secret")
        service = self._service(store)

        reply = service.dispatch(
            FESLFrame.from_fields(
                "acct",
                {
                    "TXN": "SuggestScreenNames",
                    "name": "Driver",
                    "keywords.[]": "2",
                    "keywords.0": "Race",
                    "keywords.1": "Carbon",
                    "maxSuggestions": "4",
                },
                transaction=0x80000006,
            ),
            FESLConnection(),
        )[0]

        self.assertEqual(reply.transaction, 0x80000006)
        self.assertEqual(reply.fields["TXN"], "SuggestScreenNames")
        self.assertEqual(reply.fields["names.[]"], "4")
        suggestions = [reply.fields[f"names.{index}"] for index in range(4)]
        self.assertEqual(len(set(item.casefold() for item in suggestions)), 4)
        self.assertNotIn("driver", {item.casefold() for item in suggestions})
        self.assertNotIn("driver1", {item.casefold() for item in suggestions})

    def test_existing_account_with_same_password_completes_only_missing_metadata(self) -> None:
        store = CredentialStore(salt_factory=lambda size: b"\x0b" * size)
        store.create_account(
            "Driver",
            "secret",
            country_code="RO",
        )
        original_hash = store.accounts()[0].password_hash
        service = self._service(store)

        reply = service.dispatch(
            FESLFrame.from_fields(
                "acct",
                {
                    "TXN": "AddAccount",
                    "name": "Driver",
                    "password": "secret",
                    "email": "driver@example.invalid",
                    "DOBDay": "4",
                    "DOBMonth": "8",
                    "DOBYear": "2000",
                    "countryCode": "GB",
                    "zipCode": "500000",
                    "eaMailFlag": "1",
                    "thirdPartyMailFlag": "0",
                },
                transaction=0x80000004,
            ),
            FESLConnection(),
        )[0]

        self.assertEqual(reply.fields, {"TXN": "NuAddAccount"})
        account = store.accounts()[0]
        self.assertEqual(account.password_hash, original_hash)
        self.assertEqual(account.country_code, "RO")
        self.assertEqual(account.email, "driver@example.invalid")
        self.assertEqual(
            (account.dob_day, account.dob_month, account.dob_year),
            ("4", "8", "2000"),
        )
        self.assertEqual(account.zip_code, "500000")
        self.assertEqual(account.ea_mail_flag, "1")
        self.assertEqual(account.third_party_mail_flag, "0")

    def test_existing_account_with_wrong_password_still_returns_taken_error(self) -> None:
        store = CredentialStore(salt_factory=lambda size: b"\x0c" * size)
        store.create_account("Driver", "secret")
        service = self._service(store)

        reply = service.dispatch(
            FESLFrame.from_fields(
                "acct",
                {
                    "TXN": "AddAccount",
                    "name": "Driver",
                    "password": "wrong",
                    "countryCode": "RO",
                },
                transaction=0x80000004,
            ),
            FESLConnection(),
        )[0]

        self.assertEqual(reply.fields["errorCode"], "160")
        self.assertEqual(store.accounts()[0].country_code, "")

    def test_login_error_probe_returns_only_the_requested_numeric_code(self) -> None:
        service = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
            IdentityStore(token_factory=lambda: "unused-session."),
            login_error_probe_code=101,
            authentication_mode="open",
        )
        connection = FESLConnection()

        reply = service.dispatch(
            FESLFrame.from_fields(
                "acct",
                {"TXN": "Login", "name": "Driver", "password": "anything"},
                transaction=12,
            ),
            connection,
        )[0]

        self.assertEqual(
            reply.fields,
            {
                "TXN": "Login",
                "errorCode": "101",
                "errorContainer.[]": "0",
            },
        )
        self.assertIsNone(connection.identity)

    def test_service_defaults_to_password_mode(self) -> None:
        service = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
            IdentityStore(token_factory=lambda: "unused-session."),
        )
        self.assertEqual(service.authentication_mode, "password")
        connection = FESLConnection()
        reply = service.dispatch(
            FESLFrame.from_fields(
                "acct",
                {"TXN": "Login", "name": "Unknown", "password": "ignored"},
                transaction=13,
            ),
            connection,
        )[0]
        self.assertEqual(reply.fields["errorCode"], "101")
        self.assertIsNone(connection.identity)

    def test_open_mode_remains_backward_compatible(self) -> None:
        service = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
            IdentityStore(token_factory=lambda: "open-session."),
            authentication_mode="open",
        )
        connection = FESLConnection()
        reply = service.dispatch(
            FESLFrame.from_fields(
                "acct",
                {"TXN": "Login", "name": "AnyDriver", "password": "ignored"},
                transaction=11,
            ),
            connection,
        )[0]
        self.assertEqual(reply.fields["lkey"], "open-session.")
        self.assertEqual(reply.fields["displayName"], "AnyDriver")


if __name__ == "__main__":
    unittest.main()
