import base64
import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest

from classic.accounts.credentials import CredentialStore
from classic.accounts.identity import IdentityStore
from classic.core.config import Endpoint
from classic.ea.directory import SessionDirectory, SessionState
from classic.ea.ranking import ClassicRankingStore
from classic.games.underground2.auth import create_auth_service
from classic.protocols.auth import ClassicAuthContext
from classic.protocols.frame import ClassicEAFrame
from classic.protocols.prelogin import (
    ClassicPreloginContext,
    ClassicPreloginProfile,
    ClassicPreloginService,
)
from classic.protocols.ranking_codec import (
    decode_u2_rank_result,
    u2_stat_category,
)


class Underground2RankingTests(unittest.TestCase):
    _CAPTURED_V3_TEST = (
        "AwIAAA+rAAIBAgEEQAACCELR5mdCRYUfQe7jh0IS0HNBAwo+QXNoc0IcF5QAAAAA"
        "AAAAAAAAAAAAAAAAAAIAAAUAAQEEQAACTEK2j11CJGZnQgeZbUIqWipBGHriQXEI"
        "MkIdjFUAAAAAQwCQ1AAAAAAAAAAAAAAA"
    )
    _CAPTURED_V3_PAROLA = (
        "AwIAAA+rAAIAAgEEQAABrkLR5mdCRYUfQe68dkISwuNBAeuGQXMrA0IpWicAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAQEBBD//7I9CtpVAQki4U0IHspJCKg4tQRlwpEF2BBlC"
        "JZKmAAAAAAAAAAAAAAAAAAAAAAACAAAC"
    )

    def _service(
        self,
        root: Path,
        profile: ClassicPreloginProfile | None = None,
    ):
        credentials = CredentialStore(root / "auth.json")
        credentials.create_account("HostAccount", "password", persona="Host")
        credentials.create_account("GuestAccount", "password", persona="Guest")
        identities = IdentityStore(token_factory=lambda: "token")
        auth = create_auth_service(credentials, identities, verify_passwords=False)
        sessions = SessionDirectory()
        ranking = ClassicRankingStore(root / "stats.json")
        service = ClassicPreloginService(
            auth,
            profile=profile or ClassicPreloginProfile(game_id="underground2"),
            control_endpoint=Endpoint("127.0.0.1", 13505),
            sessions=sessions,
            ranking=ranking,
        )

        def context(account_name: str, persona: str, connection: str):
            identity, token = identities.login(account_name, persona)
            return ClassicPreloginContext(
                auth=ClassicAuthContext(
                    connection_id=connection,
                    account=credentials.resolve_account(account_name),
                    identity=identity,
                    session_token=token,
                    lkey=token,
                    persona=persona,
                ),
                authenticated=True,
                persona_selected=True,
                client_address="127.0.0.1",
                send_wire=lambda _wire: True,
            )

        return service, sessions, ranking, context

    @staticmethod
    def _commands(frames: tuple[bytes, ...]) -> list[str]:
        return [ClassicEAFrame.decode_one(frame)[0].command for frame in frames]

    @staticmethod
    def _resu(*, reporter: int, race_type: int = 1, laps: int = 2) -> str:
        participant_count = 2
        block_size = 58 + 8 * laps
        payload = bytearray(7 + participant_count * block_size)
        payload[0] = participant_count
        payload[1] = race_type
        payload[2] = reporter
        payload[3:5] = (4012).to_bytes(2, "big")
        payload[5] = 0
        payload[6] = laps
        for index, place in ((0, 1), (1, 2)):
            offset = 7 + index * block_size
            payload[offset] = index
            payload[offset + 2] = place
            payload[offset + 3] = 1
            payload[offset + 12 : offset + 16] = struct.pack(">f", 31.25 + index)
            disc_offset = offset + 12 + laps * 4 + 24
            payload[disc_offset : disc_offset + 4] = struct.pack(">f", 0.0)
            drift_offset = offset + 12 + laps * 4 + 32
            payload[drift_offset : drift_offset + 4] = (1000 + index).to_bytes(
                4, "big"
            )
        return base64.b64encode(payload).decode("ascii")

    def test_u2_selection_rooms_move_and_game_room_name(self) -> None:
        with TemporaryDirectory() as temporary:
            service, sessions, _ranking, make_context = self._service(Path(temporary))
            context = make_context("HostAccount", "Host", "host")

            selection = ClassicEAFrame.decode_one(service._selection_frame())[0]
            self.assertEqual(selection.fields()["GAMES"], "1")
            self.assertEqual(selection.fields()["SLOTS"], "36")

            room_frames = service._u2_room_frames()
            self.assertEqual(len(room_frames), 8)
            rooms = [ClassicEAFrame.decode_one(frame)[0].fields()["N"] for frame in room_frames]
            self.assertEqual(rooms, [f"{letter}.LAN" for letter in "ABCDEFGH"])

            moved = service.dispatch(
                ClassicEAFrame.from_fields("move", (("NAME", "B.LAN"),)),
                context,
            )
            self.assertEqual(moved.reason, "room_moved")
            self.assertEqual(self._commands(moved.frames), ["move", "+who", "+usr", "+pop"])
            self.assertEqual(context.u2_room_id, 2)
            self.assertEqual(context.u2_room_name, "B.LAN")

            created = service.dispatch(
                ClassicEAFrame.from_fields(
                    "gcre",
                    (("NAME", "U2 race"), ("MAXSIZE", 2)),
                ),
                context,
            )
            game = sessions.get_game(context.lobby_game_id)
            self.assertIsNotNone(game)
            self.assertEqual(game.room_id, 2)
            created_frame = ClassicEAFrame.decode_one(created.frames[0])[0]
            self.assertEqual(created_frame.fields()["ROOM"], "B.LAN")

    def test_u2_client_game_size_controls_start_threshold(self) -> None:
        with TemporaryDirectory() as temporary:
            service, sessions, _ranking, make_context = self._service(Path(temporary))
            host = make_context("HostAccount", "Host", "host")
            guest = make_context("GuestAccount", "Guest", "guest")

            created = service.dispatch(
                ClassicEAFrame.from_fields(
                    "gcre",
                    (("NAME", "Ranked race"), ("MINSIZE", 3), ("MAXSIZE", 3)),
                ),
                host,
            )
            game = sessions.get_game(host.lobby_game_id)
            self.assertIsNotNone(game)
            self.assertEqual((game.min_players, game.capacity), (3, 3))
            created_fields = ClassicEAFrame.decode_one(created.frames[0])[0].fields()
            self.assertEqual(created_fields["MINSIZE"], "3")
            self.assertEqual(created_fields["MAXSIZE"], "3")

            self.assertTrue(
                sessions.join_game(
                    game.game_id,
                    guest.auth.identity.user_id,
                    persona="Guest",
                    address="127.0.0.1",
                )
            )
            waiting = service.dispatch(ClassicEAFrame.from_fields("gsta", ()), host)
            self.assertEqual(waiting.reason, "game_start_waiting_for_players")

            self.assertTrue(sessions.join_game(game.game_id, 999, persona="Third"))
            started = service.dispatch(ClassicEAFrame.from_fields("gsta", ()), host)
            self.assertEqual(started.reason, "game_started")

    def test_u2_server_game_size_policy_overrides_client_and_gset(self) -> None:
        with TemporaryDirectory() as temporary:
            profile = ClassicPreloginProfile(
                game_id="underground2",
                u2_game_size_policy="server",
                u2_game_min_players=2,
                u2_game_max_players=4,
            )
            service, sessions, _ranking, make_context = self._service(
                Path(temporary),
                profile,
            )
            host = make_context("HostAccount", "Host", "host")
            service.dispatch(
                ClassicEAFrame.from_fields(
                    "gcre",
                    (("MINSIZE", 3), ("MAXSIZE", 3)),
                ),
                host,
            )
            game = sessions.get_game(host.lobby_game_id)
            self.assertIsNotNone(game)
            self.assertEqual((game.min_players, game.capacity), (2, 4))

            updated = service.dispatch(
                ClassicEAFrame.from_fields(
                    "gset",
                    (("MINSIZE", 4), ("MAXSIZE", 4)),
                ),
                host,
            )
            self.assertEqual(updated.reason, "game_settings")
            self.assertEqual((game.min_players, game.capacity), (2, 4))

    def test_u2_owner_gset_preserves_explicit_client_sizes(self) -> None:
        with TemporaryDirectory() as temporary:
            service, sessions, _ranking, make_context = self._service(Path(temporary))
            host = make_context("HostAccount", "Host", "host")
            service.dispatch(
                ClassicEAFrame.from_fields(
                    "gcre",
                    (("MINSIZE", 2), ("MAXSIZE", 4)),
                ),
                host,
            )
            game = sessions.get_game(host.lobby_game_id)
            self.assertIsNotNone(game)

            reply = service.dispatch(
                ClassicEAFrame.from_fields(
                    "gset",
                    (("MINSIZE", 3), ("MAXSIZE", 3)),
                ),
                host,
            )
            self.assertEqual((game.min_players, game.capacity), (3, 3))
            fields = ClassicEAFrame.decode_one(reply.frames[0])[0].fields()
            self.assertEqual(fields["MINSIZE"], "3")
            self.assertEqual(fields["MAXSIZE"], "3")

    def test_u2_user_seeds_stock_game_report_acknowledgement(self) -> None:
        with TemporaryDirectory() as temporary:
            service, _sessions, _ranking, make_context = self._service(Path(temporary))
            context = make_context("HostAccount", "Host", "host")

            frame = ClassicEAFrame.decode_one(service._user_frame(context))[0]
            fields = frame.fields()
            self.assertEqual(fields["PERS"], "Host")
            self.assertEqual(fields["ACK_REP"], "186")
            self.assertEqual(fields["REP"], "186")
            self.assertIn("STAT", fields)
            self.assertIn("LMSTAT", fields)

    def test_captured_v3_reports_decode_variable_records(self) -> None:
        test_result = decode_u2_rank_result(
            {
                "REPT": "test",
                "NAME0": "test",
                "NAME1": "driver",
                "RESU": self._CAPTURED_V3_TEST,
            },
            "test",
        )
        self.assertIsNotNone(test_result)
        self.assertEqual(test_result.participant_count, 2)
        self.assertEqual(test_result.reporter_index, 0)
        self.assertEqual(test_result.place, 1)
        self.assertEqual(test_result.race_type, 0)
        self.assertEqual(test_result.category, 0)
        self.assertEqual(test_result.track, 4011)
        self.assertEqual(test_result.direction, 0)
        self.assertEqual(test_result.laps, 2)

        driver_result = decode_u2_rank_result(
            {
                "REPT": "driver",
                "NAME0": "driver",
                "NAME1": "test",
                "RESU": self._CAPTURED_V3_PAROLA,
            },
            "driver",
        )
        self.assertIsNotNone(driver_result)
        self.assertEqual(driver_result.place, 2)
        self.assertEqual(driver_result.track, 4011)

    def test_postrace_resu_updates_ranked_stats_once_after_reconnect(self) -> None:
        with TemporaryDirectory() as temporary:
            service, sessions, ranking, make_context = self._service(Path(temporary))
            host = make_context("HostAccount", "Host", "host-before-race")
            guest = make_context("GuestAccount", "Guest", "guest-before-race")
            guest_wires: list[bytes] = []
            guest.send_wire = lambda wire: not guest_wires.append(wire)
            service._register(guest)
            host.u2_room_id = guest.u2_room_id = 2
            host.u2_room_name = guest.u2_room_name = "B.LAN"
            game = sessions.create_game(
                2,
                host.auth.identity.user_id,
                capacity=2,
                min_players=2,
                name="Ranked Sprint",
                host_persona="Host",
                host_address="127.0.0.1",
            )
            self.assertTrue(
                sessions.join_game(
                    game.game_id,
                    guest.auth.identity.user_id,
                    persona="Guest",
                    address="127.0.0.1",
                )
            )
            host.lobby_game_id = game.game_id
            guest.lobby_game_id = game.game_id

            started = service.dispatch(ClassicEAFrame.from_fields("gsta", ()), host)
            self.assertEqual(started.reason, "game_started")
            self.assertEqual(game.state, SessionState.ACTIVE)
            self.assertEqual(self._commands(started.frames), ["gsta", "+mgm", "+ses"])
            session = ClassicEAFrame.decode_one(started.frames[2])[0].fields()
            self.assertRegex(session["AUTH"], r"^[0-9a-f]{32}$")
            self.assertEqual(session["SELF"], "Host")
            self.assertEqual(self._commands(tuple(guest_wires)), ["gsta", "+mgm", "+ses"])
            guest_session = ClassicEAFrame.decode_one(guest_wires[2])[0].fields()
            self.assertEqual(guest_session["AUTH"], session["AUTH"])
            self.assertEqual(guest_session["SELF"], "Guest")
            game.participant_race_addresses = {
                host.auth.identity.user_id: "100.64.0.1",
                guest.auth.identity.user_id: "100.64.0.2",
            }
            self.assertEqual(
                service._u2_pending_games[host.auth.identity.user_id],
                game.game_id,
            )
            service.release(host)
            service.release(guest)

            host_after = make_context("HostAccount", "Host", "host-after-race")
            host_report = ClassicEAFrame.from_fields(
                "rank",
                (
                    ("REPT", "Host"),
                    ("NAME0", "Host"),
                    ("NAME1", "Guest"),
                    ("RESU", self._resu(reporter=0)),
                ),
            )
            reply = service.dispatch(host_report, host_after)
            self.assertEqual(reply.reason, "game_report")
            self.assertEqual(ClassicEAFrame.decode_one(reply.frames[0])[0].fields()["TIME"], "866")
            # The stock nfsuserver rank path sends only the rank reply.  Lobby
            # presence/game deletion frames here clear the race object before
            # the retail Race Stats screen consumes it.
            self.assertEqual(self._commands(reply.frames), ["rank"])
            self.assertEqual(ranking.summary("underground2", "Host", 1)["wins"], 1)
            # U2 has no aggregate slot. A Sprint report must not increment
            # Circuit.
            self.assertEqual(ranking.summary("underground2", "Host", 0)["wins"], 0)

            service.dispatch(host_report, host_after)
            self.assertEqual(ranking.summary("underground2", "Host", 1)["wins"], 1)

            guest_after = make_context("GuestAccount", "Guest", "guest-after-race")
            guest_report = ClassicEAFrame.from_fields(
                "rank",
                (
                    ("REPT", "Guest"),
                    ("NAME0", "Host"),
                    ("NAME1", "Guest"),
                    ("RESU", self._resu(reporter=1)),
                ),
            )
            guest_reply = service.dispatch(guest_report, guest_after)
            self.assertEqual(self._commands(guest_reply.frames), ["rank"])
            self.assertEqual(ranking.summary("underground2", "Guest", 1)["losses"], 1)
            self.assertEqual(game.state, SessionState.FINISHED)
            self.assertTrue(game.participant_race_addresses)

    def test_u2_modes_update_only_their_own_six_ranked_slots(self) -> None:
        with TemporaryDirectory() as temporary:
            _service, _sessions, ranking, _make_context = self._service(
                Path(temporary)
            )
            ranking.record_result(
                "underground2",
                "Driver",
                category_index=2,
                outcome="WIN",
            )
            self.assertEqual(ranking.summary("underground2", "Driver", 2)["wins"], 1)
            self.assertEqual(ranking.summary("underground2", "Driver", 0)["wins"], 0)

            ranking.record_result(
                "underground2",
                "Driver",
                category_index=5,
                outcome="WIN",
            )
            self.assertEqual(ranking.summary("underground2", "Driver", 5)["wins"], 1)
            self.assertEqual(ranking.summary("underground2", "Driver", 4)["wins"], 0)

    def test_u2_race_type_maps_all_six_ranked_stat_modes(self) -> None:
        expected = {
            0: 0,  # Circuit
            1: 1,  # Sprint
            2: 2,  # Drag
            3: 3,  # Drift
            4: 4,  # Street X
            5: 5,  # URL
            6: -1,
        }
        for race_type, category in expected.items():
            with self.subTest(race_type=race_type):
                self.assertEqual(
                    u2_stat_category(race_type),
                    category,
                )

    def test_u2_stock_snap_channels_follow_native_menu_order(self) -> None:
        expected = {
            6: 1,
            7: 2,
            8: 4,
            9: 3,
            10: 5,
            11: 6,
            12: 1,
            13: 2,
            14: 4,
            15: 3,
            16: 5,
            17: 6,
        }
        for channel, board in expected.items():
            with self.subTest(channel=channel):
                self.assertEqual(
                    ClassicPreloginService._u2_snap_stats_board(1, channel),
                    board,
                )

    def test_u2_drift_and_drag_snap_rows_use_their_native_tabs(self) -> None:
        with TemporaryDirectory() as temporary:
            service, _sessions, ranking, make_context = self._service(Path(temporary))
            context = make_context("HostAccount", "Host", "host")
            ranking.update_fields(
                "underground2",
                "Host",
                2,
                {"wins": 2, "losses": 1, "rep": 1200},
            )
            ranking.update_fields(
                "underground2",
                "Host",
                3,
                {"wins": 7, "losses": 3, "rep": 1700},
            )

            def personal_row(channel: int, index: int) -> dict[str, str]:
                frames = service._snap_frames(
                    {
                        "INDEX": str(index),
                        "CHAN": str(channel),
                        "START": "0",
                        "RANGE": "1",
                        "FIND": "$",
                    },
                    context,
                )
                rows = [ClassicEAFrame.decode_one(frame)[0] for frame in frames[1:]]
                return next(
                    row.fields() for row in rows if row.fields().get("N") == "Host"
                )

            drift = personal_row(14, 21)
            drag = personal_row(15, 31)
            self.assertEqual(drift["S"], f"{1700:x},7,3")
            self.assertEqual(drag["S"], f"{1200:x},2,1")

    def test_u2_global_snap_keeps_street_x_and_url_rows_separate(self) -> None:
        with TemporaryDirectory() as temporary:
            service, _sessions, ranking, make_context = self._service(Path(temporary))
            context = make_context("HostAccount", "Host", "host")
            ranking.update_fields(
                "underground2",
                "Host",
                4,
                {"wins": 3, "losses": 2, "rep": 1400},
            )
            ranking.update_fields(
                "underground2",
                "Host",
                5,
                {"wins": 7, "losses": 1, "rep": 1800},
            )

            def snapshot(channel: int, index: int) -> tuple[dict[str, str], dict[str, str]]:
                frames = service._snap_frames(
                    {
                        "INDEX": str(index),
                        "CHAN": str(channel),
                        "START": "0",
                        "RANGE": "100",
                        "FIND": "$",
                    },
                    context,
                )
                header = ClassicEAFrame.decode_one(frames[0])[0].fields()
                rows = [ClassicEAFrame.decode_one(frame)[0] for frame in frames[1:]]
                self.assertEqual(header["RANGE"], str(len(rows)))
                return header, next(
                    row.fields() for row in rows if row.fields().get("N") == "Host"
                )

            street_x_header, street_x = snapshot(10, 41)
            url_header, url = snapshot(11, 51)
            self.assertNotEqual(street_x_header["RANGE"], "100")
            self.assertNotEqual(url_header["RANGE"], "100")
            self.assertEqual(street_x["P"], f"{1400:x}")
            self.assertEqual(url["P"], f"{1800:x}")
            self.assertEqual(street_x["S"], f"{1400:x},3,2")
            self.assertEqual(url["S"], f"{1800:x},7,1")
            self.assertGreaterEqual(int(street_x["R"]), 1)
            self.assertGreaterEqual(int(url["R"]), 1)

            def personal_row(channel: int, index: int) -> dict[str, str]:
                frames = service._snap_frames(
                    {
                        "INDEX": str(index),
                        "CHAN": str(channel),
                        "RANGE": "1",
                        "FIND": "$",
                    },
                    context,
                )
                rows = [ClassicEAFrame.decode_one(frame)[0] for frame in frames[1:]]
                return next(
                    row.fields() for row in rows if row.fields().get("N") == "Host"
                )

            street_x_personal = personal_row(16, 41)
            url_personal = personal_row(17, 51)
            self.assertEqual(street_x_personal["P"], f"{1400:x}")
            self.assertEqual(url_personal["P"], f"{1800:x}")
            self.assertEqual(street_x_personal["S"], f"{1400:x},3,2")
            self.assertEqual(url_personal["S"], f"{1800:x},7,1")
            self.assertGreaterEqual(int(street_x_personal["R"]), 1)
            self.assertGreaterEqual(int(url_personal["R"]), 1)

    def test_u2_profile_uses_six_native_five_long_mode_blocks(self) -> None:
        with TemporaryDirectory() as temporary:
            ranking = ClassicRankingStore(Path(temporary) / "stats.json")
            ranking.update_fields(
                "underground2",
                "Driver",
                4,
                {"wins": 3, "losses": 2, "rep": 1400},
            )
            ranking.update_fields(
                "underground2",
                "Driver",
                5,
                {"wins": 7, "losses": 1, "rep": 1800},
            )

            values = ranking.profile_hex_csv("underground2", "Driver").split(",")
            self.assertEqual(len(values), 39)
            self.assertEqual(values[38], "")
            self.assertEqual(values[28:31], ["578", "3", "2"])
            self.assertEqual(values[33:36], ["708", "7", "1"])

    def test_v1_u2_slots_migrate_to_six_mode_retail_order(self) -> None:
        with TemporaryDirectory() as temporary:
            stats_path = Path(temporary) / "stats.json"
            old_blocks = []
            for marker in range(5):
                old_blocks.extend([marker + 1, marker + 10, 0, 0, 100, 101, 101])
            stats_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "games": {
                            "underground2": {
                                "personas": {
                                    "driver": {
                                        "persona": "Driver",
                                        "values": old_blocks,
                                    }
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            ranking = ClassicRankingStore(stats_path)
            # v1 order: URL, Circuit, Sprint, Drag, Drift.
            self.assertEqual(ranking.summary("underground2", "Driver", 0)["wins"], 11)
            self.assertEqual(ranking.summary("underground2", "Driver", 3)["wins"], 14)
            self.assertEqual(ranking.summary("underground2", "Driver", 4)["wins"], 0)
            self.assertEqual(ranking.summary("underground2", "Driver", 5)["wins"], 10)

    def test_roomless_stock_create_defaults_to_ranked_room(self) -> None:
        with TemporaryDirectory() as temporary:
            service, sessions, _ranking, make_context = self._service(Path(temporary))
            context = make_context("HostAccount", "Host", "roomless-host")
            created = service.dispatch(
                ClassicEAFrame.from_fields(
                    "gcre",
                    (
                        ("NAME", "Roomless stock race"),
                        ("MAXSIZE", 2),
                        ("PARAMS", "TRACK%3d4012%0aDIR%3d0%0aLAPS%3d2"),
                    ),
                ),
                context,
            )
            game = sessions.get_game(context.lobby_game_id)
            self.assertIsNotNone(game)
            self.assertEqual(game.room_id, 1)
            self.assertEqual(context.u2_room_name, "A.LAN")
            response = ClassicEAFrame.decode_one(created.frames[0])[0]
            self.assertEqual(response.fields()["ROOM"], "A.LAN")


if __name__ == "__main__":
    unittest.main()
