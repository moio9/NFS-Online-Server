from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from common.accounts import SQLiteAccountDatabase
from common.social import SocialService


class SQLiteSocialServiceTests(unittest.TestCase):
    def test_friend_graph_persists_across_service_instances(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account("alice", "pw", persona="Alice")
            database.create_account("bob", "pw", persona="Bob")
            provider = lambda: tuple(record.persona for record in database.personas())

            social = SocialService(database=database, persona_provider=provider)
            requested = social.request_friend("Alice", "Bob")
            self.assertTrue(requested.accepted)
            self.assertEqual(requested.reason, "requested")
            accepted = social.respond_friend("Bob", "Alice", True)
            self.assertTrue(accepted.accepted)
            self.assertEqual(accepted.reason, "accepted")

            reloaded = SocialService(database=database, persona_provider=provider)
            self.assertEqual([row.user for row in reloaded.snapshot("Alice", "B")], ["Bob"])
            self.assertEqual([row.user for row in reloaded.snapshot("Bob", "B")], ["Alice"])

    def test_block_removes_friend_and_is_persistent(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account("alice", "pw", persona="Alice")
            database.create_account("bob", "pw", persona="Bob")
            provider = lambda: tuple(record.persona for record in database.personas())
            social = SocialService(database=database, persona_provider=provider)
            social.request_friend("Alice", "Bob")
            social.respond_friend("Bob", "Alice", True)

            result = social.set_blocked("Alice", "Bob", True)
            self.assertTrue(result.accepted)
            self.assertTrue(result.changed)
            self.assertEqual(social.snapshot("Alice", "B"), ())
            self.assertTrue(social.is_blocked("Alice", "Bob"))

            reloaded = SocialService(database=database, persona_provider=provider)
            self.assertEqual(reloaded.snapshot("Alice", "B"), ())
            self.assertTrue(reloaded.is_blocked("Alice", "Bob"))

    def test_game_player_snapshot_is_live_same_game_and_not_social(self) -> None:
        social = SocialService(persona_provider=lambda: ("Alice", "Bob", "Carol", "Dave"))
        social.register_lobby("alice-mw", "alice", "Alice", "127.0.0.1", game_id="most_wanted")
        social.register_lobby("bob-mw", "bob", "Bob", "127.0.0.2", game_id="most_wanted")
        social.register_lobby("carol-u2", "carol", "Carol", "127.0.0.3", game_id="underground2")
        social.register_lobby("dave-mw", "dave", "Dave", "127.0.0.4", game_id="most_wanted")
        social.set_game_session("alice-mw", "Alice", "most_wanted", "room-1")
        social.set_game_session("bob-mw", "Bob", "most_wanted", "room-1")
        social.set_game_session("carol-u2", "Carol", "underground2", "room-1")
        social.set_game_session("dave-mw", "Dave", "most_wanted", "room-1")
        social.request_friend("Alice", "Dave")
        social.respond_friend("Dave", "Alice", True)

        rows = social.game_player_snapshot("Alice", "most_wanted")
        self.assertEqual([row.user for row in rows], ["Bob"])
        self.assertEqual(rows[0].attr, "D")
        self.assertTrue(rows[0].online)

        social.set_game_session("bob-mw", "Bob", "most_wanted", "room-2")
        self.assertEqual(social.game_player_snapshot("Alice", "most_wanted"), ())
        social.set_game_session("bob-mw", "Bob", "most_wanted", "room-1")
        social.set_blocked("Alice", "Bob", True)
        self.assertEqual(social.game_player_snapshot("Alice", "most_wanted"), ())

    def test_game_player_directory_pushes_add_and_remove(self) -> None:
        social = SocialService()
        events: list[tuple[str, dict[str, str]]] = []
        social.register_lobby("alice-mw", "alice", "Alice", "127.0.0.1", game_id="most_wanted")
        self.assertIsNotNone(
            social.register_control(
                "alice-control",
                "127.0.0.1",
                "Alice",
                lambda verb, fields: not events.append((verb, dict(fields))),
                game_id="most_wanted",
            )
        )
        social.register_lobby("bob-mw", "bob", "Bob", "127.0.0.2", game_id="most_wanted")
        self.assertEqual(events, [])
        social.set_game_session("alice-mw", "Alice", "most_wanted", "room-1")
        social.set_game_session("bob-mw", "Bob", "most_wanted", "room-1")
        self.assertIn(("RNOT", {"CHNG": "A", "USER": "Bob", "ATTR": "D"}), events)
        self.assertTrue(any(verb == "PGET" and fields.get("USER") == "Bob" for verb, fields in events))
        events.clear()
        social.clear_game_session("bob-mw")
        self.assertEqual(events, [("RNOT", {"CHNG": "D", "USER": "Bob", "ATTR": "D"})])


if __name__ == "__main__":
    unittest.main()
