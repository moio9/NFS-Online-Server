"""Carbon ranked result codec, arbitration and persistence tests."""

from datetime import datetime
import unittest
from unittest import mock

from carbon.accounts.identity import IdentityStore
from carbon.gamemanager.results import (
    FINAL_GAME_RESULTS,
    GAME_RESULTS,
    LEADER_FINISHED,
    RACER_FINISHED,
    FinishSignal,
    PlayerGameResults,
    RaceResultTracker,
    RacerResult,
)
from carbon.gamemanager.race_state import RacePhase
from carbon.progression import (
    DNF_LOSSES_STAT,
    ONLINE_REP_STAT,
    SKILL_LEVEL_STAT,
    TOTAL_GAMES_FINISHED_STAT,
    TOTAL_GAMES_STARTED_STAT,
    CarbonProgressionStore,
    calculate_rep_score,
    calculate_skill_levels,
)
from carbon.tests import test_carbon_race_flow as race_flow


class CarbonResultCodecTests(unittest.TestCase):
    def test_finish_signal_order_matches_decompilation(self) -> None:
        signal = FinishSignal(server_car_id=2, finish_reason=11)
        self.assertEqual(
            signal.encode(LEADER_FINISHED).hex(),
            "000000000d0000000b00000002",
        )
        self.assertEqual(
            FinishSignal.decode(signal.encode(RACER_FINISHED), expected_type=RACER_FINISHED),
            signal,
        )

    def test_game_and_final_results_round_trip_all_fields(self) -> None:
        rows = (
            RacerResult(
                ranking=1,
                finish_reason=3,
                canyon_points=123.5,
                server_car_id=1,
                laps_completed=2.0,
                race_time=75.25,
                best_lap_time=35.0,
                average_speed=110.0,
                top_speed=180.0,
                lbs_nos_used=4.5,
                delta_xp=28.0,
                custom=7.0,
                custom2=8.0,
            ),
            RacerResult(
                lost_connection=True,
                ranking=2,
                finish_reason=11,
                server_car_id=2,
            ),
        )
        results = PlayerGameResults(
            number_of_racers=2,
            race_mode=3,
            track_number=42,
            track_direction=1,
            number_of_laps=2,
            reporting_server_car_id=1,
            racers=rows,
        )
        game_raw = results.encode(GAME_RESULTS)
        self.assertEqual(len(game_raw), 391)
        decoded = PlayerGameResults.decode(game_raw)
        self.assertEqual(decoded.results.number_of_racers, 2)
        self.assertEqual(decoded.results.racers[0].server_car_id, 1)
        self.assertAlmostEqual(decoded.results.racers[0].race_time, 75.25)
        self.assertTrue(decoded.results.racers[1].lost_connection)

        final_raw = results.encode(
            FINAL_GAME_RESULTS,
            when=datetime(2026, 7, 20, 18, 45, 12),
        )
        self.assertEqual(len(final_raw), 401)
        final = PlayerGameResults.decode(final_raw)
        self.assertIsNotNone(final.timestamp)
        assert final.timestamp is not None
        self.assertEqual(final.timestamp.hour, 18)
        self.assertEqual(final.timestamp.month, 6)
        self.assertEqual(final.timestamp.year_since_1900, 126)

    def test_tracker_rejects_car_spoof_and_owns_ranking(self) -> None:
        tracker = RaceResultTracker(ranked=True)
        self.assertTrue(tracker.bind_car(100, 1))
        self.assertTrue(tracker.bind_car(200, 2))
        self.assertFalse(tracker.record_finish(100, FinishSignal(2, 0)))
        self.assertTrue(tracker.record_finish(200, FinishSignal(2, 0)))
        self.assertTrue(tracker.record_finish(100, FinishSignal(1, 0)))
        final = tracker.finalize(((100, 1), (200, 2)), race_mode_fallback=5)
        self.assertEqual(final.winner_profile_ids, (200,))
        self.assertEqual(
            {item.profile_id: item.ranking for item in final.placements},
            {100: 2, 200: 1},
        )

    def test_unranked_report_quorum_preserves_ai_roster_and_local_car_ids(self) -> None:
        tracker = RaceResultTracker(ranked=False)
        rows = (
            RacerResult(ranking=2, server_car_id=100, race_time=105.0),
            RacerResult(ranking=1, server_car_id=110, race_time=101.0),
            RacerResult(ranking=3, server_car_id=101, race_time=106.0),
            RacerResult(ranking=4, server_car_id=102, race_time=107.0),
            RacerResult(ranking=5, server_car_id=103, race_time=108.0),
        )
        host_report = PlayerGameResults(
            number_of_racers=5,
            reporting_server_car_id=100,
            racers=rows,
        )
        guest_report = PlayerGameResults(
            number_of_racers=5,
            reporting_server_car_id=110,
            racers=rows,
        )

        self.assertTrue(tracker.record_report(1000, host_report))
        self.assertFalse(tracker.is_complete((1000, 2000)))
        self.assertTrue(tracker.record_report(2000, guest_report))
        self.assertTrue(tracker.is_complete((1000, 2000)))

        final = tracker.finalize(((1000, 17954), (2000, 3305)))
        self.assertEqual(final.results.number_of_racers, 5)
        self.assertEqual(final.results.reporting_server_car_id, 110)
        self.assertEqual(
            [row.server_car_id for row in final.results.racers[:5]],
            [100, 110, 101, 102, 103],
        )
        self.assertEqual(final.winner_profile_ids, (2000,))
        self.assertEqual(
            {item.profile_id: item.ranking for item in final.placements},
            {1000: 2, 2000: 1},
        )

    def test_ranked_unanimous_report_quorum_replaces_missing_leader_finished(self) -> None:
        tracker = RaceResultTracker(ranked=True)
        rows = (
            RacerResult(ranking=2, server_car_id=140, race_time=105.0),
            RacerResult(ranking=1, server_car_id=150, race_time=101.0),
        )
        host_report = PlayerGameResults(
            number_of_racers=2,
            reporting_server_car_id=140,
            racers=rows,
        )
        guest_report = PlayerGameResults(
            number_of_racers=2,
            reporting_server_car_id=150,
            racers=rows,
        )

        self.assertTrue(tracker.record_report(1000, host_report))
        self.assertFalse(tracker.is_complete((1000, 2000)))
        self.assertTrue(tracker.record_report(2000, guest_report))
        self.assertEqual(
            tracker.ranked_report_order((1000, 2000)),
            (150, 140),
        )
        self.assertTrue(tracker.is_complete((1000, 2000)))

        final = tracker.finalize(((1000, 17954), (2000, 3305)))
        self.assertEqual(final.winner_profile_ids, (2000,))
        self.assertEqual(
            {item.profile_id: item.ranking for item in final.placements},
            {1000: 2, 2000: 1},
        )
        self.assertTrue(all(item.finished for item in final.placements))

    def test_ranked_conflicting_reports_do_not_form_result_quorum(self) -> None:
        tracker = RaceResultTracker(ranked=True)
        host_rows = (
            RacerResult(ranking=1, server_car_id=140),
            RacerResult(ranking=2, server_car_id=150),
        )
        guest_rows = (
            RacerResult(ranking=2, server_car_id=140),
            RacerResult(ranking=1, server_car_id=150),
        )
        self.assertTrue(tracker.record_report(
            1000,
            PlayerGameResults(
                number_of_racers=2,
                reporting_server_car_id=140,
                racers=host_rows,
            ),
        ))
        self.assertTrue(tracker.record_report(
            2000,
            PlayerGameResults(
                number_of_racers=2,
                reporting_server_car_id=150,
                racers=guest_rows,
            ),
        ))
        self.assertIsNone(tracker.ranked_report_order((1000, 2000)))
        self.assertFalse(tracker.is_complete((1000, 2000)))


class CarbonRankedProgressionTests(unittest.TestCase):
    def test_original_skill_and_rep_formulas(self) -> None:
        self.assertEqual(calculate_skill_levels((1000.0, 1000.0), (1, 2)), (1012.0, 988.0))
        self.assertEqual(calculate_rep_score(2, 1, finished=True), 28.0)
        self.assertEqual(calculate_rep_score(2, 2, finished=True), 0.0)
        self.assertEqual(calculate_rep_score(2, 1, finished=False), 0.0)

    def test_authoritative_ranked_result_updates_stats(self) -> None:
        identities = IdentityStore(token_factory=lambda: "ranked.")
        winner, _ = identities.login("Winner", "Winner")
        dnf, _ = identities.login("DNF", "DNF")
        store = CarbonProgressionStore()
        result = store.record_authoritative_race(
            (winner, dnf),
            event_type=1,
            rankings={winner.profile_id: 1, dnf.profile_id: 2},
            finished_profile_ids={winner.profile_id},
            ranked=True,
        )
        self.assertAlmostEqual(result.skill_levels[winner.profile_id], 1012.0)
        self.assertAlmostEqual(result.skill_levels[dnf.profile_id], 988.0)
        self.assertEqual(result.rep_awards[winner.profile_id], 28.0)
        self.assertEqual(store.stat_for_profile(winner.profile_id, TOTAL_GAMES_STARTED_STAT), 1.0)
        self.assertEqual(store.stat_for_profile(winner.profile_id, TOTAL_GAMES_FINISHED_STAT), 1.0)
        self.assertEqual(store.stat_for_profile(dnf.profile_id, DNF_LOSSES_STAT), 1.0)
        self.assertEqual(store.stat_for_profile(winner.profile_id, ONLINE_REP_STAT), 28.0)
        self.assertEqual(store.stat_for_profile(winner.profile_id, SKILL_LEVEL_STAT), 1012.0)


class CarbonResultFlowTests(unittest.TestCase):
    def test_ranked_finish_order_sends_leader_and_final_results(self) -> None:
        flow = race_flow._finalized_flow()
        flow.game.properties["B-U-ranked"] = "1"
        race_flow._advance_to_countdown_expired(flow)
        race_flow._send(
            flow.service,
            flow.host_addr,
            outer=100,
            sequence=0x20,
            acknowledgement=0x130,
            logical=bytes.fromhex("000000000800000001"),
        )
        race_flow._send(
            flow.service,
            flow.host_addr,
            outer=101,
            sequence=0x21,
            acknowledgement=0x131,
            logical=bytes.fromhex("000000000f"),
        )
        race_flow._send(
            flow.service,
            flow.guest_addr,
            outer=102,
            sequence=0x22,
            acknowledgement=0x132,
            logical=bytes.fromhex("000000000f"),
        )
        self.assertEqual(flow.service._race[flow.game.gid].phase, RacePhase.RACING)

        first = race_flow._send(
            flow.service,
            flow.guest_addr,
            outer=110,
            sequence=0x30,
            acknowledgement=0x140,
            logical=FinishSignal(2, 1).encode(LEADER_FINISHED),
        )
        first_types = [body[4] for body in race_flow._logical_replies(first) if len(body) >= 5]
        self.assertEqual(first_types.count(LEADER_FINISHED), 2)
        self.assertTrue(all(
            FinishSignal.decode(body, expected_type=LEADER_FINISHED).finish_reason == 0
            for body in race_flow._logical_replies(first)
            if len(body) >= 13 and body[4] == LEADER_FINISHED
        ))
        self.assertNotIn(FINAL_GAME_RESULTS, first_types)

        publication_phases: list[tuple[int, RacePhase]] = []
        publication_order: list[int | str] = []
        append_active_body = flow.service._append_active_body
        finalize_if_complete = flow.service.race_results.finalize_if_complete

        def append_with_phase(replies, destination, body):
            if len(body) >= 5 and body[4] in {
                LEADER_FINISHED,
                FINAL_GAME_RESULTS,
            }:
                publication_order.append(body[4])
                publication_phases.append(
                    (body[4], flow.service._race[flow.game.gid].phase)
                )
            return append_active_body(replies, destination, body)

        def finalize_after_leader(game):
            publication_order.append("finalize")
            return finalize_if_complete(game)

        with mock.patch.object(
            flow.service,
            "_append_active_body",
            side_effect=append_with_phase,
        ), mock.patch.object(
            flow.service.race_results,
            "finalize_if_complete",
            side_effect=finalize_after_leader,
        ):
            second = race_flow._send(
                flow.service,
                flow.host_addr,
                outer=111,
                sequence=0x31,
                acknowledgement=0x141,
                logical=FinishSignal(1, 1).encode(LEADER_FINISHED),
            )
        second_types = [body[4] for body in race_flow._logical_replies(second) if len(body) >= 5]
        self.assertEqual(second_types.count(LEADER_FINISHED), 2)
        self.assertEqual(second_types.count(FINAL_GAME_RESULTS), 2)
        self.assertEqual(
            publication_order,
            [
                LEADER_FINISHED,
                LEADER_FINISHED,
                "finalize",
                FINAL_GAME_RESULTS,
                FINAL_GAME_RESULTS,
            ],
        )
        self.assertEqual(
            publication_phases,
            [
                (LEADER_FINISHED, RacePhase.RACING),
                (LEADER_FINISHED, RacePhase.RACING),
                (FINAL_GAME_RESULTS, RacePhase.RACING),
                (FINAL_GAME_RESULTS, RacePhase.RACING),
            ],
        )
        self.assertEqual(flow.service._race[flow.game.gid].phase, RacePhase.FINISHED)

        guest_profile = flow.guest.profile_id
        host_profile = flow.host.profile_id
        self.assertEqual(flow.service.progression.stat_for_profile(guest_profile, ONLINE_REP_STAT), 28.0)
        self.assertAlmostEqual(flow.service.progression.stat_for_profile(guest_profile, SKILL_LEVEL_STAT), 1012.0)
        self.assertAlmostEqual(flow.service.progression.stat_for_profile(host_profile, SKILL_LEVEL_STAT), 988.0)

    def test_client_racer_finished_is_relayed_without_result_authority(self) -> None:
        flow = race_flow._finalized_flow()
        race_flow._advance_to_countdown_expired(flow)
        race_flow._send(
            flow.service,
            flow.host_addr,
            outer=100,
            sequence=0x20,
            acknowledgement=0x130,
            logical=bytes.fromhex("000000000800000001"),
        )
        race_flow._send(
            flow.service,
            flow.host_addr,
            outer=101,
            sequence=0x21,
            acknowledgement=0x131,
            logical=bytes.fromhex("000000000f"),
        )
        race_flow._send(
            flow.service,
            flow.guest_addr,
            outer=102,
            sequence=0x22,
            acknowledgement=0x132,
            logical=bytes.fromhex("000000000f"),
        )
        self.assertEqual(
            flow.service._race[flow.game.gid].phase,
            RacePhase.RACING,
        )

        signal = FinishSignal(0x37A, 1).encode(RACER_FINISHED)
        with self.assertLogs(
            "carbon.rebroadcaster.service",
            level="INFO",
        ) as captured:
            first = race_flow._send(
                flow.service,
                flow.host_addr,
                outer=110,
                sequence=0x30,
                acknowledgement=0x140,
                logical=signal,
            )
        relayed = [
            body
            for body in race_flow._logical_replies(first)
            if len(body) >= 13 and body[4] == RACER_FINISHED
        ]
        self.assertEqual(len(relayed), 2)
        self.assertTrue(all(
            FinishSignal.decode(
                body,
                expected_type=RACER_FINISHED,
            ) == FinishSignal(0x37A, 1)
            for body in relayed
        ))
        tracker = flow.service.race_results.trackers[flow.game.gid]
        self.assertEqual(tracker.finish_signals, {})
        self.assertEqual(tracker.finish_order, [])
        self.assertEqual(tracker.reports, {})
        self.assertFalse(tracker.final_sent)
        self.assertEqual(
            flow.service._race[flow.game.gid].phase,
            RacePhase.RACING,
        )
        self.assertTrue(any(
            "V833 client RacerFinished relayed" in message
            and "authority=notification-only" in message
            for message in captured.output
        ))

        duplicate = race_flow._send(
            flow.service,
            flow.host_addr,
            outer=111,
            sequence=0x31,
            acknowledgement=0x141,
            logical=signal,
        )
        self.assertFalse(any(
            len(body) >= 5 and body[4] == RACER_FINISHED
            for body in race_flow._logical_replies(duplicate)
        ))

    def test_ranked_reports_finalize_when_race_omits_leader_finished(self) -> None:
        flow = race_flow._finalized_flow()
        flow.game.properties["B-U-ranked"] = "1"
        race_flow._advance_to_countdown_expired(flow)
        race_flow._send(
            flow.service,
            flow.host_addr,
            outer=100,
            sequence=0x20,
            acknowledgement=0x130,
            logical=bytes.fromhex("000000000800000001"),
        )
        race_flow._send(
            flow.service,
            flow.host_addr,
            outer=101,
            sequence=0x21,
            acknowledgement=0x131,
            logical=bytes.fromhex("000000000f"),
        )
        race_flow._send(
            flow.service,
            flow.guest_addr,
            outer=102,
            sequence=0x22,
            acknowledgement=0x132,
            logical=bytes.fromhex("000000000f"),
        )
        self.assertEqual(flow.service._race[flow.game.gid].phase, RacePhase.RACING)

        rows = (
            RacerResult(ranking=2, server_car_id=140, race_time=105.0),
            RacerResult(ranking=1, server_car_id=150, race_time=101.0),
        )
        host_report = PlayerGameResults(
            number_of_racers=2,
            reporting_server_car_id=140,
            racers=rows,
        ).encode(GAME_RESULTS)
        guest_report = PlayerGameResults(
            number_of_racers=2,
            reporting_server_car_id=150,
            racers=rows,
        ).encode(GAME_RESULTS)

        race_flow._send(
            flow.service,
            flow.guest_addr,
            outer=110,
            sequence=0x30,
            acknowledgement=0x140,
            logical=FinishSignal(150, 11).encode(RACER_FINISHED),
        )
        first = race_flow._send(
            flow.service,
            flow.host_addr,
            outer=111,
            sequence=0x31,
            acknowledgement=0x141,
            logical=host_report,
        )
        self.assertFalse(any(
            len(body) >= 5 and body[4] == FINAL_GAME_RESULTS
            for body in race_flow._logical_replies(first)
        ))

        with self.assertLogs(
            "carbon.rebroadcaster.service",
            level="INFO",
        ) as captured:
            second = race_flow._send(
                flow.service,
                flow.guest_addr,
                outer=112,
                sequence=0x32,
                acknowledgement=0x142,
                logical=guest_report,
            )

        final_bodies = [
            body
            for body in race_flow._logical_replies(second)
            if len(body) >= 5 and body[4] == FINAL_GAME_RESULTS
        ]
        self.assertEqual(len(final_bodies), 2)
        self.assertEqual(flow.service._race[flow.game.gid].phase, RacePhase.FINISHED)
        self.assertTrue(any(
            "authoritative FinalGameResults sent" in message
            and "authority=unanimous-authenticated-reports" in message
            for message in captured.output
        ))

    def test_unranked_authenticated_reports_send_full_ai_final_results(self) -> None:
        flow = race_flow._finalized_flow()
        race_flow._advance_to_countdown_expired(flow)
        race_flow._send(
            flow.service,
            flow.host_addr,
            outer=100,
            sequence=0x20,
            acknowledgement=0x130,
            logical=bytes.fromhex("000000000800000001"),
        )
        race_flow._send(
            flow.service,
            flow.host_addr,
            outer=101,
            sequence=0x21,
            acknowledgement=0x131,
            logical=bytes.fromhex("000000000f"),
        )
        race_flow._send(
            flow.service,
            flow.guest_addr,
            outer=102,
            sequence=0x22,
            acknowledgement=0x132,
            logical=bytes.fromhex("000000000f"),
        )
        self.assertEqual(flow.service._race[flow.game.gid].phase, RacePhase.RACING)

        rows = (
            RacerResult(ranking=2, server_car_id=100, race_time=105.0),
            RacerResult(ranking=1, server_car_id=110, race_time=101.0),
            RacerResult(ranking=3, server_car_id=101, race_time=106.0),
            RacerResult(ranking=4, server_car_id=102, race_time=107.0),
            RacerResult(ranking=5, server_car_id=103, race_time=108.0),
        )
        host_report = PlayerGameResults(
            number_of_racers=5,
            reporting_server_car_id=100,
            racers=rows,
        ).encode(GAME_RESULTS)
        guest_report = PlayerGameResults(
            number_of_racers=5,
            reporting_server_car_id=110,
            racers=rows,
        ).encode(GAME_RESULTS)

        with self.assertLogs(
            "carbon.rebroadcaster.service",
            level="INFO",
        ) as captured:
            first = race_flow._send(
                flow.service,
                flow.host_addr,
                outer=110,
                sequence=0x30,
                acknowledgement=0x140,
                logical=host_report,
            )
            second = race_flow._send(
                flow.service,
                flow.guest_addr,
                outer=111,
                sequence=0x31,
                acknowledgement=0x141,
                logical=guest_report,
            )

        self.assertNotIn(
            FINAL_GAME_RESULTS,
            [
                body[4]
                for body in race_flow._logical_replies(first)
                if len(body) >= 5
            ],
        )
        final_bodies = [
            body
            for body in race_flow._logical_replies(second)
            if len(body) >= 5 and body[4] == FINAL_GAME_RESULTS
        ]
        self.assertEqual(len(final_bodies), 2)
        for body in final_bodies:
            decoded = PlayerGameResults.decode(body).results
            self.assertEqual(decoded.number_of_racers, 5)
            self.assertEqual(decoded.reporting_server_car_id, 110)
            self.assertEqual(
                [row.server_car_id for row in decoded.racers[:5]],
                [100, 110, 101, 102, 103],
            )
        self.assertEqual(
            flow.service._race[flow.game.gid].phase,
            RacePhase.FINISHED,
        )
        self.assertEqual(
            sum("GameResults captured" in message and "accepted=1" in message for message in captured.output),
            2,
        )
        self.assertTrue(any(
            "authoritative FinalGameResults sent" in message
            and "racers=5" in message
            for message in captured.output
        ))


if __name__ == "__main__":
    unittest.main()
