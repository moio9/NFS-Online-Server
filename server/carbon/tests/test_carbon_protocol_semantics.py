from __future__ import annotations

import unittest

from carbon.gamemanager.protocol import (
    GMMessageType,
    OLMessageType,
    ObservedActiveGameState,
    gm_message_tag,
    logical_message,
)
from carbon.gamemanager.session_object import (
    SESSION_OBJECT_CHUNK_OFFSETS,
)


class CarbonProtocolSemanticTests(unittest.TestCase):
    def test_retail_olmsg_table_is_complete_and_contiguous(self) -> None:
        expected = {
            "GAME_INIT_INFO": 0x00,
            "CLOCK_SYNC_REQUEST": 0x01,
            "CLOCK_SYNC_START": 0x02,
            "CLOCK_SYNC_END": 0x03,
            "PLAYER_CAR_DATA": 0x04,
            "PLAYER_CONTROLLED_AI_CAR": 0x05,
            "CAR_STATE": 0x06,
            "CAR_STATE_BLOCK": 0x07,
            "START_LOADING": 0x08,
            "READY_TO_START": 0x09,
            "START_RACE_SYNC_BEGIN": 0x0A,
            "START_RACE_SYNC_MESSAGE": 0x0B,
            "START_RACE": 0x0C,
            "LEADER_FINISHED": 0x0D,
            "RACER_FINISHED": 0x0E,
            "READY": 0x0F,
            "GAME_RESULTS": 0x10,
            "FINAL_GAME_RESULTS": 0x11,
            "LATENCY_INFO": 0x12,
            "MATCHMAKING_ON_REQUEST": 0x13,
            "MATCHMAKING_OFF_REQUEST": 0x14,
            "INVITES_ON_REQUEST": 0x15,
            "INVITES_OFF_REQUEST": 0x16,
            "ENABLE_JOINS_REQUEST": 0x17,
            "DISABLE_JOINS_REQUEST": 0x18,
            "ACTIVE_GAME_COLLECT_STATS": 0x19,
            "ACTIVE_GAME_UPDATE_STATS": 0x1A,
            "START_TIMER": 0x1B,
            "ACTIVE_GAME_MESSAGE": 0x1C,
            "GAME_ATTRIBUTES": 0x1D,
            "BIG_MESSAGE": 0x1E,
            "PURSUIT_TAG_SYNC": 0x1F,
            "KILL_REBROADCASTER": 0x20,
            "POST_RACE_SYNC": 0x21,
        }
        self.assertEqual({item.name: int(item) for item in OLMessageType}, expected)
        self.assertEqual([int(item) for item in OLMessageType], list(range(0x22)))

    def test_named_gamemanager_tags_preserve_wire_ids(self) -> None:
        expected = {
            GMMessageType.SESSION_TICKET: "0180",
            GMMessageType.HOST_HELLO: "0182",
            GMMessageType.PLAYER_ROSTER: "0183",
            GMMessageType.PLAYER_PUBLISH: "0184",
            GMMessageType.PLAYER_JOINED: "0185",
            GMMessageType.PLAYER_LEFT: "0187",
            GMMessageType.HOST_PROPERTIES: "018c",
        }
        self.assertEqual(
            {kind: gm_message_tag(kind).hex() for kind in expected},
            expected,
        )

    def test_known_outer_types_do_not_claim_inner_state_symbols(self) -> None:
        self.assertEqual(logical_message(OLMessageType.ACTIVE_GAME_MESSAGE).hex(), "000000001c")
        self.assertEqual(int(ObservedActiveGameState.COUNTDOWN_EXPIRED), 2)
        self.assertEqual(int(ObservedActiveGameState.COUNTDOWN_CONTEXT), 9)
        self.assertEqual(int(ObservedActiveGameState.PLAYER_COUNTDOWN_CONTEXT), 14)
        self.assertEqual(int(ObservedActiveGameState.ACTIVE_GAME_ALLOCATING), 15)

    def test_session_object_chunk_offsets_are_explicit(self) -> None:
        self.assertEqual(SESSION_OBJECT_CHUNK_OFFSETS, (0, 0x1E4, 0x3C8))


if __name__ == "__main__":
    unittest.main()
