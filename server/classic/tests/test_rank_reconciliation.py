"""Contradictory reports must not produce partial or duplicate ranked awards."""
import base64
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import unquote

import test_mw_ranking as mw
import test_u2_ranking as u2
import test_sqlite_ranking as sql
from classic.ea.directory import SessionState
from classic.ea.sqlite_ranking import SQLiteClassicRankingStore
from classic.protocols.frame import ClassicEAFrame
from classic.protocols.ranking_codec import decode_mw_rank_result


class ReconciliationTests(unittest.TestCase):
    def test_u2_conflicting_reports_are_quarantined_before_any_award(self):
        for conflict, backend in ((c, b) for c in ("place", "track", "category", None) for b in ("json", "sqlite")):
            with self.subTest(conflict=conflict, backend=backend), TemporaryDirectory() as tmp:
                fixture = u2.Underground2RankingTests()
                service, sessions, ranking, context = fixture._service(Path(tmp))
                if backend == "sqlite":
                    sql_fixture = sql.SQLiteClassicRankingStoreTests()
                    database = sql_fixture._database(Path(tmp))
                    for name in ("Host", "Guest"):
                        sql_fixture._account(database, name)
                    ranking = service.ranking = SQLiteClassicRankingStore(database)
                host = context("HostAccount", "Host", "host")
                guest = context("GuestAccount", "Guest", "guest")
                uid, gid = host.auth.identity.user_id, guest.auth.identity.user_id
                game = sessions.create_game(2, uid, host_persona="Host")
                sessions.join_game(game.game_id, gid, persona="Guest")
                sessions.set_state(game.game_id, SessionState.ACTIVE)
                service._u2_pending_games.update({uid: game.game_id, gid: game.game_id})
                def request(reporter):
                    raw = bytearray(base64.b64decode(fixture._resu(reporter=reporter)))
                    if reporter == 1:
                        if conflict == "place":
                            raw[9], raw[83] = 2, 1
                        elif conflict == "track":
                            raw[4] ^= 1
                        elif conflict == "category":
                            raw[1] = 0
                    return ClassicEAFrame.from_fields("rank", (
                        ("REPT", "Host" if reporter == 0 else "Guest"),
                        ("NAME0", "Host"), ("NAME1", "Guest"),
                        ("RESU", base64.b64encode(raw).decode()),
                    ))
                service.dispatch(request(0), host)
                self.assertEqual(ranking.summary("underground2", "Host", 1)["wins"], 0)
                service.dispatch(request(1), guest)
                self.assertTrue(game.ranking_committed)
                self.assertEqual(game.ranking_disputed, conflict is not None)
                self.assertEqual(ranking.summary("underground2", "Host", 1)["wins"], int(conflict is None))
                service.dispatch(request(1), guest)
                self.assertEqual(ranking.summary("underground2", "Guest", 1)["losses"], int(conflict is None))

    def test_mw_decoder_keeps_name_mapped_claims_after_lobby_departure(self):
        with TemporaryDirectory() as tmp:
            service, sessions, _, context = mw.MostWantedReportTests()._service(Path(tmp))
            uid = context.auth.identity.user_id
            game = sessions.create_game(0, uid, host_persona="Driver")
            sessions.join_game(game.game_id, uid+1, persona="Opponent")
            sessions.set_state(game.game_id, SessionState.ACTIVE)
            sessions.leave_game(game.game_id, uid+1)
            fields = dict(REPT="Driver", NAME0="Opponent", NAME1="Driver",
                          RESU=mw.MostWantedReportTests.DISCONNECT_SURVIVOR_RESU)
            decoded = decode_mw_rank_result(fields, "Driver", game)
            self.assertIsNotNone(decoded)
            self.assertEqual(decoded.placements, (("driver", 1), ("opponent", 2)))

    def test_sqlite_pending_reports_survive_reload_and_commit_once(self):
        with TemporaryDirectory() as tmp:
            fixture = sql.SQLiteClassicRankingStoreTests()
            db = fixture._database(Path(tmp))
            for name in ("Winner", "Loser"):
                fixture._account(db, name)
            reports = [
                dict(persona=name, outcome=outcome, category_index=1, race_key="race",
                     reporter_key=name, race_metadata={"server_run_id":"stable", "ranked":True, "status":"reported", "defer_aggregate":True},
                     result_metadata={"place":place})
                for name, outcome, place in (("Winner","WIN",1), ("Loser","LOSS",2))
            ]
            store = SQLiteClassicRankingStore(db)
            for row in reports:
                store.record_result("most_wanted", **row)
            self.assertEqual(store.summary("most_wanted", "Winner", 1)["wins"], 0)
            store = SQLiteClassicRankingStore(db)
            for row in reports:
                row["race_metadata"].update(status="complete", defer_aggregate=False)
            for _ in range(2):
                store.record_race("most_wanted", reports)
            self.assertEqual(store.summary("most_wanted", "Winner", 1)["wins"], 1)
            self.assertEqual(store.summary("most_wanted", "Loser", 1)["losses"], 1)

    def test_sqlite_batch_rolls_back_all_awards_on_second_write_failure(self):
        with TemporaryDirectory() as tmp:
            fixture = sql.SQLiteClassicRankingStoreTests()
            db = fixture._database(Path(tmp))
            for name in ("First", "Second"):
                fixture._account(db, name)
            second_id = db.identity_for_persona("Second").persona_id
            with db.transaction() as conn:
                conn.execute(f"""CREATE TRIGGER fail_second BEFORE UPDATE ON game_player_stats
                    WHEN NEW.persona_id={int(second_id)} AND NEW.losses > OLD.losses
                    BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END""")
            store = SQLiteClassicRankingStore(db)
            reports = [
                dict(persona=name, outcome=outcome, category_index=1, race_key="race",
                     reporter_key=name, race_metadata={"ranked":True,"status":"complete"},
                     result_metadata={"place":place})
                for name,outcome,place in (("First","WIN",1),("Second","LOSS",2))
            ]
            import sqlite3
            with self.assertRaises(sqlite3.IntegrityError):
                store.record_race("most_wanted", reports)
            self.assertEqual(store.summary("most_wanted", "First", 1)["wins"], 0)
            with db.connect() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM game_races").fetchone()[0], 0)

    def test_mw_owner_disconnect_keeps_late_guest_report_and_awards_dnf_once(self):
        from classic.protocols.prelogin import ClassicPreloginProfile
        with TemporaryDirectory() as tmp:
            service, sessions, ranking, context = u2.Underground2RankingTests()._service(
                Path(tmp), profile=ClassicPreloginProfile(game_id="most_wanted"))
            host = context("HostAccount", "Host", "host")
            guest = context("GuestAccount", "Guest", "guest")
            uid, gid = host.auth.identity.user_id, guest.auth.identity.user_id
            game = sessions.create_game(0, uid, host_persona="Host", custflags="I")
            sessions.join_game(game.game_id, gid, persona="Guest")
            sessions.set_state(game.game_id, SessionState.ACTIVE)
            host.lobby_game_id = guest.lobby_game_id = game.game_id
            service._pending_rank_games.update({uid:game, gid:game})
            service._register(guest)
            service.release(host)
            self.assertIsNone(sessions.get_game(game.game_id))
            self.assertEqual(game.rank_departed, {uid})
            self.assertEqual(ranking.summary("most_wanted", "Host", 3)["disconnects"], 0)
            request = ClassicEAFrame.from_fields("rank", (
                ("REPT", "Guest"), ("NAME0", "Host"), ("NAME1", "Guest"),
                ("RESU", mw.MostWantedReportTests.DISCONNECT_SURVIVOR_RESU)))
            reply = service.dispatch(request, guest)
            self.assertEqual([ClassicEAFrame.decode_one(row)[0].command for row in reply.frames], ["rank"])
            self.assertEqual(ranking.summary("most_wanted", "Host", 3)["disconnects"], 1)
            self.assertEqual(ranking.summary("most_wanted", "Guest", 3)["wins"], 1)
            service.dispatch(request, guest)
            self.assertEqual(ranking.summary("most_wanted", "Host", 3)["disconnects"], 1)

    def test_u2_passive_lobby_close_is_not_a_disconnect_but_new_race_is_terminal(self):
        with TemporaryDirectory() as tmp:
            fixture = u2.Underground2RankingTests()
            service, sessions, ranking, context = fixture._service(Path(tmp))
            host = context("HostAccount", "Host", "host")
            guest = context("GuestAccount", "Guest", "guest")
            uid, gid = host.auth.identity.user_id, guest.auth.identity.user_id
            game = sessions.create_game(2, uid, host_persona="Host")
            sessions.join_game(game.game_id, gid, persona="Guest")
            sessions.set_state(game.game_id, SessionState.ACTIVE)
            host.lobby_game_id = guest.lobby_game_id = game.game_id
            service._pending_rank_games.update({uid:game,gid:game})
            service._u2_pending_games.update({uid:game.game_id,gid:game.game_id})
            service.release(host)
            self.assertFalse(game.rank_departed)
            # Reconnecting and explicitly creating another race is different.
            returning = context("HostAccount", "Host", "return")
            service.dispatch(ClassicEAFrame.from_fields("gcre", (("NAME","Next"),)), returning)
            self.assertEqual(game.rank_departed, {uid})
            request = ClassicEAFrame.from_fields("rank", (
                ("REPT","Guest"), ("NAME0","Host"), ("NAME1","Guest"),
                ("RESU",fixture._resu(reporter=1))))
            service.dispatch(request, guest)
            self.assertEqual(ranking.summary("underground2","Host",1)["disconnects"],1)
            self.assertEqual(ranking.summary("underground2","Guest",1)["losses"],1)
            self.assertTrue(game.ranking_committed)

    def test_all_clients_leave_without_evidence_does_not_invent_results(self):
        with TemporaryDirectory() as tmp:
            service,sessions,ranking,context = mw.MostWantedReportTests()._service(Path(tmp))
            uid=context.auth.identity.user_id
            game=sessions.create_game(0,uid,host_persona="Driver",custflags="I")
            sessions.join_game(game.game_id,uid+1,persona="Other")
            sessions.set_state(game.game_id,SessionState.ACTIVE)
            service._pending_rank_games.update({uid:game,uid+1:game})
            service._depart_rank_race(uid)
            service._depart_rank_race(uid+1)
            self.assertFalse(service._pending_rank_games)
            self.assertFalse(game.results)
            self.assertEqual(ranking.summary("most_wanted","Driver",0)["disconnects"],0)

    def test_changed_staged_telemetry_is_disputed_not_promoted(self):
        with TemporaryDirectory() as tmp:
            fixture = sql.SQLiteClassicRankingStoreTests()
            db = fixture._database(Path(tmp))
            fixture._account(db, "Driver")
            store = SQLiteClassicRankingStore(db)
            row = dict(persona="Driver", outcome="WIN", category_index=1,
                       race_key="one", reporter_key="driver", nos_used=2.0,
                       race_metadata={"ranked": True, "defer_aggregate": True},
                       result_metadata={"place":1, "elapsed_ms":1000})
            store.record_result("most_wanted", **row)
            row["nos_used"] = 100.0
            row["race_metadata"] = {"ranked":True,"status":"complete"}
            store.record_race("most_wanted", [row])
            self.assertEqual(store.summary("most_wanted","Driver",1)["wins"],0)
            with db.connect() as conn:
                self.assertEqual(conn.execute("SELECT status FROM game_races").fetchone()[0],"disputed")

    def test_failed_final_batch_can_be_retried_by_duplicate_report(self):
        from unittest.mock import patch
        with TemporaryDirectory() as tmp:
            fixture=u2.Underground2RankingTests()
            service,sessions,ranking,context=fixture._service(Path(tmp))
            host=context("HostAccount","Host","host")
            guest=context("GuestAccount","Guest","guest")
            uid,gid=host.auth.identity.user_id,guest.auth.identity.user_id
            game=sessions.create_game(2,uid,host_persona="Host")
            sessions.join_game(game.game_id,gid,persona="Guest")
            sessions.set_state(game.game_id,SessionState.ACTIVE)
            service._u2_pending_games.update({uid:game.game_id,gid:game.game_id})
            def report(index):
                return ClassicEAFrame.from_fields("rank", (
                    ("REPT","Host" if index==0 else "Guest"),("NAME0","Host"),("NAME1","Guest"),
                    ("RESU",fixture._resu(reporter=index))))
            service.dispatch(report(0),host)
            with patch.object(ranking,"record_race",side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    service.dispatch(report(1),guest)
            self.assertFalse(game.ranking_committed)
            self.assertEqual(ranking.summary("underground2","Host",1)["wins"],0)
            service.dispatch(report(1),guest)
            self.assertTrue(game.ranking_committed)
            self.assertEqual(ranking.summary("underground2","Host",1)["wins"],1)
