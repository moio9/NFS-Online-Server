from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from classic.admin.stats import main as stats_main
from classic.ea.ranking import ClassicRankingStore
from classic.ea.sqlite_ranking import SQLiteClassicRankingStore
from common.accounts import SQLiteAccountDatabase


class StatisticsAdminCommandTests(unittest.TestCase):
    def test_shared_account_database_is_the_authoritative_admin_store(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_path = root / "accounts.sqlite3"
            files_path = root / "users"
            database = SQLiteAccountDatabase(database_path, files_path)
            database.create_account("Account", "password", persona="Driver")
            config_path = root / "classic.cfg"
            config_path.write_text(
                f"ACCOUNT_DB={database_path}\n"
                f"ACCOUNT_FILES={files_path}\n"
                "ACCOUNT_SQLITE_BUSY_TIMEOUT_MS=5000\n",
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    stats_main(
                        [
                            "--config",
                            str(config_path),
                            "set",
                            "Driver",
                            "circuit",
                            "--wins",
                            "4",
                            "--skill",
                            "900",
                        ]
                    ),
                    0,
                )

            row = SQLiteClassicRankingStore(database).summary(
                "most_wanted",
                "Driver",
                1,
            )
            self.assertEqual(row["wins"], 4)
            self.assertEqual(row["rep"], 900)
            self.assertFalse((root / "stats.json").exists())

    def test_unrelated_server_validation_does_not_block_stats_tool(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stats_path = root / "stats.json"
            config_path = root / "classic.cfg"
            config_path.write_text(
                f"STATS_DATA={stats_path}\n"
                "CARBON_MESSENGER_IPC_LISTEN=127.0.0.1:13506\n"
                "CARBON_MESSENGER_IPC_SECRET=\n",
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    stats_main(
                        [
                            "--config",
                            str(config_path),
                            "set",
                            "Driver",
                            "circuit",
                            "--wins",
                            "10",
                        ]
                    ),
                    0,
                )

            row = ClassicRankingStore(stats_path).summary(
                "most_wanted", "Driver", 1
            )
            self.assertEqual(row["wins"], 10)

    def test_set_add_show_and_reset_use_mw_by_default(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stats_path = root / "stats.json"
            config_path = root / "classic.cfg"
            config_path.write_text(f"STATS_DATA={stats_path}\n", encoding="utf-8")

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    stats_main(
                        [
                            "--config",
                            str(config_path),
                            "set",
                            "Driver",
                            "circuit",
                            "--wins",
                            "10",
                            "--losses",
                            "2",
                            "--skill",
                            "1500",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    stats_main(
                        [
                            "--config",
                            str(config_path),
                            "add",
                            "Driver",
                            "circuit",
                            "--wins",
                            "1",
                            "--skill",
                            "100",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    stats_main(
                        [
                            "--config",
                            str(config_path),
                            "show",
                            "Driver",
                            "circuit",
                        ]
                    ),
                    0,
                )

            row = ClassicRankingStore(stats_path).summary(
                "most_wanted", "Driver", 1
            )
            self.assertEqual(row["wins"], 11)
            self.assertEqual(row["losses"], 2)
            self.assertEqual(row["rep"], 1600)
            self.assertIn("category=circuit", output.getvalue())
            self.assertIn("skill=1600", output.getvalue())

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    stats_main(
                        [
                            "--config",
                            str(config_path),
                            "reset",
                            "Driver",
                            "circuit",
                        ]
                    ),
                    0,
                )
            reset = ClassicRankingStore(stats_path).summary(
                "most_wanted", "Driver", 1
            )
            self.assertEqual(reset["wins"], 0)
            self.assertEqual(reset["losses"], 0)
            self.assertEqual(reset["rep"], 100)

    def test_u2_streetx_and_url_are_distinct_categories(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stats_path = root / "stats.json"
            config_path = root / "classic.cfg"
            config_path.write_text(f"STATS_DATA={stats_path}\n", encoding="utf-8")

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    stats_main(
                        [
                            "--config",
                            str(config_path),
                            "--game",
                            "u2",
                            "set",
                            "Driver",
                            "streetx",
                            "--wins",
                            "3",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    stats_main(
                        [
                            "--config",
                            str(config_path),
                            "--game",
                            "u2",
                            "set",
                            "Driver",
                            "url",
                            "--wins",
                            "7",
                        ]
                    ),
                    0,
                )

            store = ClassicRankingStore(stats_path)
            self.assertEqual(store.summary("underground2", "Driver", 4)["wins"], 3)
            self.assertEqual(store.summary("underground2", "Driver", 5)["wins"], 7)


if __name__ == "__main__":
    unittest.main()
