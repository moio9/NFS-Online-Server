from __future__ import annotations

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from common.accounts import SCHEMA_VERSION, SQLiteAccountDatabase


class SQLiteAliasCredentialTests(unittest.TestCase):
    def test_alias_uses_the_current_account_password(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account(
                "current-login",
                "current-password",
                persona="Driver",
                aliases=("legacy-login",),
            )

            self.assertTrue(
                database.authenticate("current-login", "current-password").accepted
            )
            self.assertTrue(
                database.authenticate("legacy-login", "current-password").accepted
            )
            self.assertFalse(
                database.authenticate("legacy-login", "legacy-password").accepted
            )

            database.set_password("current-login", "replacement-password")
            self.assertFalse(
                database.authenticate("legacy-login", "current-password").accepted
            )
            self.assertTrue(
                database.authenticate("legacy-login", "replacement-password").accepted
            )

    def test_schema_two_is_upgraded_without_obsolete_alias_credentials(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "accounts.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE login_aliases (
                        alias_key TEXT PRIMARY KEY,
                        alias TEXT NOT NULL,
                        account_id INTEGER NOT NULL,
                        created_at REAL NOT NULL
                    );
                    PRAGMA user_version=2;
                    """
                )

            SQLiteAccountDatabase(path, root / "users")
            with sqlite3.connect(path) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])

            self.assertIn("login_aliases", tables)
            self.assertNotIn("login_alias_credentials", tables)
            self.assertEqual(version, SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
