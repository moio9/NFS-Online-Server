from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from common.accounts import SQLiteAccountDatabase, SQLiteSessionRegistry
from carbon.accounts.sqlite_backend import SQLiteCredentialStore, SQLiteIdentityStore
from carbon.fesl.blob import CarbonBlob
from carbon.fesl.frame import FESLFrame
from carbon.fesl.service import CarbonEndpoints, CarbonFESLService, FESLConnection
from carbon.fesl.sqlite_blob import SQLiteCarbonBlobStore


class SharedSQLiteCarbonTests(unittest.TestCase):
    def test_register_game_does_not_create_or_store_cdkey_before_add_account(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            credentials = SQLiteCredentialStore(database)
            service = CarbonFESLService(
                CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
                SQLiteIdentityStore(database, token_factory=lambda: "carbon-key."),
                credentials=credentials,
                authentication_mode="password",
            )
            connection = FESLConnection(connection_id="carbon-register")
            registered = service.dispatch(
                FESLFrame.from_fields(
                    "acct",
                    {
                        "TXN": "RegisterGame",
                        "code": "PRIVATE-CD-KEY",
                        "game": "NFS-2007",
                        "name": "FreshCarbon",
                        "password": "secret",
                        "platform": "PC",
                    },
                    transaction=0x80000003,
                ),
                connection,
            )[0]
            self.assertEqual(registered.fields, {"TXN": "RegisterGame"})
            self.assertIsNone(database.resolve_account("FreshCarbon"))

            created = service.dispatch(
                FESLFrame.from_fields(
                    "acct",
                    {
                        "TXN": "AddAccount",
                        "name": "FreshCarbon",
                        "password": "secret",
                        "email": "fresh@example.invalid",
                        "countryCode": "RO",
                    },
                    transaction=0x80000004,
                ),
                connection,
            )[0]
            self.assertEqual(created.fields, {"TXN": "NuAddAccount"})
            self.assertTrue(credentials.authenticate("FreshCarbon", "secret").accepted)
            with database.connect() as sql:
                columns = {row[1] for row in sql.execute("PRAGMA table_info(accounts)")}
            self.assertTrue({"cdkey", "cd_key", "product_key", "registration_code"}.isdisjoint({name.casefold() for name in columns}))

    def test_carbon_add_account_completes_classic_account_metadata_without_rehashing(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account(
                "Driver",
                "secret",
                persona="RaceDriver",
                country_code="RO",
            )
            before = database.resolve_account("Driver")
            service = CarbonFESLService(
                CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
                SQLiteIdentityStore(database, token_factory=lambda: "carbon-key."),
                credentials=SQLiteCredentialStore(database),
                authentication_mode="password",
            )

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
                    },
                    transaction=0x80000004,
                ),
                FESLConnection(connection_id="carbon-complete"),
            )[0]

            self.assertEqual(reply.fields, {"TXN": "NuAddAccount"})
            after = database.resolve_account("Driver")
            self.assertEqual(after.password_hash, before.password_hash)
            self.assertEqual(after.primary_persona, "RaceDriver")
            self.assertEqual(after.country_code, "RO")
            self.assertEqual(after.email, "driver@example.invalid")
            self.assertEqual(
                (after.dob_day, after.dob_month, after.dob_year),
                ("4", "8", "2000"),
            )
            self.assertEqual(after.zip_code, "500000")

    def test_sqlite_screen_name_suggestions_exclude_personas_and_aliases(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account(
                "Driver",
                "secret",
                persona="Driver1",
                aliases=("Driver2",),
            )
            credentials = SQLiteCredentialStore(database)
            self.assertFalse(credentials.screen_name_available("driver"))
            self.assertFalse(credentials.screen_name_available("driver1"))
            self.assertFalse(credentials.screen_name_available("driver2"))
            self.assertTrue(credentials.screen_name_available("driver3"))

    def test_carbon_duplicate_login_routes_new_client_to_native_dupl_notice(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            credentials = SQLiteCredentialStore(database)
            credentials.create_account("Driver", "secret", persona="RaceDriver")
            tokens = iter(("carbon-old-key.", "unused-duplicate-key."))
            identities = SQLiteIdentityStore(database, token_factory=lambda: next(tokens))
            sessions = SQLiteSessionRegistry(
                database,
                game="carbon",
                server_id="carbon-test",
                lease_seconds=120,
            )
            service = CarbonFESLService(
                CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
                identities,
                credentials=credentials,
                authentication_mode="password",
                active_sessions=sessions,
            )
            first = FESLConnection(connection_id="carbon-1")
            reply = service.dispatch(
                FESLFrame.from_fields(
                    "acct",
                    {"TXN": "Login", "name": "Driver", "password": "secret"},
                    transaction=1,
                ),
                first,
            )[0]
            self.assertEqual(reply.fields["displayName"], "RaceDriver")

            second = FESLConnection(connection_id="carbon-2")
            duplicate = service.dispatch(
                FESLFrame.from_fields(
                    "acct",
                    {"TXN": "Login", "name": "Driver", "password": "secret"},
                    transaction=2,
                ),
                second,
            )[0]
            self.assertNotIn("errorCode", duplicate.fields)
            self.assertEqual(duplicate.fields["lkey"], "unused-duplicate-key.")
            self.assertEqual(duplicate.fields["displayName"], "RaceDriver")
            self.assertEqual(second.identity, first.identity)
            self.assertEqual(
                identities.resolve_session("carbon-old-key."),
                first.identity,
            )
            self.assertIsNone(identities.resolve_session("unused-duplicate-key."))
            forced_logoffs = identities.forced_logoffs()
            self.assertEqual(len(forced_logoffs), 1)
            self.assertEqual(forced_logoffs[0][0], "unused-duplicate-key.")
            self.assertEqual(forced_logoffs[0][1], first.identity)
            self.assertEqual(forced_logoffs[0][2], "DUPL")
            self.assertEqual(
                sessions.session_for("Driver").connection_id,
                "carbon-1",
            )

            service.disconnect(second)
            self.assertEqual(
                sessions.session_for("Driver").connection_id,
                "carbon-1",
            )

    def test_banned_account_uses_native_fesl_error_103(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account("Driver", "secret", persona="RaceDriver")
            database.set_banned("Driver", True)
            service = CarbonFESLService(
                CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
                SQLiteIdentityStore(database, token_factory=lambda: "carbon-key."),
                credentials=SQLiteCredentialStore(database),
                authentication_mode="password",
            )
            connection = FESLConnection(connection_id="carbon-banned")

            rejected = service.dispatch(
                FESLFrame.from_fields(
                    "acct",
                    {"TXN": "Login", "name": "Driver", "password": "secret"},
                    transaction=4,
                ),
                connection,
            )[0]

            self.assertEqual(rejected.fields["errorCode"], "103")
            self.assertEqual(
                rejected.fields["localizedMessage"],
                '"This account has been banned. Contact Customer Support."',
            )
            self.assertIsNone(connection.identity)

    def test_fesl_blob_round_trip_uses_shared_filesystem_store(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            credentials = SQLiteCredentialStore(database)
            credentials.create_account("Driver", "secret", persona="RaceDriver")
            identities = SQLiteIdentityStore(database, token_factory=lambda: "carbon-key.")
            service = CarbonFESLService(
                CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
                identities,
                credentials=credentials,
                authentication_mode="password",
                blobs=SQLiteCarbonBlobStore(database),
            )
            connection = FESLConnection(connection_id="carbon-blob")
            service.dispatch(
                FESLFrame.from_fields(
                    "acct",
                    {"TXN": "Login", "name": "Driver", "password": "secret"},
                    transaction=1,
                ),
                connection,
            )
            add_reply = service.dispatch(
                FESLFrame.from_fields(
                    "blob",
                    {
                        "TXN": "AddBlob",
                        "ownerId": str(connection.identity.profile_id),
                        "ownerType": "1",
                        "type": "11",
                        "name": "GHOST_SHARED_0",
                        "content": "R0hPU1Q=",
                    },
                    transaction=0xB0000041,
                ),
                connection,
            )[0]
            blob_id = int(add_reply.fields["blobId"])
            content_reply = service.dispatch(
                FESLFrame.from_fields(
                    "blob",
                    {"TXN": "GetBlobContent", "blobId": str(blob_id)},
                    transaction=0x80000042,
                ),
                connection,
            )[0]
            self.assertEqual(content_reply.fields["content"], "R0hPU1Q=")
            identity = database.identity("Driver", "RaceDriver")
            files = list(
                (database.persona_directory(identity, "carbon") / "shadows").glob("*.bin")
            )
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), b"GHOST")

    def test_blob_payload_is_stored_under_account_folder_and_survives_restart(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account("Driver", "secret", persona="RaceDriver")
            identity = database.identity("Driver", "RaceDriver", require_carbon_wire_id=True)
            first = SQLiteCarbonBlobStore(database)
            blob = first.add(
                CarbonBlob(
                    blob_id=0,
                    owner_id=identity.profile_id,
                    owner_type=1,
                    blob_type=11,
                    creator="RaceDriver",
                    name="GHOST_A44B31B9_0",
                    content="R0hPU1Q=",
                )
            )
            self.assertGreater(blob.blob_id, 0)

            shadow_dir = database.persona_directory(identity, "carbon") / "shadows"
            files = list(shadow_dir.glob("*.bin"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), b"GHOST")

            second = SQLiteCarbonBlobStore(
                SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            )
            loaded = second.get(blob.blob_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.content, "R0hPU1Q=")
            self.assertEqual(loaded.name, "GHOST_A44B31B9_0")
            self.assertTrue(second.remove_owned(blob.blob_id, identity.profile_id))
            self.assertFalse(files[0].exists())


    def test_blob_reconcile_removes_metadata_when_payload_is_missing(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account("Driver", "secret", persona="RaceDriver")
            identity = database.identity("Driver", "RaceDriver", require_carbon_wire_id=True)
            store = SQLiteCarbonBlobStore(database)
            blob = store.add(
                CarbonBlob(
                    blob_id=0,
                    owner_id=identity.profile_id,
                    owner_type=1,
                    blob_type=11,
                    creator="RaceDriver",
                    name="GHOST_MISSING_0",
                    content="R0hPU1Q=",
                )
            )
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT relative_path FROM assets WHERE game='carbon' AND wire_id=?",
                    (blob.blob_id,),
                ).fetchone()
            payload_path = database.user_root / str(row["relative_path"])
            payload_path.unlink()

            restarted = SQLiteCarbonBlobStore(database)
            self.assertIsNone(restarted.get(blob.blob_id))
            with database.connect() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM assets WHERE game='carbon' AND wire_id=?",
                    (blob.blob_id,),
                ).fetchone()[0]
            self.assertEqual(count, 0)

    def test_blob_reconcile_repairs_size_and_checksum_after_crash_residue(self) -> None:
        import hashlib

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account("Driver", "secret", persona="RaceDriver")
            identity = database.identity("Driver", "RaceDriver", require_carbon_wire_id=True)
            store = SQLiteCarbonBlobStore(database)
            blob = store.add(
                CarbonBlob(
                    blob_id=0,
                    owner_id=identity.profile_id,
                    owner_type=1,
                    blob_type=11,
                    creator="RaceDriver",
                    name="GHOST_REPAIR_0",
                    content="R0hPU1Q=",
                )
            )
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT relative_path FROM assets WHERE game='carbon' AND wire_id=?",
                    (blob.blob_id,),
                ).fetchone()
            payload_path = database.user_root / str(row["relative_path"])
            payload_path.write_bytes(b"CHANGED")

            restarted = SQLiteCarbonBlobStore(database)
            with database.connect() as connection:
                repaired = connection.execute(
                    "SELECT byte_size, sha256 FROM assets WHERE game='carbon' AND wire_id=?",
                    (blob.blob_id,),
                ).fetchone()
            self.assertEqual(int(repaired["byte_size"]), len(b"CHANGED"))
            self.assertEqual(
                repaired["sha256"],
                hashlib.sha256(b"CHANGED").hexdigest(),
            )
            self.assertEqual(restarted.get(blob.blob_id).content, "Q0hBTkdFRA==")

    def test_blob_reconcile_quarantines_unindexed_payload_instead_of_deleting_it(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account("Driver", "secret", persona="RaceDriver")
            identity = database.identity("Driver", "RaceDriver", require_carbon_wire_id=True)
            blob_directory = database.persona_directory(identity, "carbon") / "blobs"
            blob_directory.mkdir(parents=True, exist_ok=True)
            orphan = blob_directory / "orphan.bin"
            orphan.write_bytes(b"ORPHAN")

            SQLiteCarbonBlobStore(database)

            self.assertFalse(orphan.exists())
            quarantined = list(
                (root / "backups" / "orphaned-carbon-assets").glob("orphan-*.bin")
            )
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_bytes(), b"ORPHAN")


if __name__ == "__main__":
    unittest.main()
