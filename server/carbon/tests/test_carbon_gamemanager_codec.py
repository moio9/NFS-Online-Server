"""Byte-level checks for the clean Carbon GameManager PlayerData codec."""

import unittest

from carbon.gamemanager.player_codec import (
    PlayerCodecError,
    PlayerWireData,
    decode_player_data,
    encode_join,
    encode_leave,
    encode_player_data,
    encode_roster,
)


class CarbonPlayerCodecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.player = PlayerWireData(
            player_id=2,
            name="Driver",
            profile_id=698_687_004,
            state=6,
            internal_ip="127.0.0.1",
            internal_port=1042,
            external_ip="192.0.2.10",
            external_port=3658,
        )

    def test_player_data_round_trip(self) -> None:
        encoded = encode_player_data(self.player)
        decoded, consumed = decode_player_data(encoded)
        self.assertEqual(decoded, self.player)
        self.assertEqual(consumed, len(encoded))

    def test_roster_and_join_have_distinct_capture_tags(self) -> None:
        roster = encode_roster(self.player)
        join = encode_join(self.player)
        self.assertTrue(roster.startswith(b"\x01\x83\x00\x02"))
        self.assertTrue(join.startswith(b"\x01\x85\x00\x02\x00\x02"))
        self.assertTrue(roster.endswith(b"\x04"))
        self.assertTrue(join.endswith(b"\x04"))

    def test_leave_announcement_encodes_player_and_signed_reason(self) -> None:
        self.assertEqual(encode_leave(2), bytes.fromhex("0187000280"))
        self.assertEqual(encode_leave(2, -1), bytes.fromhex("018700027f"))

    def test_invalid_endpoint_is_rejected(self) -> None:
        invalid = PlayerWireData(**{**self.player.__dict__, "internal_port": 70_000})
        with self.assertRaises(PlayerCodecError):
            encode_player_data(invalid)
