from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from common.accounts import SQLiteAccountDatabase
from common.public_presence import (
    clear_carbon_public_presence,
    set_carbon_public_presence,
    website_appear_offline,
)


class CarbonPublicPresenceTests(unittest.TestCase):
    def test_website_visibility_preference_is_account_wide(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            account = database.create_account("driver", "pw", persona="Driver")
            database.add_persona("driver", "SecondDriver")
            self.assertFalse(website_appear_offline(database, "Driver"))
            with database.transaction() as connection:
                connection.execute(
                    "CREATE TABLE web_account_preferences("
                    "account_id INTEGER PRIMARY KEY, appear_online INTEGER NOT NULL DEFAULT 1)"
                )
                connection.execute(
                    "INSERT INTO web_account_preferences(account_id,appear_online) VALUES(?,0)",
                    (account.account_id,),
                )
            self.assertTrue(website_appear_offline(database, "driver"))
            self.assertTrue(website_appear_offline(database, "SecondDriver"))
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE web_account_preferences SET appear_online=1 WHERE account_id=?",
                    (account.account_id,),
                )
            self.assertFalse(website_appear_offline(database, "Driver"))

    def test_late_disconnect_does_not_clear_replacement_connection(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            set_carbon_public_presence(database, "Driver", "old", "DISC")
            set_carbon_public_presence(database, "Driver", "new", "CHAT")
            clear_carbon_public_presence(database, "Driver", "old")
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT connection_id, show FROM carbon_public_presence WHERE persona='driver'"
                ).fetchone()
            self.assertEqual((row["connection_id"], row["show"]), ("new", "CHAT"))
            clear_carbon_public_presence(database, "Driver", "new")
            with database.connect() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM carbon_public_presence"
                ).fetchone()[0]
            self.assertEqual(count, 0)
