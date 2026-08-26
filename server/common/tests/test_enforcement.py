from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from common.accounts import SCHEMA_VERSION, SQLiteAccountDatabase
from common.enforcement import (
    AccountEnforcementError,
    AccountPolicyEvent,
    AccountPolicyMonitor,
    LiveAccountConnection,
    LiveAccountConnectionRegistry,
    PolicyCloseGate,
    SQLiteAccountPolicyEventStore,
)


class AccountPolicyDatabaseTests(unittest.TestCase):
    def make_database(self, root: Path) -> SQLiteAccountDatabase:
        return SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")

    def test_policy_changes_and_session_revocation_commit_one_event(self) -> None:
        with TemporaryDirectory() as temporary:
            database = self.make_database(Path(temporary))
            record = database.create_account("alice", "pw", persona="Alice")
            identity = database.identity("alice", "Alice")
            with database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO active_sessions(
                        account_id, persona_id, game, connection_id, session_token,
                        server_id, connected_at, heartbeat_at, expires_at
                    ) VALUES(?, ?, 'carbon', 'connection', 'token', 'server', 1, 1, 9999999999)
                    """,
                    (identity.account_id, identity.persona_id),
                )

            database.set_banned("alice", True)
            database.set_banned("alice", True)  # idempotent; no duplicate event
            with database.connect() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                events = connection.execute(
                    """
                    SELECT account_id, account_name, action
                      FROM account_policy_events
                     ORDER BY event_id
                    """
                ).fetchall()
                session_row = connection.execute(
                    "SELECT COUNT(*) FROM active_sessions"
                ).fetchone()
                sessions = int(session_row[0])
            self.assertEqual(version, SCHEMA_VERSION)
            self.assertEqual(
                [
                    (
                        int(row["account_id"]),
                        str(row["account_name"]),
                        str(row["action"]),
                    )
                    for row in events
                ],
                [(record.account_id, "alice", "ban")],
            )
            self.assertEqual(sessions, 0)

            database.set_banned("alice", False)
            database.set_enabled("alice", False)
            database.set_enabled("alice", True)
            store = SQLiteAccountPolicyEventStore(database)
            actions = [event.action for event in store.events_after(0)]
            self.assertEqual(actions, ["ban", "unban", "disable", "enable"])

    def test_kick_revokes_session_without_banning_or_disabling_account(self) -> None:
        with TemporaryDirectory() as temporary:
            database = self.make_database(Path(temporary))
            record = database.create_account("alice", "pw", persona="Alice")
            identity = database.identity("alice", "Alice")
            with database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO active_sessions(
                        account_id, persona_id, game, connection_id, session_token,
                        server_id, connected_at, heartbeat_at, expires_at
                    ) VALUES(?, ?, 'carbon', 'connection', 'token', 'server', 1, 1, 9999999999)
                    """,
                    (identity.account_id, identity.persona_id),
                )

            kicked = database.kick("Alice")
            self.assertEqual(kicked.account_id, record.account_id)
            self.assertTrue(kicked.enabled)
            self.assertFalse(kicked.banned)

            store = SQLiteAccountPolicyEventStore(database)
            events = store.events_after(0)
            self.assertEqual([event.action for event in events], ["kick"])
            self.assertTrue(events[0].restrictive)
            self.assertEqual(events[0].disconnect_reason, "account-kicked")
            with database.connect() as connection:
                count = int(connection.execute("SELECT COUNT(*) FROM active_sessions").fetchone()[0])
            self.assertEqual(count, 0)

    def test_schema_three_policy_table_is_migrated_for_kick(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = self.make_database(root)
            record = database.create_account("alice", "pw", persona="Alice")
            database.set_banned("alice", True)
            with database.transaction() as connection:
                connection.execute("DROP INDEX account_policy_events_account_idx")
                connection.execute("ALTER TABLE account_policy_events RENAME TO policy_v4")
                connection.execute(
                    """
                    CREATE TABLE account_policy_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_id INTEGER NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                        account_name TEXT NOT NULL,
                        action TEXT NOT NULL CHECK(action IN ('ban', 'unban', 'disable', 'enable')),
                        created_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO account_policy_events(event_id, account_id, account_name, action, created_at)
                    SELECT event_id, account_id, account_name, action, created_at FROM policy_v4
                    """
                )
                connection.execute("DROP TABLE policy_v4")
                connection.execute(
                    "CREATE INDEX account_policy_events_account_idx ON account_policy_events(account_id, event_id)"
                )
                connection.execute("PRAGMA user_version=3")

            upgraded = self.make_database(root)
            upgraded.set_banned("alice", False)
            upgraded.kick("alice")
            with upgraded.connect() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                actions = [str(row[0]) for row in connection.execute(
                    "SELECT action FROM account_policy_events ORDER BY event_id"
                ).fetchall()]
                sql = str(connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='account_policy_events'"
                ).fetchone()[0])
            self.assertEqual(version, SCHEMA_VERSION)
            self.assertEqual(actions, ["ban", "unban", "kick"])
            self.assertIn("'kick'", sql)
            self.assertEqual(upgraded.resolve_account(record.account_name).account_id, record.account_id)

    def test_identities_for_account_returns_only_owned_personas(self) -> None:
        with TemporaryDirectory() as temporary:
            database = self.make_database(Path(temporary))
            alice = database.create_account(
                "alice", "pw", persona="Alice", personas=("AliceTwo",)
            )
            database.create_account("bob", "pw", persona="Bob")
            identities = database.identities_for_account(alice.account_id)
            self.assertEqual([identity.persona for identity in identities], ["Alice", "AliceTwo"])
            self.assertTrue(
                all(identity.account_id == alice.account_id for identity in identities)
            )

    def test_monitor_starts_at_current_tail_and_retries_failed_event(self) -> None:
        with TemporaryDirectory() as temporary:
            database = self.make_database(Path(temporary))
            database.create_account("alice", "pw", persona="Alice")
            database.set_banned("alice", True)
            store = SQLiteAccountPolicyEventStore(database)
            delivered: list[str] = []
            fail_once = {"value": True}

            def handler(event: AccountPolicyEvent) -> None:
                if event.action == "unban" and fail_once["value"]:
                    fail_once["value"] = False
                    raise RuntimeError("retry")
                delivered.append(event.action)

            monitor = AccountPolicyMonitor(store, handler, name="test-monitor")
            monitor.start()
            try:
                self.assertEqual(monitor.cursor, 1)
                database.set_banned("alice", False)
                with self.assertLogs("common.enforcement", level="ERROR"):
                    self.assertEqual(monitor.poll_once(), 0)
                self.assertEqual(monitor.cursor, 1)
                self.assertEqual(monitor.poll_once(), 1)
                self.assertEqual(delivered, ["unban"])
            finally:
                monitor.stop()
            monitor.start()
            self.assertEqual(monitor.cursor, 2)
            monitor.stop()


class PolicyCloseGateTests(unittest.TestCase):
    def test_gate_is_idempotent_and_never_extends_its_deadline(self) -> None:
        now = [10.0]
        gate = PolicyCloseGate(grace_seconds=2.0, clock=lambda: now[0])

        self.assertFalse(gate.active)
        self.assertTrue(gate.request("account-banned"))
        self.assertTrue(gate.active)
        self.assertEqual(gate.reason, "account-banned")
        self.assertAlmostEqual(gate.remaining() or 0.0, 2.0)

        now[0] = 11.0
        self.assertFalse(gate.request("later-reason", grace_seconds=5.0))
        self.assertAlmostEqual(gate.remaining() or 0.0, 1.0)
        self.assertEqual(gate.reason, "account-banned")

        self.assertFalse(gate.request("shorter", grace_seconds=0.25))
        self.assertAlmostEqual(gate.remaining() or 0.0, 0.25)
        now[0] = 11.25
        self.assertTrue(gate.expired())
        self.assertEqual(gate.remaining(), 0.0)

    def test_force_expires_active_gate_without_replacing_reason(self) -> None:
        now = [5.0]
        gate = PolicyCloseGate(clock=lambda: now[0])
        gate.request("account-disabled")
        self.assertFalse(gate.force("send-error"))
        self.assertTrue(gate.expired())
        self.assertEqual(gate.reason, "account-disabled")

    def test_negative_grace_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be negative"):
            PolicyCloseGate(grace_seconds=-0.1)
        gate = PolicyCloseGate()
        with self.assertRaisesRegex(ValueError, "must not be negative"):
            gate.request("account-banned", grace_seconds=-0.1)


class LiveAccountConnectionRegistryTests(unittest.TestCase):
    def test_restrictive_policy_begins_close_before_cleanup_and_close_request(self) -> None:
        registry = LiveAccountConnectionRegistry(name="test")
        calls: list[str] = []
        def begin_close(_event: AccountPolicyEvent) -> bool:
            calls.append("begin-close")
            return True

        registry.register(
            LiveAccountConnection(
                connection_id="carbon:1",
                protocol="carbon-fesl",
                account_name=lambda: "Alice",
                begin_close=begin_close,
                request_close=lambda reason: calls.append(f"request-close:{reason}"),
            )
        )
        registry.register(
            LiveAccountConnection(
                connection_id="other:1",
                protocol="classic",
                account_name=lambda: "Bob",
                request_close=lambda reason: calls.append(f"wrong:{reason}"),
            )
        )

        result = registry.enforce(
            AccountPolicyEvent(1, 1, "alice", "ban", 1.0),
            before_close=lambda: calls.append("cleanup"),
        )
        self.assertEqual(
            calls,
            ["begin-close", "cleanup", "request-close:account-banned"],
        )
        self.assertEqual(result.matched, 1)
        self.assertEqual(result.notified, 1)
        self.assertEqual(result.closing, 1)
        self.assertEqual(result.protocols, ("carbon-fesl",))

        self.assertEqual(
            registry.enforce(AccountPolicyEvent(2, 1, "alice", "unban", 2.0)).matched,
            0,
        )

    def test_begin_close_failure_still_runs_cleanup_and_close_request(self) -> None:
        registry = LiveAccountConnectionRegistry(name="test")
        calls: list[str] = []

        def fail_begin_close(_event: AccountPolicyEvent) -> bool:
            calls.append("begin-close")
            raise RuntimeError("notification failed")

        registry.register(
            LiveAccountConnection(
                connection_id="carbon:1",
                protocol="carbon-fesl",
                account_name=lambda: "Alice",
                begin_close=fail_begin_close,
                request_close=lambda reason: calls.append(f"request-close:{reason}"),
            )
        )

        with self.assertLogs("common.enforcement", level="ERROR"):
            with self.assertRaises(AccountEnforcementError) as raised:
                registry.enforce(
                    AccountPolicyEvent(1, 1, "alice", "ban", 1.0),
                    before_close=lambda: calls.append("cleanup"),
                )

        self.assertEqual(
            calls,
            ["begin-close", "cleanup", "request-close:account-banned"],
        )
        self.assertEqual(raised.exception.failures[0].phase, "begin-close")

    def test_close_request_failure_is_reported_after_all_connections_run(self) -> None:
        registry = LiveAccountConnectionRegistry(name="test")
        calls: list[str] = []

        def fail_close(reason: str) -> None:
            calls.append(f"failed-close:{reason}")
            raise RuntimeError("close failed")

        def begin_close(_event: AccountPolicyEvent) -> bool:
            calls.append("begin-close")
            return True

        registry.register(
            LiveAccountConnection(
                connection_id="carbon:1",
                protocol="carbon-fesl",
                account_name=lambda: "Alice",
                begin_close=begin_close,
                request_close=fail_close,
            )
        )
        registry.register(
            LiveAccountConnection(
                connection_id="carbon:2",
                protocol="carbon-theater",
                account_name=lambda: "Alice",
                request_close=lambda reason: calls.append(f"closed-second:{reason}"),
            )
        )

        with self.assertLogs("common.enforcement", level="ERROR"):
            with self.assertRaises(AccountEnforcementError) as raised:
                registry.enforce(
                    AccountPolicyEvent(1, 1, "alice", "ban", 1.0),
                    before_close=lambda: calls.append("cleanup"),
                )

        self.assertEqual(
            calls,
            [
                "begin-close",
                "cleanup",
                "failed-close:account-banned",
                "closed-second:account-banned",
            ],
        )
        self.assertEqual(raised.exception.failures[0].phase, "request-close")


    def test_cleanup_failure_still_requests_close_for_matching_connections(self) -> None:
        registry = LiveAccountConnectionRegistry(name="test")
        calls: list[str] = []
        def begin_close(_event: AccountPolicyEvent) -> bool:
            calls.append("begin-close")
            return True

        registry.register(
            LiveAccountConnection(
                connection_id="classic:1",
                protocol="classic-lobby",
                account_name=lambda: "Alice",
                begin_close=begin_close,
                request_close=lambda reason: calls.append(f"request-close:{reason}"),
            )
        )

        def fail_cleanup() -> None:
            calls.append("cleanup")
            raise RuntimeError("cleanup failed")

        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            registry.enforce(
                AccountPolicyEvent(1, 1, "alice", "disable", 1.0),
                before_close=fail_cleanup,
            )

        self.assertEqual(
            calls,
            ["begin-close", "cleanup", "request-close:account-disabled"],
        )


if __name__ == "__main__":
    unittest.main()
