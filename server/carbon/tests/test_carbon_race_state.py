"""Semantic HostProps encoding and explicit Carbon race-state tests."""

import unittest

from carbon.gamemanager.race_session import (
    InviteStatus,
    JoinMode,
    HostProperties,
    decode_session_attributes,
    latency_info,
    locked_host_properties,
    open_host_properties,
    reopen_host_properties,
    session_attributes,
    start_lock_host_properties,
    start_race_sync,
)
from carbon.gamemanager.race_state import (
    GameRaceState,
    RacePhase,
    RaceStateError,
    RoomAccess,
)


class CarbonSessionAttributeTests(unittest.TestCase):
    def test_ranked_1d15_matches_retail_rebroadcaster_attribute_order(self) -> None:
        body = session_attributes({
            "B-U-version": "298_prod_server+22012b18",
            "B-U-matchmaking_state": "1",
            "B-U-game_type": "0",
            "B-U-help_type": "0",
            "B-U-game_mode": "0",
            "B-U-skill": "",
            "B-U-team_play": "1",
            "B-U-car_tier": "1",
            "B-U-max_online_player": "8",
            "B-U-length": "1",
            "B-U-track": "",
            "B-U-n2o": "1",
            "B-U-collision_detection": "1",
            "B-U-player_dnf": "",
            "B-U-location": "WH-EU",
            "B-U-race_type_circuit": "ABSTAIN",
            "B-U-race_type_sprint": "mu.5.2",
            "B-U-race_type_canyon_due": "ABSTAIN",
            "B-U-race_type_speedtrap": "ABSTAIN",
            "B-U-race_type_knockout": "ABSTAIN",
            "B-U-race_type_pursuit_tag": "ABSTAIN",
        })
        self.assertEqual(
            body.hex(),
            "000000001d150000183239385f70726f645f7365727665722b3232303132623138"
            "0100013102000130030001300400013005000006000131070001310800013809000131"
            "0a00000b0001310c0001310d00000e000557482d45550f00074142535441494e"
            "1000066d752e352e321100074142535441494e1200074142535441494e"
            "1300074142535441494e1400074142535441494e",
        )
        decoded = decode_session_attributes(body)
        self.assertEqual(decoded["matchmaking_state"], "1")
        self.assertEqual(decoded["game_type"], "0")
        self.assertEqual(decoded["race_type_sprint"], "mu.5.2")


    def test_challenge_coop_1d15_matches_official_capture(self) -> None:
        body = session_attributes({
            "B-U-version": "298_prod_server+22012b18",
            "B-U-matchmaking_state": "0",
            "B-U-game_type": "2",
            "B-U-help_type": "0",
            "B-U-game_mode": "0",
            "B-U-skill": "",
            "B-U-team_play": "1",
            "B-U-car_tier": "1",
            "B-U-max_online_player": "2",
            "B-U-length": "1",
            "B-U-track": "",
            "B-U-n2o": "1",
            "B-U-collision_detection": "1",
            "B-U-player_dnf": "",
            "B-U-location": "WH-EU",
            "B-U-race_type_circuit": "ABSTAIN",
            "B-U-race_type_sprint": "cs.2.1",
            "B-U-race_type_canyon_due": "ABSTAIN",
            "B-U-race_type_speedtrap": "ABSTAIN",
            "B-U-race_type_knockout": "ABSTAIN",
            "B-U-race_type_pursuit_tag": "ABSTAIN",
        })
        self.assertEqual(
            body.hex(),
            "000000001d150000183239385f70726f645f7365727665722b3232303132623138"
            "0100013002000132030001300400013005000006000131070001310800013209000131"
            "0a00000b0001310c0001310d00000e000557482d45550f00074142535441494e"
            "10000663732e322e311100074142535441494e1200074142535441494e"
            "1300074142535441494e1400074142535441494e",
        )
        decoded = decode_session_attributes(body)
        self.assertEqual(decoded["game_type"], "2")
        self.assertEqual(decoded["help_type"], "0")
        self.assertEqual(decoded["car_tier"], "1")
        self.assertEqual(decoded["race_type_sprint"], "cs.2.1")

    def test_game_type_is_not_hardcoded_to_unranked(self) -> None:
        ranked = decode_session_attributes(session_attributes({"B-U-game_type": "0"}))
        unranked = decode_session_attributes(session_attributes({"B-U-game_type": "1"}))
        coop = decode_session_attributes(session_attributes({"B-U-game_type": "2"}))
        self.assertEqual(ranked["game_type"], "0")
        self.assertEqual(unranked["game_type"], "1")
        self.assertEqual(coop["game_type"], "2")


class CarbonHostPropertiesTests(unittest.TestCase):
    def test_open_and_locked_properties_match_capture(self) -> None:
        self.assertEqual(
            open_host_properties(2).encode().hex(),
            "018c000101808100000002",
        )
        self.assertEqual(
            locked_host_properties(8).encode().hex(),
            "018c000000828000000008",
        )

    def test_progressive_start_lock_matches_capture(self) -> None:
        self.assertEqual(
            [item.encode().hex() for item in start_lock_host_properties(8)],
            [
                "018c000101808000000008",
                "018c000100808000000008",
                "018c000000808000000008",
                "018c000000828000000008",
            ],
        )

    def test_progressive_post_race_reopen_matches_capture(self) -> None:
        self.assertEqual(
            [item.encode().hex() for item in reopen_host_properties(8)],
            [
                "018c000000828100000008",
                "018c000001828100000008",
                "018c000101828100000008",
                "018c000101808100000008",
            ],
        )

    def test_named_wire_fields_encode_without_raw_hex_constants(self) -> None:
        props = HostProperties(
            wire_flag0=True,
            join_in_progress=False,
            join_via_presence=True,
            invite_status=InviteStatus.HOST_ONLY,
            join_mode=JoinMode.AUTO,
            join_flags=0x1234,
            max_hosted_players=4,
        )
        self.assertEqual(props.encode().hex(), "018c010001818212340004")

    def test_latency_and_start_sync_fields_are_explicit(self) -> None:
        self.assertEqual(
            latency_info(2, 25.0).hex(),
            "00000000120000000241c80000",
        )
        self.assertEqual(
            start_race_sync(
                0x01020304,
                start_delay_seconds=2.0,
                ping=0.0,
            ).hex(),
            "000000000a010203044000000000000000",
        )


class CarbonRaceStateTests(unittest.TestCase):
    def test_full_lifecycle_is_monotonic(self) -> None:
        state = GameRaceState(countdown_duration=30.5)
        self.assertEqual(state.phase, RacePhase.SESSION_SETUP)
        self.assertEqual(state.room_access, RoomAccess.OPEN)

        self.assertTrue(state.begin_countdown(100.0))
        self.assertEqual(state.countdown_deadline, 130.5)
        self.assertTrue(state.lock_room_access())
        self.assertTrue(state.mark_countdown_expired())
        self.assertTrue(state.mark_start_locked())
        self.assertTrue(state.mark_loading(now=140.0, source_player_id=11))
        self.assertEqual(state.loading_started_at, 140.0)
        self.assertEqual(state.loading_player_ids, {11})
        self.assertTrue(state.observe_loading_player(22))
        self.assertFalse(state.observe_loading_player(22))
        self.assertTrue(state.mark_racing())
        self.assertEqual(state.phase, RacePhase.RACING)
        self.assertEqual(state.room_access, RoomAccess.LOCKED)

    def test_invalid_transition_is_rejected(self) -> None:
        state = GameRaceState()
        with self.assertRaises(RaceStateError):
            state.mark_loading()


if __name__ == "__main__":
    unittest.main()
