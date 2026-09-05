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
