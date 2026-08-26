from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from common.accounts import SQLiteAccountDatabase, SQLiteSessionRegistry
from classic.accounts.sqlite_backend import SQLiteCredentialStore, SQLiteIdentityStore
from classic.games.most_wanted.auth import create_auth_service as create_mw_auth_service
from classic.games.underground2.auth import create_auth_service as create_u2_auth_service
from classic.protocols.auth import ClassicAuthContext
from classic.protocols.frame import ClassicEAFrame
from classic.protocols.password import make_password_token


class SharedSQLiteAccountTests(unittest.TestCase):
    def test_u2_account_creation_can_authenticate_on_the_same_connection(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            credentials = SQLiteCredentialStore(database)
            identities = SQLiteIdentityStore(database, token_factory=lambda: "u2-key.")
            sessions = SQLiteSessionRegistry(
                database,
                game="underground2",
                server_id="u2-test",
                lease_seconds=30,
            )
            service = create_u2_auth_service(
                credentials,
                identities,
                active_sessions=sessions,
            )
            context = ClassicAuthContext(
                connection_id="u2-create-then-auth",
                client_ip="127.0.0.1",
                session_challenge="u2-session",
                mask="Public Key",
            )
            password = make_password_token("secret", "Public Key")

            created = service.dispatch(
                ClassicEAFrame.from_fields(
                    "acct",
                    {
                        "EMAIL": "fresh-u2@example.test",
                        "NAME": "FreshU2",
                        "PERS": "FreshU2",
                        "PASS": password,
                        "PSES": "Public Key",
                    },
                ),
                context,
            )
            self.assertTrue(created.accepted)
            self.assertEqual(created.reason, "created")
            self.assertIsNone(context.identity)
            self.assertIsNone(sessions.session_for("fresh-u2@example.test"))

            authenticated = service.dispatch(
                ClassicEAFrame.from_fields(
                    "auth",
                    {
                        "EMAIL": "fresh-u2@example.test",
                        "PASS": password,
                        "PSES": "Public Key",
                    },
                ),
                context,
            )
            self.assertTrue(authenticated.accepted)
            self.assertFalse(authenticated.close_connection)
            self.assertIsNotNone(context.identity)
            session = sessions.session_for("fresh-u2@example.test")
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.connection_id, "u2-create-then-auth")

    def test_mw_first_login_registration_persists_in_shared_sqlite(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            credentials = SQLiteCredentialStore(database)
            identities = SQLiteIdentityStore(database, token_factory=lambda: "mw-key.")
            service = create_mw_auth_service(
                credentials,
                identities,
                auto_enroll=True,
            )
            context = ClassicAuthContext(
                connection_id="mw-create",
                client_ip="127.0.0.1",
                session_challenge="mw-session",
                mask="Public Key",
            )
            reply = service.login(
                {
                    "NAME": "FreshMW",
                    "PASS": make_password_token("secret", "Public Key"),
                    "PSES": "Public Key",
                },
                context,
            )
            self.assertTrue(reply.accepted)
            self.assertEqual(reply.reason, "enrolled")
            self.assertTrue(credentials.authenticate("FreshMW", "secret").accepted)
            credentials.add_persona("FreshMW", "AlternateMW")
            deleted = service.delete_persona({"PERS": "FreshMW"}, context)
            self.assertTrue(deleted.accepted)
            self.assertEqual(context.persona, "AlternateMW")
            self.assertIsNotNone(context.identity)
            self.assertEqual(context.identity.persona, "AlternateMW")
            self.assertEqual(
                context.identity.persona_id,
                database.identity_for_persona("AlternateMW").persona_id,
            )
            reopened = SQLiteCredentialStore(
                SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            )
            self.assertTrue(reopened.authenticate("FreshMW", "secret").accepted)

    def test_account_personas_aliases_ids_and_directories_are_persistent(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(
                root / "accounts.sqlite3",
                root / "users",
                salt_factory=lambda size: b"\x42" * size,
            )
            credentials = SQLiteCredentialStore(database)
            created = credentials.create_account(
                "SharedAccount",
                "secret",
                persona="Driver",
                email="driver@example.test",
                aliases=("ClassicLogin",),
                personas=("Driver", "SecondDriver"),
            )
            self.assertEqual(created.all_personas, ("Driver", "SecondDriver"))
            self.assertEqual(
                credentials.authenticate("ClassicLogin", "secret").persona,
                "Driver",
            )
            self.assertEqual(
                credentials.authenticate("driver@example.test", "secret").account_name,
                "SharedAccount",
            )

            first_identity = SQLiteIdentityStore(database, token_factory=lambda: "token-1").login(
                "SharedAccount",
                "Driver",
            )[0]
            second_database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            second_identity = SQLiteIdentityStore(
                second_database,
                token_factory=lambda: "token-2",
            ).login("SharedAccount", "Driver")[0]
            self.assertEqual(first_identity.profile_id, second_identity.profile_id)

            record = second_database.resolve_account("SharedAccount")
            self.assertIsNotNone(record)
            account_root = second_database.account_directory(record.account_uuid)
            self.assertTrue((account_root / "common").is_dir())
            persona = second_database.identity("SharedAccount", "Driver")
            self.assertTrue(
                (second_database.persona_directory(persona, "carbon") / "shadows").is_dir()
            )

    def test_single_session_is_atomic_across_games_and_expires(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [1_000.0]
            database = SQLiteAccountDatabase(
                root / "accounts.sqlite3",
                root / "users",
                clock=lambda: now[0],
            )
            database.create_account(
                "Account",
                "secret",
                persona="Driver",
                email="driver@example.test",
            )
            u2 = SQLiteSessionRegistry(
                database,
                game="underground2",
                server_id="u2-test",
                lease_seconds=30,
                clock=lambda: now[0],
            )
            carbon = SQLiteSessionRegistry(
                database,
                game="carbon",
                server_id="carbon-test",
                lease_seconds=30,
                clock=lambda: now[0],
            )
            self.assertIsNone(u2.claim("u2-connection", "Account", "Driver"))
            self.assertEqual(
                carbon.claim("carbon-connection", "Account", "Driver"),
                "account_in_use",
            )
            self.assertEqual(u2.session_for("Account").game, "underground2")

            now[0] += 31
            self.assertIsNone(carbon.claim("carbon-connection", "Account", "Driver"))
            self.assertEqual(carbon.session_for("Account").game, "carbon")
            carbon.release("carbon-connection")
            self.assertIsNone(carbon.session_for("Account"))

            database.set_banned("Account", True)
            self.assertEqual(
                carbon.claim("banned-connection", "Account", "Driver"),
                "banned",
            )
            database.set_banned("Account", False)
            self.assertIsNone(
                carbon.claim("disabled-connection", "Account", "Driver")
            )
            database.set_enabled("Account", False)
            self.assertFalse(carbon.touch("disabled-connection"))
            self.assertIsNone(carbon.session_for("Account"))
            database.set_enabled("Account", True)
            database.set_email_blocked("driver@example.test", True)
            self.assertEqual(
                carbon.claim("blocked-connection", "Account", "Driver"),
                "banned",
            )


if __name__ == "__main__":
    unittest.main()
