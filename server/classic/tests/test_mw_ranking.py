from __future__ import annotations

import base64
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from urllib.parse import unquote

from classic.accounts.credentials import CredentialStore
from classic.accounts.identity import IdentityStore
from classic.core.config import Endpoint
from classic.ea.directory import SessionDirectory, SessionState
from classic.ea.ranking import ClassicPlayerStats, ClassicRankingStore
from classic.games.most_wanted.auth import create_auth_service
from classic.protocols.auth import ClassicAuthContext
from classic.protocols.frame import ClassicEAFrame
from classic.protocols.prelogin import (
    ClassicPreloginContext,
    ClassicPreloginProfile,
    ClassicPreloginService,
)
from classic.protocols.ranking_codec import (
    decode_mw_rank_result,
    mw_rank_payload_trace,
)


class ClassicRankingStoreTests(unittest.TestCase):
    def test_mw_profile_csv_uses_native_44_value_mode_blocks(self) -> None:
        with TemporaryDirectory() as temporary:
            store = ClassicRankingStore(Path(temporary) / "stats.json")
            store.record_result(
                "most_wanted",
                "Driver",
                category_index=2,
                outcome="WIN",
                nos_used=43.266,
            )
            profile = store.profile_hex_csv("most_wanted", "Driver")
            profile_values = profile.split(",")

            self.assertTrue(profile.endswith(","))
            self.assertEqual(len(profile_values), 45)
            self.assertEqual(profile_values[20], "c8")
            self.assertEqual(profile_values[21], "1")
            self.assertEqual(profile_values[22], "0")
            self.assertEqual(profile_values[27], "2b")
            self.assertEqual(profile_values[31], "0")
            self.assertEqual(profile_values[40], "0")
            self.assertEqual(profile_values[44], "")

            drag = ClassicPlayerStats.create("Driver")
            drag.set(3, "rep", 67)
            self.assertEqual(drag.mw_snap_hex_csv(3, 1).split(",")[0], "1")
            personal_values = drag.mw_personal_hex_csv(3).split(",")
            self.assertEqual(len(personal_values), 35)
            self.assertEqual(personal_values[21], "43")
            self.assertEqual(personal_values[25], "43")
            self.assertEqual(drag.mw_profile_hex_csv().split(",")[32], "43")
            self.assertEqual(drag.mw_profile_hex_csv().split(",")[40], "0")

    def test_mw_index_groups_ignore_callback_channel(self) -> None:
        self.assertEqual(ClassicPreloginService._mw_snap_stats_board(4), 2)
        self.assertEqual(ClassicPreloginService._mw_snap_stats_board(8), 3)
        self.assertEqual(ClassicPreloginService._mw_snap_stats_board(12), 4)
        self.assertEqual(ClassicPreloginService._mw_snap_stats_board(27), 2)
        self.assertEqual(ClassicPreloginService._mw_snap_stats_board(39), 4)

    def test_results_persist_and_rebuild_leaderboard(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "stats.json"
            hidden: set[str] = set()
            store = ClassicRankingStore(
                path,
                persona_visible=lambda persona: persona.casefold() not in hidden,
            )
            store.record_result(
                "most_wanted",
                "Winner",
                category_index=1,
                outcome="WIN",
                opponent_personas=("Loser",),
            )
            store.record_result(
                "most_wanted",
                "Loser",
                category_index=1,
                outcome="LOSS",
                opponent_personas=("Winner",),
            )
            board = store.leaderboard("most_wanted", 1, limit=10)
            self.assertEqual([row.persona for row in board], ["Winner", "Loser"])
            self.assertEqual(store.summary("most_wanted", "Winner", 1)["wins"], 1)
            self.assertEqual(store.summary("most_wanted", "Loser", 1)["losses"], 1)

            hidden.add("winner")
            self.assertEqual(
                [row.persona for row in store.leaderboard("most_wanted", 1)],
                ["Loser"],
            )
            store.record_result(
                "most_wanted",
                "Winner",
                category_index=1,
                outcome="WIN",
            )
            self.assertEqual(store.summary("most_wanted", "Winner", 1)["wins"], 1)

            reloaded = ClassicRankingStore(path)
            self.assertEqual(reloaded.summary("most_wanted", "Winner", 1)["rank"], 1)
            self.assertEqual(reloaded.summary("most_wanted", "Loser", 1)["rank"], 2)

    def test_mw_nos_persists_accumulates_and_resets_per_mode(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "stats.json"
            store = ClassicRankingStore(path)
            store.record_result(
                "most_wanted",
                "Driver",
                category_index=2,
                outcome="WIN",
                nos_used=43.266,
            )
            store.record_result(
                "most_wanted",
                "Driver",
                category_index=2,
                outcome="LOSS",
                nos_used=0.0,
            )

            reloaded = ClassicRankingStore(path)
            self.assertAlmostEqual(
                reloaded.summary("most_wanted", "Driver", 2)["nos_used"],
                43.266,
                places=3,
            )
            self.assertAlmostEqual(
                reloaded.summary("most_wanted", "Driver", 0)["nos_used"],
                43.266,
                places=3,
            )
            self.assertEqual(
                reloaded.profile_hex_csv("most_wanted", "Driver").split(",")[27],
                "2b",
            )

            reloaded.reset("most_wanted", "Driver", 2)
            reset = ClassicRankingStore(path)
            self.assertEqual(
                reset.summary("most_wanted", "Driver", 2)["nos_used"],
                0.0,
            )

    def test_running_store_hot_reloads_external_admin_edit(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "stats.json"
            running = ClassicRankingStore(path)
            running.get_or_create("most_wanted", "Driver")

            administrator = ClassicRankingStore(path)
            administrator.update_fields(
                "most_wanted",
                "Driver",
                1,
                {"wins": 12, "losses": 3, "rep": 2400},
            )

            current = running.summary("most_wanted", "Driver", 1)
            self.assertEqual(current["wins"], 12)
            self.assertEqual(current["losses"], 3)
            self.assertEqual(current["rep"], 2400)

    def test_admin_adjust_reset_and_delete_are_persistent(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "stats.json"
            store = ClassicRankingStore(path)
            store.update_fields(
                "most_wanted",
                "Driver",
                2,
                {"wins": 5, "disconnects": 2, "rep": 900},
            )
            store.update_fields(
                "most_wanted",
                "Driver",
                2,
                {"wins": -2, "disconnects": -1, "rep": 100},
                relative=True,
            )
            adjusted = store.summary("most_wanted", "Driver", 2)
            self.assertEqual(adjusted["wins"], 3)
            self.assertEqual(adjusted["disconnects"], 1)
            self.assertEqual(adjusted["rep"], 1000)

            store.reset("most_wanted", "Driver", 2)
            reset = ClassicRankingStore(path).summary("most_wanted", "Driver", 2)
            self.assertEqual(reset["wins"], 0)
            self.assertEqual(reset["disconnects"], 0)
            self.assertEqual(reset["rep"], 100)

            self.assertTrue(store.delete("most_wanted", "Driver"))
            self.assertFalse(store.delete("most_wanted", "Driver"))


class MostWantedReportTests(unittest.TestCase):
    NORMAL_HOST_RESU = (
        "AwICAAfQAAEBAgAAAAAAAEBrt1wAAAAAQY0oq0HIj2sAAAAAAAAAAAAAAAAAAAAAAAAAAEELd6AAAAAAAQwAAAAAAEGadFoAAAAAP0kGCEFEl/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAA%3d"
    )
    NORMAL_GUEST_RESU_WITH_TRAILER = (
        "AwICAQfQAAEBAgAAAAAAAEBrt1wAAAAAQY0oq0HIj2sAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAEELd6AAAAAAAQAAAAAAAECF2+wAAAAAOwsfDT1m/5wAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAABAAIABw%3d%3d"
    )
    DISCONNECT_SURVIVOR_RESU = (
        "AwICAQfQAAEBAQwAAAAAAEEhyIYAAAAAOoGQUT2PDgEAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAACAAAAAgwAAAAAAEEhyIYAAAAAOobssD1nQB8AAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAA%3d"
    )
    SPRINT_INTER_RECORD_GAP_RESU = (
        "AwIBAQfQAAEAAgAAP4AAAELEVHsAAAAAQnapGELJ7AJAc5IQQTB1rAAAAAAA"
        "AAAAAAAAAEItEJEAAAIAAgEBAAA/gAAAQrluw0K5bsNCga74QqdxdUALvPBB"
        "GpdAAAAAAAAAAAAAAAAAAAAAAAAAAA%3d%3d"
    )

    def _service(self, root: Path):
        credentials = CredentialStore(root / "auth.json")
        credentials.create_account("Account", "password", persona="Driver")
        identities = IdentityStore(token_factory=lambda: "token")
        auth = create_auth_service(credentials, identities, verify_passwords=False)
        sessions = SessionDirectory()
        ranking = ClassicRankingStore(root / "stats.json")
        service = ClassicPreloginService(
            auth,
            profile=ClassicPreloginProfile(game_id="most_wanted"),
            control_endpoint=Endpoint("127.0.0.1", 13505),
            sessions=sessions,
            ranking=ranking,
        )
        account = credentials.resolve_account("Account")
        identity, token = identities.login("Account", "Driver")
        auth_context = ClassicAuthContext(
            connection_id="mw-test",
            account=account,
            identity=identity,
            session_token=token,
            lkey=token,
            persona="Driver",
        )
        context = ClassicPreloginContext(
            auth=auth_context,
            authenticated=True,
            persona_selected=True,
            client_address="127.0.0.1",
        )
        return service, sessions, ranking, context

    def test_drag_personal_stats_uses_full_channel_10_snapshot(self) -> None:
        with TemporaryDirectory() as temporary:
            service, _sessions, ranking, context = self._service(Path(temporary))
            ranking.record_result(
                "most_wanted",
                "Driver",
                category_index=3,
                outcome="LOSS",
            )

            frames = service._snap_frames(
                {
                    "INDEX": "12",
                    "CHAN": "10",
                    "START": "0",
                    "RANGE": "1",
                    "FIND": "$",
                },
                context,
            )
            header, row = [ClassicEAFrame.decode_one(frame)[0] for frame in frames]
            fields = row.fields()
            header_fields = header.fields()

            self.assertEqual(header.command, "snap")
            self.assertEqual(header_fields["RANGE"], "1")
            self.assertEqual(row.command, "+snp")
            self.assertEqual(fields["P"], "0,1,1")
            self.assertNotIn("R", fields)
            self.assertEqual(len(fields["S"].split(",")), 35)
            self.assertEqual(fields["S"].split(",")[21], "5f")
            self.assertEqual(fields["S"].split(",")[25], "5f")
            self.assertEqual(header_fields["SEQN"], "0")

    def test_drag_rating_uses_native_seven_long_channel_7_row(self) -> None:
        with TemporaryDirectory() as temporary:
            service, _sessions, ranking, context = self._service(Path(temporary))
            ranking.record_result(
                "most_wanted",
                "Driver",
                category_index=3,
                outcome="LOSS",
            )

            frames = service._snap_frames(
                {
                    "INDEX": "12",
                    "CHAN": "7",
                    "START": "0",
                    "RANGE": "100",
                },
                context,
            )
            header = ClassicEAFrame.decode_one(frames[0])[0]
            row = ClassicEAFrame.decode_one(frames[1])[0]
            fields = row.fields()
            header_fields = header.fields()

            values = fields["S"].split(",")

            self.assertEqual(header.command, "snap")
            self.assertEqual(header_fields["RANGE"], "1")
            self.assertEqual(row.command, "+snp")
            self.assertEqual(fields["P"], "0")
            self.assertNotIn("R", fields)
            self.assertEqual(len(values), 7)
            self.assertEqual(values[0], "1")
            self.assertEqual(values[4], "5f")
            self.assertEqual(header_fields["SEQN"], "0")

    def test_leaderboard_header_advertises_actual_row_count(self) -> None:
        with TemporaryDirectory() as temporary:
            service, _sessions, ranking, context = self._service(Path(temporary))
            ranking.get_or_create("most_wanted", "Opponent")
            ranking.update_fields(
                "most_wanted",
                "Opponent",
                1,
                {"wins": 4, "losses": 1, "rep": 800},
            )

            frames = service._snap_frames(
                {
                    "INDEX": "4",
                    "CHAN": "5",
                    "START": "0",
                    "RANGE": "100",
                },
                context,
            )
            decoded = [ClassicEAFrame.decode_one(frame)[0] for frame in frames]
            header = decoded[0]
            rows = decoded[1:]
            header_fields = header.fields()

            self.assertEqual(header.command, "snap")
            self.assertEqual(header_fields["INDEX"], "4")
            self.assertEqual(header_fields["CHAN"], "5")
            self.assertEqual(header_fields["RANGE"], "2")
            self.assertEqual([row.command for row in rows], ["+snp", "+snp"])
            self.assertTrue(all(len(row.fields()["S"].split(",")) == 7 for row in rows))
            self.assertEqual(
                [row.fields()["S"].split(",")[0] for row in rows],
                ["1", "2"],
            )
            self.assertEqual(header_fields["SEQN"], "0")

    def test_rank_report_is_idempotent_and_finishes_game(self) -> None:
        with TemporaryDirectory() as temporary:
            service, sessions, ranking, context = self._service(Path(temporary))
            identity = context.auth.identity
            self.assertIsNotNone(identity)
            game = sessions.create_game(
                0,
                identity.user_id,
                capacity=1,
                min_players=1,
                host_persona="Driver",
                host_address="127.0.0.1",
            )
            context.lobby_game_id = game.game_id
            sessions.set_state(game.game_id, SessionState.ACTIVE)
            record_result = Mock(wraps=ranking.record_result)
            ranking.record_result = record_result

            request = ClassicEAFrame.from_fields(
                "RANK",
                (("OUTCOME", "WIN"), ("CATEGORY", 2), ("TIME", 90)),
                reserved=0x1234,
            )
            first = service.dispatch(request, context)
            self.assertEqual(first.reason, "game_report")
            self.assertEqual(first.frames[0][0:4], b"RANK")
            decoded, trailing = ClassicEAFrame.decode_one(first.frames[0])
            self.assertFalse(trailing)
            self.assertEqual(decoded.reserved, 0x1234)
            self.assertEqual(decoded.fields()["RECORDED"], "1")
            self.assertEqual(decoded.fields()["COMPLETE"], "1")
            self.assertEqual(sessions.get_game(game.game_id).state, SessionState.FINISHED)
            self.assertEqual(ranking.summary("most_wanted", "Driver", 1)["wins"], 1)
            recorded = record_result.call_args.kwargs
            self.assertEqual(recorded["race_key"], str(game.game_id))
            self.assertEqual(recorded["reporter_key"], identity.user_id)
            self.assertEqual(recorded["result_metadata"]["elapsed_ms"], 90_000)
            self.assertEqual(recorded["result_metadata"]["source"], "fields")

            second = service.dispatch(request, context)
            decoded_again, _ = ClassicEAFrame.decode_one(second.frames[0])
            self.assertEqual(decoded_again.fields()["RECORDED"], "0")
            self.assertEqual(ranking.summary("most_wanted", "Driver", 1)["wins"], 1)

    def test_stock_resu_records_reporter_with_reversed_record_order(self) -> None:
        with TemporaryDirectory() as temporary:
            service, sessions, ranking, context = self._service(Path(temporary))
            identity = context.auth.identity
            self.assertIsNotNone(identity)
            opponent_id = identity.user_id + 100
            game = sessions.create_game(
                0,
                opponent_id,
                capacity=2,
                min_players=2,
                host_persona="Opponent",
                host_address="127.0.0.2",
            )
            self.assertTrue(
                sessions.join_game(
                    game.game_id,
                    identity.user_id,
                    persona="Driver",
                    address="127.0.0.1",
                )
            )
            context.lobby_game_id = game.game_id
            sessions.set_state(game.game_id, SessionState.ACTIVE)

            request = ClassicEAFrame.from_fields(
                "rank",
                (
                    ("REPT", "Driver"),
                    ("RESU", self.DISCONNECT_SURVIVOR_RESU),
                    ("NAME0", "Opponent"),
                    ("NAME1", "Driver"),
                ),
            )
            reply = service.dispatch(request, context)
            decoded, _ = ClassicEAFrame.decode_one(reply.frames[0])
            self.assertEqual(decoded.fields()["RECORDED"], "1")
            self.assertEqual(decoded.fields()["COMPLETE"], "0")
            stored = sessions.get_game(game.game_id).results[identity.user_id]
            self.assertEqual(stored["source"], "mw_resu")
            self.assertEqual(stored["outcome"], "WIN")
            self.assertEqual(stored["place"], 1)
            self.assertEqual(stored["category"], 3)
            self.assertEqual(stored["time"], 10)
            self.assertEqual(ranking.summary("most_wanted", "Driver", 3)["wins"], 1)

    def test_stock_normal_resu_accepts_both_record_orders_and_trailer(self) -> None:
        sessions = SessionDirectory()
        game = sessions.create_game(
            0,
            10,
            capacity=2,
            min_players=2,
            host_persona="Driver",
        )
        sessions.join_game(game.game_id, 20, persona="Opponent")
        common_names = {"NAME0": "Driver", "NAME1": "Opponent"}

        host = decode_mw_rank_result(
            {
                "REPT": "Driver",
                "RESU": self.NORMAL_HOST_RESU,
                **common_names,
            },
            "Driver",
            game,
        )
        guest = decode_mw_rank_result(
            {
                "REPT": "Opponent",
                "RESU": self.NORMAL_GUEST_RESU_WITH_TRAILER,
                **common_names,
            },
            "Opponent",
            game,
        )

        self.assertIsNotNone(host)
        self.assertIsNotNone(guest)
        self.assertEqual((host.reporter_index, host.place, host.flags), (0, 1, 12))
        self.assertEqual((guest.reporter_index, guest.place, guest.flags), (1, 2, 0))
        self.assertEqual((host.category, guest.category), (3, 3))

    def test_stock_resu_zero_based_modes_follow_total_slot(self) -> None:
        sessions = SessionDirectory()
        game = sessions.create_game(
            0,
            10,
            capacity=2,
            min_players=2,
            host_persona="Driver",
        )
        sessions.join_game(game.game_id, 20, persona="Opponent")
        common_fields = {
            "REPT": "Driver",
            "NAME0": "Driver",
            "NAME1": "Opponent",
        }
        stock_payload = bytearray(
            base64.b64decode(unquote(self.NORMAL_HOST_RESU))
        )

        for raw_category, durable_category in (
            (0, 1),
            (1, 2),
            (2, 3),
            (3, 4),
            (4, 0),
        ):
            with self.subTest(raw_category=raw_category):
                payload = bytearray(stock_payload)
                payload[2] = raw_category
                result = decode_mw_rank_result(
                    {
                        **common_fields,
                        "RESU": base64.b64encode(payload).decode("ascii"),
                    },
                    "Driver",
                    game,
                )
                self.assertIsNotNone(result)
                self.assertEqual(result.category, durable_category)

    def test_rejected_resu_trace_preserves_structural_evidence(self) -> None:
        trace = mw_rank_payload_trace(
            {"RESU": self.NORMAL_GUEST_RESU_WITH_TRAILER}
        )
        self.assertIn("decoded=112", trace)
        self.assertIn("header=0302020107d00001", trace)
        self.assertIn("record_heads=0:010200", trace)
        self.assertIn("trailer=0007", trace)

    def test_stock_resu_accepts_two_byte_inter_record_extension(self) -> None:
        sessions = SessionDirectory()
        game = sessions.create_game(
            0,
            10,
            capacity=2,
            min_players=2,
            host_persona="driver",
        )
        sessions.join_game(game.game_id, 20, persona="test")
        result = decode_mw_rank_result(
            {
                "REPT": "test",
                "RESU": self.SPRINT_INTER_RECORD_GAP_RESU,
                "NAME0": "driver",
                "NAME1": "test",
            },
            "test",
            game,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.reporter_index, 1)
        self.assertEqual(result.place, 1)
        self.assertEqual(result.flags, 0)
        self.assertEqual(result.category, 2)
        self.assertEqual(result.record_gap, 2)
        self.assertEqual(result.nos_used, 0.0)

    def test_stock_sprint_resu_decodes_reporter_nos_usage(self) -> None:
        sessions = SessionDirectory()
        game = sessions.create_game(
            0,
            10,
            capacity=2,
            min_players=2,
            host_persona="driver",
        )
        sessions.join_game(game.game_id, 20, persona="test")
        payload = bytearray(
            base64.b64decode(unquote(self.SPRINT_INTER_RECORD_GAP_RESU))
        )
        payload[3] = 0

        result = decode_mw_rank_result(
            {
                "REPT": "driver",
                "RESU": base64.b64encode(payload).decode("ascii"),
                "NAME0": "driver",
                "NAME1": "test",
            },
            "driver",
            game,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.reporter_index, 0)
        self.assertEqual(result.category, 2)
        self.assertAlmostEqual(result.nos_used, 43.266, places=3)

    def test_owner_replacement_marks_missing_resu_report_as_disconnect(self) -> None:
        with TemporaryDirectory() as temporary:
            service, sessions, ranking, context = self._service(Path(temporary))
            identity = context.auth.identity
            self.assertIsNotNone(identity)
            guest_id = identity.user_id + 100
            game = sessions.create_game(
                0,
                identity.user_id,
                capacity=2,
                min_players=2,
                host_persona="Driver",
                host_address="127.0.0.1",
            )
            self.assertTrue(
                sessions.join_game(
                    game.game_id,
                    guest_id,
                    persona="Guest",
                    address="127.0.0.2",
                )
            )
            sessions.set_state(game.game_id, SessionState.ACTIVE)
            accepted, complete = sessions.record_result(
                game.game_id,
                guest_id,
                {
                    "persona": "Guest",
                    "outcome": "WIN",
                    "category": 2,
                    "time": 10,
                    "source": "mw_resu",
                },
            )
            self.assertTrue(accepted)
            self.assertFalse(complete)
            context.lobby_game_id = game.game_id

            reply = service.dispatch(
                ClassicEAFrame.from_fields(
                    "gcre",
                    (("NAME", "Next"), ("MAXSIZE", 2)),
                ),
                context,
            )
            self.assertEqual(reply.reason, "game_created")
            self.assertIsNone(sessions.get_game(game.game_id))
            self.assertNotEqual(context.lobby_game_id, game.game_id)
            self.assertEqual(
                ranking.summary("most_wanted", "Driver", 2)["disconnects"],
                1,
            )
            self.assertEqual(
                ranking.summary("most_wanted", "Driver", 0)["disconnects"],
                1,
            )

    def test_owner_replacement_without_resu_does_not_invent_disconnects(self) -> None:
        with TemporaryDirectory() as temporary:
            service, sessions, ranking, context = self._service(Path(temporary))
            identity = context.auth.identity
            self.assertIsNotNone(identity)
            game = sessions.create_game(
                0,
                identity.user_id,
                capacity=2,
                min_players=2,
                host_persona="Driver",
                host_address="127.0.0.1",
            )
            sessions.join_game(
                game.game_id,
                identity.user_id + 100,
                persona="Guest",
            )
            context.lobby_game_id = game.game_id
            service.dispatch(
                ClassicEAFrame.from_fields("gcre", (("NAME", "Next"),)),
                context,
            )
            self.assertEqual(
                ranking.summary("most_wanted", "Driver", 0)["disconnects"],
                0,
            )

    def test_stock_resu_rejects_participant_set_mismatch(self) -> None:
        with TemporaryDirectory() as temporary:
            service, sessions, ranking, context = self._service(Path(temporary))
            identity = context.auth.identity
            self.assertIsNotNone(identity)
            game = sessions.create_game(
                0,
                identity.user_id,
                capacity=3,
                min_players=2,
                host_persona="Driver",
                host_address="127.0.0.1",
            )
            sessions.join_game(
                game.game_id,
                identity.user_id + 100,
                persona="Opponent",
            )
            sessions.join_game(
                game.game_id,
                identity.user_id + 200,
                persona="Third",
            )
            context.lobby_game_id = game.game_id

            reply = service.dispatch(
                ClassicEAFrame.from_fields(
                    "rank",
                    (
                        ("REPT", "Driver"),
                        ("RESU", self.DISCONNECT_SURVIVOR_RESU),
                        ("NAME0", "Opponent"),
                        ("NAME1", "Driver"),
                    ),
                ),
                context,
            )
            decoded, _ = ClassicEAFrame.decode_one(reply.frames[0])
            self.assertEqual(decoded.fields()["RECORDED"], "0")
            self.assertFalse(game.reported_participants)
            self.assertEqual(
                ranking.summary("most_wanted", "Driver", 3)["wins"],
                0,
            )

    def test_stock_resu_rejects_names_outside_current_game(self) -> None:
        with TemporaryDirectory() as temporary:
            service, sessions, ranking, context = self._service(Path(temporary))
            identity = context.auth.identity
            self.assertIsNotNone(identity)
            game = sessions.create_game(
                0,
                identity.user_id + 100,
                capacity=2,
                min_players=2,
                host_persona="ActualOpponent",
            )
            sessions.join_game(
                game.game_id,
                identity.user_id,
                persona="Driver",
            )
            context.lobby_game_id = game.game_id

            reply = service.dispatch(
                ClassicEAFrame.from_fields(
                    "rank",
                    (
                        ("REPT", "Driver"),
                        ("RESU", self.DISCONNECT_SURVIVOR_RESU),
                        ("NAME0", "WrongOpponent"),
                        ("NAME1", "Driver"),
                    ),
                ),
                context,
            )
            decoded, _ = ClassicEAFrame.decode_one(reply.frames[0])
            self.assertEqual(decoded.fields()["RECORDED"], "0")
            self.assertFalse(game.reported_participants)
            self.assertEqual(
                ranking.summary("most_wanted", "Driver", 3)["wins"],
                0,
            )

    def test_gsea_applies_flag_masks(self) -> None:
        with TemporaryDirectory() as temporary:
            service, sessions, _ranking, context = self._service(Path(temporary))
            identity = context.auth.identity
            self.assertIsNotNone(identity)
            sessions.create_game(
                0,
                identity.user_id,
                capacity=4,
                min_players=2,
                name="Wanted",
                custflags="0x10",
                sysflags="0x20",
                host_persona="Driver",
            )
            sessions.create_game(
                0,
                identity.user_id + 1,
                capacity=4,
                min_players=2,
                name="Other",
                custflags="0x40",
                sysflags="0x20",
                host_persona="Other",
            )
            reply = service.dispatch(
                ClassicEAFrame.from_fields(
                    "gsea",
                    (
                        ("CUSTFLAGS", "0x10"),
                        ("CUSTMASK", "0xf0"),
                        ("SYSFLAGS", "0x20"),
                        ("SYSMASK", "0xf0"),
                    ),
                ),
                context,
            )
            commands = [ClassicEAFrame.decode_one(frame)[0] for frame in reply.frames]
            self.assertEqual(commands[0].fields()["COUNT"], "1")
            self.assertEqual(commands[-1].fields()["NAME"], "Wanted")


if __name__ == "__main__":
    unittest.main()
