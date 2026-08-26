"""Capture/decompilation-driven Carbon PlayNow matchmaking tests."""

import unittest
from unittest.mock import patch

from carbon.accounts.identity import IdentityStore
from carbon.core.config import Endpoint
from carbon.fesl.frame import FESLFrame
from carbon.fesl.service import CarbonEndpoints, CarbonFESLService, FESLConnection
from carbon.theater.directory import CarbonGameDirectory
from carbon.theater.service import CarbonTheaterService, TheaterConnection


VERSION = "298_prod_server+22012b18"
HELP_VALUES = '"0,1,2,3"'
HELP_TABLE = "0.8;-1;-1;1|1;0.9;-1;0.9|1;-1;1;1|1;0.9;1;1"


def reset_fields(game_type: str, *, help_type: str = "0", game_mode: str = "1") -> dict[str, str]:
    return {
        "TXN": "Start",
        "players.0.props.{sessionType}": "resetServer",
        "players.0.props.{filter-version}": VERSION,
        "players.0.props.{filter-game_type}": game_type,
        "players.0.props.{filter-matchmaking_state}": "1",
        "players.0.props.{pref-car_tier}": "3",
        "players.0.props.{pref-collision_detection}": "1",
        "players.0.props.{pref-game_mode}": game_mode,
        "players.0.props.{pref-help_type}": help_type,
        "players.0.props.{pref-length}": "2",
        "players.0.props.{pref-n2o}": "1",
        "players.0.props.{pref-race_type_circuit}": "ex.5.1",
        "players.0.props.{pref-race_type_knockout}": "ABSTAIN",
        "players.0.props.{pref-race_type_speedtrap}": "ABSTAIN",
        "players.0.props.{pref-race_type_pursuit_tag}": "ABSTAIN",
        "players.0.props.{pref-race_type_canyon_due}": "ABSTAIN",
        "players.0.props.{pref-race_type_sprint}": "ABSTAIN",
        "players.0.props.{pref-team_play}": "1",
        "players.0.props.{pref-max_online_player}": "8",
        "players.0.props.{pref-player_dnf}": "25",
        "players.0.props.{pref-skill}": "1000",
    }


def find_fields(game_types: str, *, help_type: str = "2", version: str = VERSION) -> dict[str, str]:
    return {
        "TXN": "Start",
        "players.0.props.{sessionType}": "findServer",
        "players.0.props.{filter-version}": version,
        "players.0.props.{filter-game_type}": game_types,
        "players.0.props.{filter-matchmaking_state}": "1",
        "players.0.props.{fitThreshold}": "0:0",
        "players.0.props.{pref-help_type}": help_type,
        "players.0.props.{fitValues-help_type}": HELP_VALUES,
        "players.0.props.{fitTable-help_type}": HELP_TABLE,
        "players.0.props.{fitWeight-help_type}": "14000",
    }

class CarbonPlayNowMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identities = IdentityStore(token_factory=lambda: "matchmaking-key.")
        self.first, _ = self.identities.login("First")
        self.second, _ = self.identities.login("Second")
        self.games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))

    def test_official_ranked_reset_resolves_a_concrete_gdat_profile(self) -> None:
        resolution = self.games.resolve_play_now(self.first, reset_fields("0"))
        self.assertIsNotNone(resolution)
        assert resolution is not None
        game = resolution.game
        row = game.row()

        self.assertTrue(resolution.created)
        self.assertTrue(game.is_ranked)
        self.assertEqual(row["B-U-game_type"], "0")
        self.assertEqual(row["B-U-matchmaking_state"], "1")
        self.assertEqual(row["B-U-version"], VERSION)
        self.assertEqual(row["B-U-game_mode"], "1")
        self.assertEqual(row["B-U-help_type"], "0")
        self.assertEqual(row["B-U-car_tier"], "3")
        self.assertEqual(row["B-U-length"], "2")
        self.assertEqual(row["B-U-skill"], "500")
        self.assertEqual(row["B-U-player_dnf"], "12")
        self.assertEqual(row["B-U-race_type_circuit"], "ex.5.1")
        self.assertEqual(row["B-U-race_type_sprint"], "ct.4.2")
        self.assertNotIn("|", row["B-U-game_type"])
        self.assertEqual(row["HU"], "1")
        self.assertEqual(row["JP"], "0")
        self.assertEqual(row["AP"], "0")
        self.assertEqual(row["QP"], "0")

    def test_ranked_unranked_and_coop_remain_distinct(self) -> None:
        ranked = self.games.resolve_play_now(self.first, reset_fields("0"))
        unranked = self.games.resolve_play_now(self.first, reset_fields("1"))
        coop = self.games.resolve_play_now(self.first, reset_fields("2", help_type="2", game_mode="0"))
        assert ranked is not None and unranked is not None and coop is not None

        self.assertEqual(ranked.game.properties["B-U-game_type"], "0")
        self.assertEqual(unranked.game.properties["B-U-game_type"], "1")
        self.assertEqual(coop.game.properties["B-U-game_type"], "2")
        self.assertEqual(coop.game.properties["B-U-matchmaking_state"], "0")
        self.assertEqual(coop.game.properties["B-U-game_mode"], "0")
        self.assertEqual(coop.game.properties["B-U-skill"], "")
        self.assertEqual(coop.game.properties["B-U-player_dnf"], "")
        self.assertEqual(coop.game.properties["B-U-team_play"], "1")
        self.assertEqual(coop.game.properties["B-U-max_online_player"], "2")
        # Retail keeps the requester helper class private to matchmaking and
        # publishes help_type=0 in the final game_type=2 room.
        self.assertEqual(coop.game.properties["B-U-help_type"], "0")
        self.assertEqual(coop.game.coop_match_help_type, "2")
        self.assertFalse(unranked.game.is_ranked)
        self.assertFalse(coop.game.is_ranked)

    def test_quick_join_selects_only_the_requested_ranked_mode(self) -> None:
        ranked = self.games.resolve_play_now(self.first, reset_fields("0"))
        unranked = self.games.resolve_play_now(self.first, reset_fields("1"))
        assert ranked is not None and unranked is not None

        ranked_match = self.games.resolve_play_now(self.second, find_fields("0"))
        unranked_match = self.games.resolve_play_now(self.second, find_fields("1"))

        self.assertIsNotNone(ranked_match)
        self.assertIsNotNone(unranked_match)
        assert ranked_match is not None and unranked_match is not None
        self.assertEqual(ranked_match.game.gid, ranked.game.gid)
        self.assertEqual(ranked_match.game.properties["B-U-game_type"], "0")
        self.assertEqual(unranked_match.game.gid, unranked.game.gid)
        self.assertEqual(unranked_match.game.properties["B-U-game_type"], "1")

    def test_challenge_identity_is_locked_before_host_gamemanager_snapshot(self) -> None:
        fields = reset_fields("2", help_type="3", game_mode="1")
        fields["players.0.props.{filter-matchmaking_state}"] = "1"
        fields["players.0.props.{pref-team_play}"] = "0"
        resolution = self.games.resolve_play_now(self.first, fields)
        self.assertIsNotNone(resolution)
        assert resolution is not None

        properties = resolution.game.properties
        self.assertEqual(properties["B-U-game_type"], "2")
        self.assertEqual(properties["B-U-matchmaking_state"], "0")
        self.assertEqual(properties["B-U-help_type"], "0")
        self.assertEqual(properties["B-U-game_mode"], "0")
        self.assertEqual(properties["B-U-max_online_player"], "2")
        self.assertEqual(properties["B-U-team_play"], "1")
        self.assertEqual(properties["B-U-skill"], "")
        self.assertEqual(properties["B-U-player_dnf"], "")
        self.assertEqual(properties["B-U-track"], "")
        for name in (
            "race_type_circuit",
            "race_type_knockout",
            "race_type_speedtrap",
            "race_type_pursuit_tag",
            "race_type_canyon_due",
            "race_type_sprint",
        ):
            self.assertEqual(properties[f"B-U-{name}"], "ABSTAIN")
        self.assertEqual(resolution.game.coop_match_help_type, "3")

    def test_challenge_allocation_preserves_only_one_concrete_cs_event(self) -> None:
        fields = reset_fields("2", help_type="2", game_mode="5")
        fields["players.0.props.{filter-matchmaking_state}"] = "0"
        for name in (
            "race_type_circuit",
            "race_type_knockout",
            "race_type_speedtrap",
            "race_type_pursuit_tag",
            "race_type_canyon_due",
            "race_type_sprint",
        ):
            fields[f"players.0.props.{{pref-{name}}}"] = "ABSTAIN"
        fields["players.0.props.{pref-race_type_speedtrap}"] = "cs.11.2"

        resolution = self.games.resolve_play_now(self.first, fields)

        self.assertIsNotNone(resolution)
        assert resolution is not None
        properties = resolution.game.properties
        self.assertEqual(properties["B-U-game_type"], "2")
        self.assertEqual(properties["B-U-game_mode"], "5")
        self.assertEqual(properties["B-U-race_type_speedtrap"], "cs.11.2")
        for name in (
            "race_type_circuit",
            "race_type_knockout",
            "race_type_pursuit_tag",
            "race_type_canyon_due",
            "race_type_sprint",
        ):
            self.assertEqual(properties[f"B-U-{name}"], "ABSTAIN")

    def test_room_ticks_and_descriptor_handle_ranges_are_allocated_per_room(self) -> None:
        with patch(
            "carbon.theater.directory.time.monotonic",
            return_value=1234.567,
        ):
            first = self.games.resolve_play_now(self.first, reset_fields("0"))
            second = self.games.resolve_play_now(
                self.second,
                reset_fields("2", help_type="2", game_mode="0"),
            )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None

        self.assertNotEqual(first.game.created_tick_ms, 0)
        self.assertNotEqual(second.game.created_tick_ms, 0)
        self.assertNotEqual(first.game.created_tick_ms, second.game.created_tick_ms)

        first_start = first.game.descriptor_handle_base
        first_end = first_start + (first.game.session.capacity - 1) * 10
        second_start = second.game.descriptor_handle_base
        second_end = second_start + (second.game.session.capacity - 1) * 10
        self.assertTrue(first_end < second_start or second_end < first_start)

    def test_reset_server_always_allocates_instead_of_reusing_a_room(self) -> None:
        first = self.games.resolve_play_now(self.first, reset_fields("0"))
        second = self.games.resolve_play_now(self.second, reset_fields("0"))
        assert first is not None and second is not None
        self.assertNotEqual(first.game.gid, second.game.gid)
        self.assertEqual(len(self.games.list()), 2)

    def test_reset_server_rejects_set_valued_creation_type(self) -> None:
        fields = reset_fields("0|2")
        self.assertIsNone(self.games.resolve_play_now(self.first, fields))
        self.assertEqual(self.games.list(), [])

    def test_find_does_not_return_an_unentered_stale_allocation_to_its_owner(self) -> None:
        created = self.games.resolve_play_now(self.first, reset_fields("0"))
        self.assertIsNotNone(created)
        self.assertIsNone(self.games.resolve_play_now(self.first, find_fields("0|2")))
        self.assertIsNotNone(self.games.resolve_play_now(self.second, find_fields("0|2")))

    def test_find_server_rejects_wrong_coop_help_type(self) -> None:
        self.games.challenge_quick_join_after_ready = True
        incompatible = self.games.resolve_play_now(
            self.first,
            reset_fields("2", help_type="1"),
        )
        self.assertIsNotNone(incompatible)
        assert incompatible is not None
        self.games.enter(incompatible.game.gid, self.first)
        self.games.set_challenge_ready(
            incompatible.game.gid,
            True,
            reason="test-ready",
        )

        no_match = self.games.resolve_play_now(
            self.second,
            find_fields("1|2", help_type="2"),
        )
        self.assertIsNone(no_match)

        compatible = self.games.resolve_play_now(
            self.first,
            reset_fields("2", help_type="2"),
        )
        self.assertIsNotNone(compatible)
        assert compatible is not None
        self.assertIsNotNone(
            self.games.enter(
                compatible.game.gid,
                self.first,
                internal_ip="192.168.1.9",
                internal_port=1042,
            )
        )
        self.games.set_challenge_ready(
            compatible.game.gid,
            True,
            reason="test-ready",
        )
        found = self.games.resolve_play_now(
            self.second,
            find_fields("1|2", help_type="2"),
        )
        self.assertIsNotNone(found)
        assert found is not None and compatible is not None
        self.assertEqual(found.game.gid, compatible.game.gid)
        self.assertFalse(found.created)


    def test_unranked_quick_search_enters_waiting_challenge_before_ready(self) -> None:
        self.games.challenge_quick_join_before_ready = True
        self.games.challenge_quick_join_after_ready = True
        host_fields = reset_fields("2", help_type="2", game_mode="0")
        host_fields["players.0.props.{filter-matchmaking_state}"] = "0"
        created = self.games.resolve_play_now(self.first, host_fields)
        self.assertIsNotNone(created)
        assert created is not None
        self.assertEqual(created.game.properties["B-U-game_type"], "2")
        self.assertEqual(created.game.properties["B-U-matchmaking_state"], "0")
        self.assertEqual(created.game.session.capacity, 2)

        # An empty allocation is not joinable until the requester completes EGAM.
        self.assertIsNone(self.games.resolve_play_now(self.second, find_fields("1|2", help_type="2")))
        self.games.enter(
            created.game.gid,
            self.first,
            internal_ip="192.168.1.9",
            internal_port=1042,
        )

        # Unranked Quick Search advertises type 1|2/state 1.  Once the requester
        # has completed EGAM it can enter this waiting type-2/state-0 room.
        before_ready = self.games.resolve_play_now(
            self.second,
            find_fields("1|2", help_type="2"),
        )
        self.assertIsNotNone(before_ready)
        assert before_ready is not None
        self.assertEqual(before_ready.game.gid, created.game.gid)
        self.assertFalse(before_ready.created)

        # Ranked Quick Search (0|2) must never cross the co-op bridge into a
        # Challenge room. Only the Unranked helper path is supported here.
        ranked = self.games.resolve_play_now(
            self.second,
            find_fields("0|2", help_type="2"),
        )
        self.assertIsNone(ranked)

        self.assertTrue(
            self.games.set_challenge_ready(
                created.game.gid,
                True,
                reason="test-ready",
            )
        )
        found = self.games.resolve_play_now(self.second, find_fields("1|2", help_type="2"))
        self.assertIsNotNone(found)
        assert found is not None
        self.assertFalse(found.created)
        self.assertEqual(found.game.gid, created.game.gid)


    def test_direct_reset_helper_joins_waiting_challenge_assist_room(self) -> None:
        self.games.challenge_quick_join_after_ready = True
        host_fields = reset_fields("2", help_type="2", game_mode="0")
        host_fields["players.0.props.{filter-matchmaking_state}"] = "0"
        created = self.games.resolve_play_now(self.first, host_fields)
        self.assertIsNotNone(created)
        assert created is not None
        self.games.enter(
            created.game.gid,
            self.first,
            internal_ip="192.168.1.9",
            internal_port=1042,
        )
        self.games.set_challenge_ready(
            created.game.gid,
            True,
            reason="test-ready",
        )

        # Live retail helper trace: no findServer, just resetServer type 1,
        # state 0, help 0 and ABSTAIN event preferences.
        helper = reset_fields("1", help_type="0", game_mode="ABSTAIN")
        helper["players.0.props.{filter-matchmaking_state}"] = "0"
        for name in (
            "race_type_circuit",
            "race_type_knockout",
            "race_type_speedtrap",
            "race_type_pursuit_tag",
            "race_type_canyon_due",
            "race_type_sprint",
        ):
            helper[f"players.0.props.{{pref-{name}}}"] = "ABSTAIN"

        joined = self.games.resolve_play_now(self.second, helper)
        self.assertIsNotNone(joined)
        assert joined is not None
        self.assertFalse(joined.created)
        self.assertEqual(joined.game.gid, created.game.gid)
        self.assertEqual(len(self.games.list()), 1)

    def test_challenge_stays_invite_only_after_ready_when_disabled(self) -> None:
        created = self.games.resolve_play_now(
            self.first,
            reset_fields("2", help_type="2", game_mode="0"),
        )
        assert created is not None
        host = self.games.enter(created.game.gid, self.first)
        assert host is not None

        self.games.set_challenge_ready(
            created.game.gid,
            True,
            reason="test-ready",
        )

        self.assertTrue(created.game.challenge_ready)
        self.assertTrue(created.game.quick_join_locked)
        self.assertIsNone(
            self.games.resolve_play_now(
                self.second,
                find_fields("1|2", help_type="2"),
            )
        )
        self.assertIsNotNone(
            self.games.enter(
                created.game.gid,
                self.second,
                invite_remote_player_id=host.player_id,
                invite_entry=True,
            )
        )

    def test_direct_reset_with_concrete_event_still_allocates_own_room(self) -> None:
        host_fields = reset_fields("2", help_type="2", game_mode="0")
        host_fields["players.0.props.{filter-matchmaking_state}"] = "0"
        created = self.games.resolve_play_now(self.first, host_fields)
        assert created is not None
        self.games.enter(created.game.gid, self.first)

        custom = reset_fields("1", help_type="0", game_mode="1")
        custom["players.0.props.{filter-matchmaking_state}"] = "0"
        own = self.games.resolve_play_now(self.second, custom)
        self.assertIsNotNone(own)
        assert own is not None
        self.assertTrue(own.created)
        self.assertNotEqual(own.game.gid, created.game.gid)

    def test_state_zero_non_coop_room_remains_closed_to_state_one_search(self) -> None:
        created = self.games.resolve_play_now(self.first, reset_fields("1"))
        assert created is not None
        created.game.properties["B-U-matchmaking_state"] = "0"
        self.games.enter(
            created.game.gid,
            self.first,
            internal_ip="192.168.1.9",
            internal_port=1042,
        )
        self.assertIsNone(self.games.resolve_play_now(self.second, find_fields("1|2", help_type="2")))

    def test_ready_lock_removes_room_from_quick_join_but_allows_invite_entry(self) -> None:
        created = self.games.resolve_play_now(self.first, reset_fields("1"))
        assert created is not None
        game = created.game
        host = self.games.enter(
            game.gid,
            self.first,
            internal_ip="192.168.1.9",
            internal_port=1042,
        )
        assert host is not None

        self.assertTrue(
            self.games.set_quick_join_locked(
                game.gid,
                True,
                reason="test-ready",
            )
        )
        self.assertIsNone(self.games.resolve_play_now(self.second, find_fields("1")))
        self.assertIsNone(self.games.enter(game.gid, self.second))

        invited = self.games.enter(
            game.gid,
            self.second,
            invite_remote_player_id=host.player_id,
            invite_entry=True,
        )
        self.assertIsNotNone(invited)

    def test_find_server_enforces_version_and_matchmaking_state(self) -> None:
        created = self.games.resolve_play_now(self.first, reset_fields("0"))
        assert created is not None
        created.game.properties["B-U-matchmaking_state"] = "0"
        self.assertIsNone(self.games.resolve_play_now(self.second, find_fields("0|2")))

        created.game.properties["B-U-matchmaking_state"] = "1"
        self.assertIsNone(
            self.games.resolve_play_now(
                self.second,
                find_fields("0|2", version="wrong-version"),
            )
        )
        self.assertIsNotNone(self.games.resolve_play_now(self.second, find_fields("0|2")))

    def test_dedicated_gdat_hu_is_not_egeg_transport_huid(self) -> None:
        resolution = self.games.resolve_play_now(self.first, reset_fields("0"))
        assert resolution is not None
        theater = CarbonTheaterService(self.identities, self.games)
        connection = TheaterConnection(identity=self.first)
        replies = theater.dispatch(
            FESLFrame.from_fields(
                "EGAM",
                {
                    "TID": "7",
                    "LID": "257",
                    "GID": resolution.game.gid,
                    "R-INT-IP": "192.168.1.5",
                    "R-INT-PORT": "1042",
                },
            ),
            connection,
        )
        self.assertEqual(resolution.game.row()["HU"], "1")
        self.assertNotEqual(resolution.game.row()["HU"], replies[2].fields["HUID"])
        self.assertEqual(replies[2].fields["HUID"], str(resolution.game.host.user_id))

    def test_find_and_reset_use_distinct_play_now_session_ids(self) -> None:
        service = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
            self.identities,
            self.games,
            authentication_mode="open",
        )
        connection = FESLConnection(identity=self.first)
        find = service.dispatch(
            FESLFrame.from_fields("pnow", find_fields("0|2"), transaction=30),
            connection,
        )
        reset = service.dispatch(
            FESLFrame.from_fields("pnow", reset_fields("0"), transaction=31),
            connection,
        )
        self.assertEqual(find[0].fields["id.id"], "2550")
        self.assertEqual(find[1].fields["id.id"], "2550")
        self.assertEqual(reset[0].fields["id.id"], "2551")
        self.assertEqual(reset[1].fields["id.id"], "2551")

    def test_fesl_returns_computed_fit_instead_of_constant_one(self) -> None:
        service = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
            self.identities,
            self.games,
            authentication_mode="open",
        )
        connection = FESLConnection(identity=self.first)
        replies = service.dispatch(
            FESLFrame.from_fields("pnow", reset_fields("0"), transaction=31),
            connection,
        )
        self.assertEqual(replies[1].fields["props.{resultType}"], "JOIN")
        fit = float(replies[1].fields["props.{avgFit}"])
        self.assertGreaterEqual(fit, 0.0)
        self.assertLess(fit, 1.0)


if __name__ == "__main__":
    unittest.main()
