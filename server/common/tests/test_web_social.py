import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest

from common.accounts import SQLiteAccountDatabase
from common.social import SocialService
from common.web_social import WebSocialEventPump, ensure_web_social_schema


class WebSocialTests(unittest.TestCase):
    def test_website_report_returns_persistent_report_id(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            social = SocialService(database=database)
            pump = WebSocialEventPump(database.path, social)
            result = pump._process({
                "event_id": 42,
                "source_persona": "Alice",
                "target_persona": "Bob",
                "action": "report",
                "payload_json": json.dumps({"reason": "Harassment"}),
            })
            self.assertTrue(result["accepted"])
            self.assertGreater(result["reportId"], 0)
            self.assertEqual(pump._process({
                "event_id": 42,
                "source_persona": "Alice",
                "target_persona": "Bob",
                "action": "report",
                "payload_json": json.dumps({"reason": "Harassment"}),
            })["reportId"], result["reportId"])
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT target, reason FROM social_reports WHERE report_id=?",
                    (result["reportId"],),
                ).fetchone()
            self.assertEqual((row["target"], row["reason"]), ("Bob", "Harassment"))

    def test_completed_website_reports_are_imported_once(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            ensure_web_social_schema(database.path)
            with database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO web_social_events (
                        event_id, created_at, source_persona, target_persona,
                        action, payload_json, status
                    ) VALUES (42, 1234.0, 'Alice', 'Bob', 'report', ?, 'done')
                    """,
                    (json.dumps({"reason": "Cheating"}),),
                )
            SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            with database.connect() as connection:
                rows = connection.execute(
                    "SELECT source_event_id, target, reason FROM social_reports"
                ).fetchall()
            self.assertEqual(
                [(row["source_event_id"], row["target"], row["reason"]) for row in rows],
                [(42, "Bob", "Cheating")],
            )

    def test_event_pump_processes_friends_and_expires_old_requests(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            for name in ("Alice", "Bob"):
                database.create_account(name, "pw", persona=name)
            social = SocialService(database=database)
            events = []
            social.register_lobby("bob", "Bob", "Bob", "127.0.0.1", game_id="carbon")
            social.register_control("bob-control", "127.0.0.1", "Bob",
                                    lambda verb, fields: not events.append((verb, dict(fields))), game_id="carbon")
            ensure_web_social_schema(database.path)
            with database.transaction() as connection:
                connection.execute("INSERT INTO web_social_events(created_at,source_persona,target_persona,action) VALUES(?,?,?,?)",
                                   (time.time() - 3600, "Alice", "Bob", "block"))
                connection.execute("INSERT INTO web_social_events(created_at,source_persona,target_persona,action) VALUES(?,?,?,?)",
                                   (time.time(), "Alice", "Bob", "friend_request"))
            pump = WebSocialEventPump(database.path, social)
            pump.start()
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with sqlite3.connect(database.path) as connection:
                        rows = connection.execute("SELECT status,result_json FROM web_social_events ORDER BY event_id").fetchall()
                    if all(row[0] in {"done", "error"} for row in rows):
                        break
                    time.sleep(0.02)
                self.assertEqual([row[0] for row in rows], ["error", "done"])
                self.assertEqual(json.loads(rows[0][1])["reason"], "expired")
                self.assertFalse(social.is_blocked("Alice", "Bob"))
                self.assertEqual(social.snapshot("Bob")[0].request, "incoming")
                self.assertTrue(any(verb == "RNOT" and fields.get("ATTR") == "R" and fields.get("CHNG") == "A"
                                    for verb, fields in events))
            finally:
                pump.stop()
