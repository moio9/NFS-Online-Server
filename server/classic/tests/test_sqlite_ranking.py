from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from classic.ea.sqlite_ranking import SQLiteClassicRankingStore
from common.accounts import SCHEMA_VERSION, SQLiteAccountDatabase


class SQLiteClassicRankingStoreTests(unittest.TestCase):
    @staticmethod
    def _database(root: Path) -> SQLiteAccountDatabase:
        return SQLiteAccountDatabase(
            root / "accounts.sqlite3",
            root / "users",
            busy_timeout_ms=5_000,
        )

    @staticmethod
    def _account(
        database: SQLiteAccountDatabase,
        name: str,
        persona: str | None = None,
    ) -> None:
        database.create_account(
            name,
            "password",
            persona=persona or name,
        )

    def test_schema_upgrade_creates_account_linked_statistics(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = self._database(root)
            self._account(database, "Driver")
            persona_id = database.identity_for_persona("Driver").persona_id
            with database.connect() as connection:
                connection.executescript(
                    """
                    DROP TABLE game_race_results;
                    DROP TABLE game_races;
                    DROP TABLE game_leaderboard_visibility;
                    DROP TABLE game_player_stats;
                    DROP TABLE legacy_imports;
                    PRAGMA user_version=1;
                    """
                )
            database = self._database(root)
            with database.connect() as connection:
                connection.executescript(
                    """
                    DROP TABLE game_race_results;
                    CREATE TABLE game_race_results (
                        race_id INTEGER NOT NULL REFERENCES game_races(race_id)
                            ON DELETE CASCADE,
                        persona_id INTEGER NOT NULL REFERENCES personas(persona_id)
                            ON DELETE CASCADE,
                        outcome TEXT NOT NULL,
                        place INTEGER NOT NULL DEFAULT 0,
                        disconnected INTEGER NOT NULL DEFAULT 0,
                        elapsed_ms INTEGER NOT NULL DEFAULT 0,
                        best_lap_ms INTEGER NOT NULL DEFAULT 0,
                        best_drift INTEGER NOT NULL DEFAULT 0,
                        nos_used REAL NOT NULL DEFAULT 0,
                        source TEXT NOT NULL DEFAULT '',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        aggregate_applied INTEGER NOT NULL DEFAULT 0,
                        reported_at REAL NOT NULL,
                        PRIMARY KEY(race_id, persona_id)
                    );
                    PRAGMA user_version=2;
                    """
                )
                race_id = int(
                    connection.execute(
                        """
                        INSERT INTO game_races(
                            game, server_run_id, session_id, category,
                            created_at, updated_at
                        ) VALUES('most_wanted', 'old-v2', 'race-1', 3, 1, 1)
                        """
                    ).lastrowid
                )
                connection.execute(
                    """
                    INSERT INTO game_race_results(
                        race_id, persona_id, outcome, reported_at
                    ) VALUES(?, ?, 'WIN', 1)
                    """,
                    (race_id, persona_id),
                )
            database = self._database(root)
            store = SQLiteClassicRankingStore(database)

            store.get_or_create("most_wanted", "Driver")

            with database.connect() as connection:
                self.assertEqual(
                    int(connection.execute("PRAGMA user_version").fetchone()[0]),
                    SCHEMA_VERSION,
                )
                rows = connection.execute(
                    """
                    SELECT s.category, p.display_name
                      FROM game_player_stats AS s
                      JOIN personas AS p ON p.persona_id=s.persona_id
                     WHERE s.game='most_wanted'
                     ORDER BY s.category
                    """
                ).fetchall()
                result_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(game_race_results)"
                    ).fetchall()
                }
                migrated_result = connection.execute(
                    "SELECT reporter_key, category FROM game_race_results"
                ).fetchone()
            self.assertEqual([int(row["category"]) for row in rows], list(range(5)))
            self.assertEqual({str(row["display_name"]) for row in rows}, {"Driver"})
            self.assertTrue({"reporter_key", "category"} <= result_columns)
            self.assertEqual(str(migrated_result["reporter_key"]), str(persona_id))
            self.assertEqual(int(migrated_result["category"]), 3)

    def test_history_and_aggregate_are_atomic_and_deduplicated(self) -> None:
        with TemporaryDirectory() as temporary:
            database = self._database(Path(temporary))
            self._account(database, "Winner")
            self._account(database, "Loser")
            store = SQLiteClassicRankingStore(database)
            metadata = {"track": 101, "laps": 3, "status": "complete"}

            for _ in range(2):
                store.record_result(
                    "most_wanted",
                    "Winner",
                    category_index=1,
                    outcome="WIN",
                    opponent_personas=("Loser",),
                    nos_used=4.5,
                    race_key="race-7",
                    reporter_key=1001,
                    race_metadata=metadata,
                    result_metadata={"place": 1, "elapsed_ms": 61_500, "source": "mw_resu"},
                )
            store.record_result(
                "most_wanted",
                "Loser",
                category_index=1,
                outcome="LOSS",
                opponent_personas=("Winner",),
                race_key="race-7",
                reporter_key=1002,
                race_metadata=metadata,
                result_metadata={"place": 2, "elapsed_ms": 63_000, "source": "mw_resu"},
            )

            self.assertEqual(store.summary("most_wanted", "Winner", 1)["wins"], 1)
            self.assertEqual(store.summary("most_wanted", "Loser", 1)["losses"], 1)
            self.assertAlmostEqual(
                float(store.summary("most_wanted", "Winner", 1)["nos_used"]),
                4.5,
            )
            store.record_result(
                "most_wanted",
                "Winner",
                category_index=1,
                outcome="WIN",
                race_key="unranked-race",
                reporter_key=1001,
                race_metadata={"ranked": False},
                result_metadata={"place": 1, "source": "mw_resu"},
            )
            self.assertEqual(store.summary("most_wanted", "Winner", 1)["wins"], 1)
            with database.connect() as connection:
                races = connection.execute(
                    "SELECT * FROM game_races ORDER BY race_id"
                ).fetchall()
                results = connection.execute(
                    "SELECT * FROM game_race_results ORDER BY race_id, place"
                ).fetchall()
            self.assertEqual(len(races), 2)
            self.assertEqual(str(races[0]["track"]), "101")
            self.assertEqual(str(races[0]["status"]), "complete")
            self.assertEqual(len(results), 3)
            self.assertEqual(
                [str(row["reporter_key"]) for row in results],
                ["1001", "1002", "1001"],
            )
            self.assertEqual(
                [int(row["aggregate_applied"]) for row in results],
                [1, 1, 0],
            )

    def test_ban_hides_without_deleting_and_blocks_late_result(self) -> None:
        with TemporaryDirectory() as temporary:
            database = self._database(Path(temporary))
            self._account(database, "Cheater")
            self._account(database, "Driver")
            database.create_account(
                "MailAccount",
                "password",
                persona="MailBlocked",
                email="blocked@example.test",
            )
            store = SQLiteClassicRankingStore(database)
            store.update_fields("most_wanted", "Cheater", 1, {"rep": 5000, "wins": 20})
            store.update_fields("most_wanted", "Driver", 1, {"rep": 1000, "wins": 2})
            store.update_fields(
                "most_wanted",
                "MailBlocked",
                1,
                {"rep": 6000, "wins": 30},
            )

            database.set_banned("Cheater", True)
            database.set_email_blocked("blocked@example.test", True)

            board = store.leaderboard("most_wanted", 1, limit=1)
            self.assertEqual([row.persona for row in board], ["Driver"])
            self.assertEqual(store.summary("most_wanted", "Driver", 1)["rank"], 1)
            self.assertEqual(
                store.update_fields(
                    "most_wanted",
                    "Driver",
                    1,
                    {"rep": 1000},
                )["rank"],
                1,
            )
            before = store.summary("most_wanted", "Cheater", 1)
            with self.assertLogs("classic.ea.sqlite_ranking", level="WARNING"):
                store.record_result(
                    "most_wanted",
                    "Cheater",
                    category_index=1,
                    outcome="WIN",
                )
                store.record_result(
                    "most_wanted",
                    "MailBlocked",
                    category_index=1,
                    outcome="WIN",
                )
            after = store.summary("most_wanted", "Cheater", 1)
            self.assertEqual(after["wins"], before["wins"])
            self.assertEqual(after["rep"], before["rep"])
            self.assertEqual(
                store.summary("most_wanted", "MailBlocked", 1)["wins"],
                30,
            )
            with database.connect() as connection:
                self.assertEqual(
                    int(connection.execute("SELECT COUNT(*) FROM game_race_results").fetchone()[0]),
                    0,
                )

    def test_persona_rename_keeps_statistics_by_id(self) -> None:
        with TemporaryDirectory() as temporary:
            database = self._database(Path(temporary))
            self._account(database, "Account", "OldName")
            store = SQLiteClassicRankingStore(database)
            persona_id = database.identity_for_persona("OldName").persona_id
            store.record_result("underground2", "OldName", category_index=3, outcome="WIN")

            with database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE personas
                       SET display_name='NewName', display_name_key='newname'
                     WHERE display_name_key='oldname'
                    """
                )

            self.assertEqual(store.summary("underground2", "NewName", 3)["wins"], 1)
            store.record_result(
                "underground2",
                "OldName",
                category_index=3,
                outcome="WIN",
                persona_id=persona_id,
            )
            self.assertEqual(store.summary("underground2", "NewName", 3)["wins"], 2)
            self.assertEqual(store.personas("underground2"), ("NewName",))
            with self.assertRaises(KeyError):
                store.summary("underground2", "OldName", 3)

    def test_legacy_json_import_is_one_shot_and_transactional(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = self._database(root)
            self._account(database, "Driver")
            legacy = root / "stats.json"
            values = [9999, 0, 0, 0, 100, 101, 101] * 6
            values[7 + 1] = 7
            legacy.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "games": {
                            "underground2": {
                                "personas": {
                                    "driver": {
                                        "persona": "Driver",
                                        "values": values,
                                        "mw_nos_used": [0.0] * 6,
                                    }
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            store = SQLiteClassicRankingStore(database, legacy_path=legacy)
            self.assertEqual(store.summary("underground2", "Driver", 1)["wins"], 7)
            SQLiteClassicRankingStore(database, legacy_path=legacy)
            with database.connect() as connection:
                self.assertEqual(
                    int(connection.execute("SELECT COUNT(*) FROM legacy_imports").fetchone()[0]),
                    1,
                )

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = self._database(root)
            self._account(database, "Known")
            legacy = root / "stats.json"
            legacy.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "games": {
                            "most_wanted": {
                                "personas": {
                                    "ghost": {
                                        "persona": "Ghost",
                                        "values": [9999, 1, 0, 0, 200, 101, 101] * 6,
                                    }
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(KeyError):
                SQLiteClassicRankingStore(database, legacy_path=legacy)
            with database.connect() as connection:
                self.assertEqual(
                    int(connection.execute("SELECT COUNT(*) FROM game_player_stats").fetchone()[0]),
                    0,
                )
                self.assertEqual(
                    int(connection.execute("SELECT COUNT(*) FROM legacy_imports").fetchone()[0]),
                    0,
                )

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = self._database(root)
            self._account(database, "Driver")
            SQLiteClassicRankingStore(database).update_fields(
                "most_wanted",
                "Driver",
                1,
                {"wins": 1},
            )
            legacy = root / "stats.json"
            legacy.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "games": {
                            "most_wanted": {
                                "personas": {
                                    "driver": {
                                        "persona": "Driver",
                                        "values": [9999, 9, 0, 0, 100, 101, 101] * 6,
                                    }
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "populated account database"):
                SQLiteClassicRankingStore(database, legacy_path=legacy)
            self.assertEqual(
                SQLiteClassicRankingStore(database).summary(
                    "most_wanted",
                    "Driver",
                    1,
                )["wins"],
                1,
            )

    def test_concurrent_updates_do_not_lose_results(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialization_errors: list[BaseException] = []

            def initialize() -> None:
                try:
                    self._database(root)
                except BaseException as exc:  # pragma: no cover - assertion reports details
                    initialization_errors.append(exc)

            initializers = [Thread(target=initialize) for _ in range(4)]
            for thread in initializers:
                thread.start()
            for thread in initializers:
                thread.join()
            self.assertEqual(initialization_errors, [])

            database = self._database(root)
            self._account(database, "Driver")
            store = SQLiteClassicRankingStore(database)
            errors: list[BaseException] = []

            def record(index: int) -> None:
                try:
                    store.record_result(
                        "most_wanted",
                        "Driver",
                        category_index=2,
                        outcome="WIN",
                        race_key=f"race-{index}",
                        reporter_key=index,
                    )
                except BaseException as exc:  # pragma: no cover - assertion reports details
                    errors.append(exc)

            threads = [Thread(target=record, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(store.summary("most_wanted", "Driver", 2)["wins"], 12)
            self.assertEqual(store.summary("most_wanted", "Driver", 0)["wins"], 12)


if __name__ == "__main__":
    unittest.main()
