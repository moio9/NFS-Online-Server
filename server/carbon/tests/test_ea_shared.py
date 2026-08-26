"""Contract checks for the cross-game EA layer."""

import unittest

from carbon.ea.directory import SessionDirectory, Visibility
from carbon.ea.text import encode_message, parse_message


class EATextTests(unittest.TestCase):
    def test_text_message_round_trip(self) -> None:
        raw = encode_message("user", {"name": "Test Driver", "uid": 7, "flags": 1.5})
        self.assertEqual(raw, '+USER NAME="Test Driver" UID=7 FLAGS=1.500000\n')
        self.assertEqual(parse_message(raw), ("+", "USER", {"NAME": "Test Driver", "UID": 7, "FLAGS": 1.5}))


class SessionDirectoryTests(unittest.TestCase):
    def test_private_room_requires_membership_or_password(self) -> None:
        directory = SessionDirectory()
        room = directory.create_room(10, "race", visibility=Visibility.PRIVATE, password="go")
        self.assertFalse(directory.join_room(room.room_id, 20))
        self.assertTrue(directory.join_room(room.room_id, 20, "go"))
        self.assertEqual([item.room_id for item in directory.visible_rooms(20)], [room.room_id])

    def test_game_lifecycle_is_shared_but_wire_free(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(257, 10, capacity=2)
        self.assertEqual(game.participants, {10})
        self.assertTrue(directory.join_game(game.game_id, 20))
        self.assertFalse(directory.join_game(game.game_id, 30))
        self.assertEqual(directory.get_game(game.game_id).participants, {10, 20})
