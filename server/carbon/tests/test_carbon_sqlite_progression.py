"""Migration fidelity, cross-connection updates and atomic race persistence."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import sqlite3
import unittest

from carbon.accounts.identity import IdentityStore
from carbon.progression import CarbonProgressionStore, EA_MODERATOR_ROLE


class SQLiteProgressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "progression.sqlite3"
        identities = IdentityStore()
        self.a, _ = identities.login("Driver", "Driver")
        self.b, _ = identities.login("Other", "Other")

    def store(self, **kwargs):
        store = CarbonProgressionStore(self.path, **kwargs)
        self.addCleanup(store.close)
        return store

    def race(self, store, key="race-1"):
        return store.record_authoritative_race(
            (self.a, self.b), event_type=3,
            rankings={self.a.profile_id: 1, self.b.profile_id: 2},
            finished_profile_ids=(self.a.profile_id, self.b.profile_id),
            ranked=True, race_key=key,
        )

    def test_import_preserves_all_values_and_never_reimports(self):
        legacy = CarbonProgressionStore(self.path.with_suffix(".json"))
        legacy.bind_identity(self.a)
        legacy.set_role("Driver", EA_MODERATOR_ROLE)
        legacy.set_stat("Driver", "Skill_Level", 1012.25)
        legacy.apply_rank_update(self.a.profile_id, "Evt_Bst_Test", 92.5,
                                 update_type=0, trusted_server=False, text="car,lap,speed")
        original = legacy.path.read_bytes()
        migrated = self.store()
        self.assertEqual(migrated._sqlite.read(), json.loads(original))
        self.assertEqual(legacy.path.read_bytes(), original)
        migrated.set_stat("Driver", "Skill_Level", 1024)
        self.assertEqual(self.store().stat_for_profile(self.a.profile_id, "Skill_Level"), 1024)
        self.assertEqual(self.store(read_only=True).stat_text_for_profile(self.a.profile_id, "Evt_Bst_Test"), "car,lap,speed")

    def test_atomic_race_replay_after_reload_and_conflicting_key(self):
        store = self.store()
        first = self.race(store)
        before = store.leaderboard_snapshot()
        other = self.store()
        self.assertEqual(self.race(other), first)
        self.assertEqual(other.leaderboard_snapshot(), before)
        with self.assertRaises(ValueError):
            other.record_authoritative_race((self.a, self.b), event_type=3,
                rankings={self.a.profile_id: 2, self.b.profile_id: 1},
                finished_profile_ids=(self.a.profile_id,), ranked=True, race_key="race-1")
        self.assertEqual(other.leaderboard_snapshot(), before)
        self.assertEqual(other._sqlite.connection.execute("SELECT count(*) FROM carbon_progression_races").fetchone()[0], 1)

    def test_failure_rolls_back_awards_scores_and_history(self):
        store = self.store()
        store.bind_identity(self.a)
        store.set_stat("Driver", "Virus2", 1)
        before = store.leaderboard_snapshot()
        with patch.object(store._sqlite, "write", side_effect=sqlite3.OperationalError("disk failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.race(store)
        self.assertEqual(self.store().leaderboard_snapshot(), before)
        self.assertEqual(store._sqlite.connection.execute("SELECT count(*) FROM carbon_progression_races").fetchone()[0], 0)
        self.race(store)
        self.assertEqual(store.stat_for_profile(self.b.profile_id, "Virus2"), 1)

    def test_concurrent_updates_do_not_overwrite_each_other(self):
        first, second = self.store(), self.store()
        first.bind_identity(self.a)
        def increment(store):
            for _ in range(15):
                store.apply_rank_update(self.a.profile_id, "Counter", 1, update_type=3, trusted_server=False)
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(increment, (first, second)))
        self.assertEqual(first.stat_for_profile(self.a.profile_id, "Counter"), 30)
        self.assertEqual(second.stat_for_profile(self.a.profile_id, "Counter"), 30)

    def test_protected_stats_and_read_only_connection(self):
        store = self.store()
        store.bind_identity(self.a)
        self.assertFalse(store.apply_rank_update(self.a.profile_id, "Skill_Level", 3000, update_type=0, trusted_server=False))
        read = self.store(read_only=True)
        self.assertEqual(read.recorded_stats_for_profile(self.a.profile_id), {})
        with self.assertRaises(sqlite3.OperationalError):
            read.set_stat("Driver", "Skill_Level", 42)
        self.assertEqual(store.stat_for_profile(self.a.profile_id, "Skill_Level"), 1000)

    def test_duplicate_profile_import_fails_without_partial_data(self):
        legacy = {"accounts": {
            "A": {"persona": "A", "profile_id": 1},
            "B": {"persona": "B", "profile_id": 1},
        }}
        self.path.with_suffix(".json").write_text(json.dumps(legacy))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM carbon_progression_accounts").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM carbon_progression_meta").fetchone()[0], 0)


# Run the existing wire contract tests against the persistent backend too.
import test_carbon_progression as progression_contracts


class SQLiteRankingServiceTests(progression_contracts.CarbonRankingServiceTests):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = CarbonProgressionStore(Path(temporary.name) / "progression.sqlite3")
        self.addCleanup(store.close)
        with patch.object(progression_contracts, "CarbonProgressionStore", return_value=store):
            super().setUp()
