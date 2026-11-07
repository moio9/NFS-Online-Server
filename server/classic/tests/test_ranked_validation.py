"""Rank eligibility uses server start state and retail room options."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import test_mw_ranking as mw
import test_u2_ranking as u2
from classic.ea.directory import SessionDirectory, SessionState
from classic.ea.ranking import ClassicRankingStore
from classic.lobby.ranking import mw_ranked_flags
from classic.protocols.frame import ClassicEAFrame


class RankedValidationTests(unittest.TestCase):
    def test_native_mw_ranked_flag(self):
        for flags in ("I", "IJKM-", "0x200", "512"):
            self.assertTrue(mw_ranked_flags(flags), flags)
        for flags in ("JKM-", "0", "", "garbage", "-1"):
            self.assertFalse(mw_ranked_flags(flags), flags)

    def test_start_snapshot_and_authenticated_participation(self):
        sessions = SessionDirectory()
        game = sessions.create_game(2, 1, custflags="I")
        sessions.join_game(game.game_id, 2)
        self.assertEqual(sessions.record_result(game.game_id, 1, {}), (False, False))
        sessions.set_state(game.game_id, SessionState.ACTIVE)
        game.custflags = "JKM-"
        game.room_id = 9
        sessions.set_state(game.game_id, SessionState.ACTIVE)
        self.assertEqual(game.started_custflags, "I")
        self.assertEqual(game.started_room_id, 2)
        self.assertEqual(game.started_participants, {1, 2})
        self.assertEqual(sessions.record_result(game.game_id, 3, {}), (False, False))
        self.assertEqual(sessions.record_result(game.game_id, 1, {}), (True, False))
        self.assertEqual(sessions.record_result(game.game_id, 1, {}), (False, False))
        self.assertEqual(sessions.record_result(game.game_id, 2, {}), (True, True))

    def test_mw_report_eligibility_and_invalid_resu_cannot_fallback(self):
        cases = (
            ("I", True, True, False, 1, 0),
            ("JKM-", True, True, False, 1, 0),
            ("I", False, True, False, 0, 0),
            ("I", True, False, False, 1, 0),
            ("I", True, True, True, 0, 0),
        )
        for flags, started, multiplayer, invalid, recorded, wins in cases:
            with self.subTest(case=(flags, started, multiplayer, invalid)), TemporaryDirectory() as tmp:
                root = Path(tmp)
                service, sessions, ranking, context = mw.MostWantedReportTests()._service(root)
                uid = context.auth.identity.user_id
                game = sessions.create_game(0, uid, custflags=flags, host_persona="Driver")
                if multiplayer:
                    sessions.join_game(game.game_id, uid + 100, persona="Opponent")
                context.lobby_game_id = game.game_id
                if started:
                    sessions.set_state(game.game_id, SessionState.ACTIVE)
                # Reports cannot reclassify the race through later room edits.
                game.custflags = "JKM-" if flags == "I" else "I"
                fields = [("OUTCOME", "WIN"), ("CATEGORY", 2)]
                if invalid:
                    fields.append(("RESU", "invalid"))
                reply = service.dispatch(ClassicEAFrame.from_fields("rank", fields), context)
                response, _ = ClassicEAFrame.decode_one(reply.frames[0])
                self.assertEqual(int(response.fields()["RECORDED"]), recorded)
                self.assertEqual(ranking.summary("most_wanted", "Driver", 1)["wins"], wins)
                reload = ClassicRankingStore(root / "stats.json")
                self.assertEqual(reload.summary("most_wanted", "Driver", 1)["wins"], wins)

    def test_u2_room_eligibility_is_frozen_at_start(self):
        with TemporaryDirectory() as tmp:
            service, sessions, _, _ = u2.Underground2RankingTests()._service(Path(tmp))
            for room_id, multiplayer, expected in ((2, True, True), (6, True, False), (2, False, False)):
                game = sessions.create_game(room_id, 1)
                if multiplayer:
                    sessions.join_game(game.game_id, 2)
                self.assertFalse(service._race_is_ranked(game))
                sessions.set_state(game.game_id, SessionState.ACTIVE)
                game.room_id = 9 if room_id == 2 else 2
                self.assertEqual(service._race_is_ranked(game), expected)

    def test_json_unranked_result_does_not_change_existing_stats(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "stats.json"
            store = ClassicRankingStore(path)
            for game in ("most_wanted", "underground2"):
                store.record_result(game, "Driver", outcome="WIN")
                before = store.summary(game, "Driver")
                store.record_result(game, "Driver", outcome="DISCONNECT", race_metadata={"ranked": False})
                self.assertEqual(store.summary(game, "Driver"), before)
                self.assertEqual(ClassicRankingStore(path).summary(game, "Driver"), before)

    def test_u2_unranked_report_completes_without_awarding_points(self):
        with TemporaryDirectory() as tmp:
            fixture = u2.Underground2RankingTests()
            service, sessions, ranking, make_context = fixture._service(Path(tmp))
            host = make_context("HostAccount", "Host", "host")
            guest = make_context("GuestAccount", "Guest", "guest")
            uid = host.auth.identity.user_id
            game = sessions.create_game(6, uid, host_persona="Host")
            sessions.join_game(game.game_id, guest.auth.identity.user_id, persona="Guest")
            sessions.set_state(game.game_id, SessionState.ACTIVE)
            service._u2_pending_games[uid] = game.game_id
            request = ClassicEAFrame.from_fields("rank", (
                ("REPT", "Host"), ("NAME0", "Host"), ("NAME1", "Guest"),
                ("RESU", fixture._resu(reporter=0)),
            ))
            reply = service.dispatch(request, host)
            self.assertEqual(reply.reason, "game_report")
            self.assertIn(uid, game.reported_participants)
            self.assertFalse(game.results[uid]["ranked"])
            self.assertEqual(ranking.summary("underground2", "Host", 1)["wins"], 0)

    def test_mw_owner_option_updates_reach_start_snapshot(self):
        with TemporaryDirectory() as tmp:
            service, sessions, _, context = mw.MostWantedReportTests()._service(Path(tmp))
            service.dispatch(ClassicEAFrame.from_fields("ucre", (("NAME", "Room"),)), context)
            service.dispatch(ClassicEAFrame.from_fields("gcre", (("NAME", "Race"),)), context)
            game = sessions.get_game(context.lobby_game_id)
            service.dispatch(ClassicEAFrame.from_fields("uadm", (("CUSTFLAGS", "IJKM-"),)), context)
            self.assertEqual(game.custflags, "IJKM-")
            sessions.join_game(game.game_id, context.auth.identity.user_id + 100, persona="Opponent")
            sessions.set_state(game.game_id, SessionState.ACTIVE)
            service.dispatch(ClassicEAFrame.from_fields("gset", (("CUSTFLAGS", "JKM-"),)), context)
            self.assertEqual(game.custflags, "JKM-")
            self.assertTrue(service._race_is_ranked(game))
