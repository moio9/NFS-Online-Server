"""Transaction checks for the clean Carbon Theater service."""

import unittest

from carbon.accounts.identity import IdentityStore, MAX_CARBON_WIRE_PLAYER_ID
from carbon.core.config import Endpoint
from carbon.fesl.frame import FESLFrame
from carbon.theater.directory import CarbonGameDirectory
from carbon.theater.service import CarbonTheaterService, TheaterConnection


class CarbonTheaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identities = IdentityStore(token_factory=lambda: "theater-key.")
        self.identity, self.token = self.identities.login("Driver")
        self.games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        self.service = CarbonTheaterService(self.identities, self.games, clock=lambda: 1234.0)
        self.connection = TheaterConnection(peer_ip="198.51.100.7", peer_port=7322)

    def test_conn_and_user_bind_the_fesl_identity(self) -> None:
        conn = FESLFrame.from_fields("CONN", {"TID": "1", "PROT": "2"}, transaction=99)
        conn_reply = self.service.dispatch(conn, self.connection)[0]
        self.assertEqual(conn_reply.transaction, 0)
        self.assertEqual(conn_reply.fields["TIME"], "1234")

        user = FESLFrame.from_fields("USER", {"TID": "2", "LKEY": self.token})
        user_reply = self.service.dispatch(user, self.connection)[0]
        self.assertEqual(user_reply.fields, {"NAME": "Driver", "TID": "2"})
        self.assertEqual(self.connection.identity, self.identity)


    def test_echo_and_keepalive_match_the_retail_liveness_shape(self) -> None:
        echo = self.service.dispatch(
            FESLFrame.from_fields("ECHO", {"TID": "1", "TYPE": "1", "UID": "907370134"}),
            self.connection,
        )[0]
        self.assertEqual(
            echo.fields,
            {
                "PORT": "7322",
                "TID": "1",
                "IP": "198.51.100.7",
                "TXN": "ECHO",
                "ERR": "0",
                "TYPE": "1",
            },
        )
        keep = self.service.dispatch(
            FESLFrame.from_fields("KEEP", {"TID": "9", "HKEEP": "1"}),
            self.connection,
        )[0]
        self.assertEqual(keep.fields, {"TID": "9", "HKEEP": "1"})


    def test_directory_rejects_wire_player_ids_with_the_signed_high_bit_set(self) -> None:
        games = CarbonGameDirectory(
            Endpoint("127.0.0.1", 19118),
            player_id_resolver=lambda _identity: MAX_CARBON_WIRE_PLAYER_ID + 1,
        )
        with self.assertRaisesRegex(ValueError, "player id out of range"):
            games.create(self.identity)

    def test_directory_accepts_the_largest_positive_signed_short_player_id(self) -> None:
        games = CarbonGameDirectory(
            Endpoint("127.0.0.1", 19118),
            player_id_resolver=lambda _identity: MAX_CARBON_WIRE_PLAYER_ID,
        )
        game = games.create(self.identity)
        participant = games.enter(game.gid, self.identity)
        self.assertIsNotNone(participant)
        assert participant is not None
        self.assertEqual(participant.player_id, MAX_CARBON_WIRE_PLAYER_ID)

    def test_room_ugid_is_fresh_when_gid_allocation_restarts(self) -> None:
        first_directory = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        second_directory = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))

        first_room = first_directory.create(self.identity)
        second_room = second_directory.create(self.identity)

        self.assertEqual(first_room.gid, second_room.gid)
        self.assertNotEqual(first_room.ugid, second_room.ugid)

    def test_messenger_snapshot_exposes_room_session_id(self) -> None:
        game = self.games.create(self.identity)
        participant = self.games.enter(game.gid, self.identity)
        self.assertIsNotNone(participant)
        snapshot = self.games.messenger_snapshot()
        self.assertEqual(snapshot["driver"]["session_id"], game.gid)

    def test_invite_uid_and_game_manager_pid_share_one_wire_namespace(self) -> None:
        host, _ = self.identities.login("InviteHost")
        guest, guest_token = self.identities.login("InviteGuest")
        games = CarbonGameDirectory(
            Endpoint("127.0.0.1", 19118),
            player_id_resolver=self.identities.wire_player_id,
        )
        game = games.create(
            host,
            {"B-U-game_type": "2", "B-U-max_online_player": "2"},
            server_hosted=True,
        )
        host_participant = games.enter(game.gid, host)
        self.assertIsNotNone(host_participant)
        assert host_participant is not None
        self.assertEqual(
            host_participant.player_id,
            self.identities.wire_player_id(host),
        )

        service = CarbonTheaterService(self.identities, games)
        connection = TheaterConnection(
            identity=guest,
            selected_gid=game.gid,
            announced_gdet_gid=game.gid,
            invite_host_persona=host.persona,
        )
        replies = service.dispatch(
            FESLFrame.from_fields(
                "EGAM",
                {
                    "TID": "7",
                    "UID": str(self.identities.wire_player_id(host)),
                    "R-UID": str(self.identities.wire_player_id(host)),
                    "R-INT-IP": "192.168.1.9",
                    "R-INT-PORT": "55277",
                },
            ),
            connection,
        )
        egeg = next(frame for frame in replies if frame.command == "EGEG")
        self.assertEqual(
            egeg.fields["PID"],
            str(self.identities.wire_player_id(guest)),
        )
        self.assertNotEqual(egeg.fields["PID"], "2")

    def test_invalid_user_session_is_explicitly_rejected(self) -> None:
        user = FESLFrame.from_fields("USER", {"TID": "2", "LKEY": "bad"})
        reply = self.service.dispatch(user, self.connection)[0]
        self.assertEqual(reply.fields["ERR"], "INVALID_SESSION")

    def test_empty_directory_returns_llst_ldat_and_glst(self) -> None:
        replies = self.service.dispatch(FESLFrame.from_fields("LLST", {"TID": "3"}), self.connection)
        self.assertEqual([reply.command for reply in replies], ["LLST", "LDAT"])
        self.assertEqual(replies[1].fields["LID"], "257")
        glst = self.service.dispatch(FESLFrame.from_fields("GLST", {"TID": "4", "LID": "257"}), self.connection)[0]
        self.assertEqual(glst.fields["NUM-GAMES"], "0")
        pcnt = self.service.dispatch(FESLFrame.from_fields("PCNT", {"TID": "5", "LID": "257"}), self.connection)[0]
        self.assertEqual(pcnt.fields, {"COUNT": "0", "TID": "5", "LID": "257"})

    def test_ranked_requires_an_explicit_session_property(self) -> None:
        game = self.games.create(self.identity, {"B-U-matchmaking_state": "1"})
        self.assertFalse(game.is_ranked)
        game.properties["B-U-ranked"] = "1"
        self.assertTrue(game.is_ranked)
        game.properties["B-U-ranked"] = "0"
        self.assertFalse(game.is_ranked)

    def test_ranked_capture_shape_and_plain_option_survive_creation(self) -> None:
        captured = self.games.create(
            self.identity,
            {"B-U-matchmaking_state": "1", "B-U-game_type": "0"},
        )
        self.assertTrue(captured.is_ranked)

        other_identity, _ = self.identities.login("RankedHost")
        explicit = self.games.create(other_identity, {"QROptionsRankedMode": "1"})
        self.assertEqual(explicit.properties["QROptionsRankedMode"], "1")
        self.assertTrue(explicit.is_ranked)

    def test_game_type_zero_stays_ranked_when_matchmaking_is_locked(self) -> None:
        game = self.games.create(
            self.identity,
            {"B-U-game_type": "0", "B-U-matchmaking_state": "0"},
        )
        self.assertTrue(game.is_ranked)

    def test_create_list_and_enter_share_one_authoritative_game(self) -> None:
        self.connection.identity = self.identity
        created = self.service.dispatch(
            FESLFrame.from_fields("CGAM", {"TID": "5", "MAX-PLAYERS": "4", "N": "Test Room"}),
            self.connection,
        )[0]
        gid = created.fields["GID"]
        self.assertEqual(created.fields["EKEY"], "9181081919")

        listed = self.service.dispatch(
            FESLFrame.from_fields("GLST", {"TID": "6", "LID": "257"}),
            self.connection,
        )
        self.assertEqual([reply.command for reply in listed], ["GLST", "GDAT"])
        self.assertEqual(listed[1].fields["GID"], gid)

        entered = self.service.dispatch(
            FESLFrame.from_fields(
                "EGAM",
                {
                    "TID": "7",
                    "LID": "257",
                    "GID": gid,
                    "R-INT-IP": "192.168.1.9",
                    "R-INT-PORT": "1042",
                },
            ),
            self.connection,
        )
        self.assertEqual([reply.command for reply in entered], ["GDET", "EGAM", "EGEG"])
        self.assertNotIn("TID", entered[0].fields)
        self.assertEqual(entered[0].fields["UGID"], self.games.get(gid).ugid)
        self.assertNotIn("GUGID", entered[0].fields)
        self.assertEqual(entered[1].fields["TID"], "7")
        self.assertEqual(entered[2].fields["PID"], "1")
        self.assertEqual(entered[2].fields["P"], "19118")
        self.assertEqual(entered[2].fields["HUID"], str(self.identity.user_id))
        self.assertEqual(self.games.get(gid).row()["HU"], entered[2].fields["HUID"])
        host_participant = self.games.get(gid).participants[self.identity.user_id]
        self.assertEqual(host_participant.internal_ip, "192.168.1.9")
        self.assertEqual(host_participant.internal_port, 1042)

        guest_identity, guest_token = self.identities.login("Guest")
        guest_connection = TheaterConnection()
        self.service.dispatch(
            FESLFrame.from_fields("USER", {"TID": "8", "LKEY": guest_token}),
            guest_connection,
        )
        guest_entered = self.service.dispatch(
            FESLFrame.from_fields("EGAM", {"TID": "9", "LID": "257", "GID": gid, "R-INT-PORT": "2042"}),
            guest_connection,
        )
        self.assertEqual(guest_connection.identity, guest_identity)
        self.assertEqual(guest_entered[2].fields["PID"], "2")
        self.assertEqual(self.games.get(gid).row()["AP"], "2")

    def test_egeg_uses_local_endpoint_only_for_exact_reported_ip(self) -> None:
        games = CarbonGameDirectory(
            Endpoint("198.51.100.25", 19118),
            local_race_endpoint=Endpoint("192.168.1.150", 19118),
            player_id_resolver=self.identities.wire_player_id,
        )
        service = CarbonTheaterService(self.identities, games)
        game = games.create(self.identity)

        local_connection = TheaterConnection(
            identity=self.identity,
            selected_gid=game.gid,
        )
        local_replies = service.dispatch(
            FESLFrame.from_fields(
                "EGAM",
                {
                    "TID": "10",
                    "GID": game.gid,
                    "R-INT-IP": "192.168.1.150",
                    "R-INT-PORT": "1042",
                },
            ),
            local_connection,
        )
        local_egeg = next(frame for frame in local_replies if frame.command == "EGEG")
        self.assertEqual(local_egeg.fields["I"], "192.168.1.150")
        self.assertEqual(local_egeg.fields["INT-IP"], "192.168.1.150")

        remote_identity, _ = self.identities.login("RemoteDevice")
        remote_connection = TheaterConnection(
            identity=remote_identity,
            selected_gid=game.gid,
        )
        remote_replies = service.dispatch(
            FESLFrame.from_fields(
                "EGAM",
                {
                    "TID": "11",
                    "GID": game.gid,
                    "R-INT-IP": "192.168.1.100",
                    "R-INT-PORT": "1042",
                },
            ),
            remote_connection,
        )
        remote_egeg = next(frame for frame in remote_replies if frame.command == "EGEG")
        self.assertEqual(remote_egeg.fields["I"], "198.51.100.25")
        self.assertEqual(remote_egeg.fields["INT-IP"], "198.51.100.25")

    def test_invited_guest_resolves_gdat_by_host_persona_then_enters_without_gid(self) -> None:
        self.connection.identity = self.identity
        game = self.games.create(self.identity, {"B-U-game_type": "2"}, server_hosted=True)
        self.games.enter(game.gid, self.identity, internal_ip="192.168.1.9", internal_port=1042)
        self.games.set_quick_join_locked(game.gid, True, reason="test-ready")
        guest_identity, guest_token = self.identities.login("Guest")
        guest = TheaterConnection()
        self.service.dispatch(
            FESLFrame.from_fields("USER", {"TID": "2", "LKEY": guest_token}),
            guest,
        )

        ordinary_lookup = self.service.dispatch(
            FESLFrame.from_fields("GDAT", {"TID": "5", "GID": game.gid}),
            self.connection,
        )
        self.assertEqual([reply.command for reply in ordinary_lookup], ["GDAT"])
        self.assertEqual(ordinary_lookup[0].fields["AP"], "1")

        lookup = self.service.dispatch(
            FESLFrame.from_fields("GDAT", {"TID": "6", "USER": "Driver"}),
            guest,
        )
        self.assertEqual([reply.command for reply in lookup], ["GDAT", "GDET"])
        self.assertEqual(lookup[0].fields["GID"], game.gid)
        self.assertNotIn("UGID", lookup[0].fields)
        self.assertNotIn("GUGID", lookup[0].fields)
        self.assertEqual(lookup[0].fields["HU"], "1")
        self.assertEqual(lookup[0].fields["AP"], "0")
        self.assertEqual(lookup[0].fields["JP"], "1")
        self.assertEqual(lookup[0].fields["QP"], "0")
        self.assertEqual(
            lookup[1].fields,
            {
                "TID": "6",
                "UGID": game.ugid,
                "LID": game.lobby_id,
                "GID": game.gid,
            },
        )
        self.assertEqual(guest.selected_gid, game.gid)

        entered = self.service.dispatch(
            FESLFrame.from_fields(
                "EGAM",
                {"TID": "7", "R-INT-IP": "192.168.1.9", "R-INT-PORT": "2042"},
            ),
            guest,
        )
        self.assertEqual([reply.command for reply in entered], ["EGAM", "EGEG"])
        self.assertEqual(entered[1].fields["GID"], game.gid)
        self.assertEqual(entered[1].fields["PID"], "2")
        self.assertIn(guest_identity.user_id, game.participants)
        self.assertFalse(
            self.games.messenger_snapshot()["guest"]["invite_join_complete"]
        )

        # Runtime publishes this only after the EGAM+EGEG batch is written.
        self.assertTrue(self.service.complete_invite_entry(guest))
        self.assertTrue(
            self.games.messenger_snapshot()["guest"]["invite_join_complete"]
        )

        populated_lookup = self.service.dispatch(
            FESLFrame.from_fields("GDAT", {"TID": "8", "GID": game.gid}),
            self.connection,
        )
        self.assertEqual(populated_lookup[0].fields["AP"], "2")

    def test_invite_room_snapshot_derives_track_and_population(self) -> None:
        game = self.games.create(
            self.identity,
            {
                "B-U-game_type": "0",
                "B-U-game_mode": "0",
                "B-U-max_online_player": "4",
                "B-U-collision_detection": "0",
                "B-U-length": "3",
                "B-U-track": "",
                "B-U-race_type_sprint": "ct.4.2",
            },
            server_hosted=True,
        )
        self.games.enter(game.gid, self.identity)

        details = self.games.invite_fields_for_persona(self.identity.persona)

        self.assertEqual(details["game_type"], "0")
        self.assertEqual(details["game_mode"], "0")
        self.assertEqual(details["max_online_player"], "4")
        self.assertEqual(details["collision_detection"], "0")
        self.assertEqual(details["length"], "3")
        self.assertEqual(details["track"], "ct.4.2")
        self.assertEqual(details["race_type_sprint"], "ct.4.2")
        self.assertEqual(details["race_type_circuit"], "ABSTAIN")
        self.assertEqual(details["race_type_speedtrap"], "ABSTAIN")
        self.assertEqual(details["AP"], "1")
        self.assertEqual(details["MP"], "4")

    def test_challenge_circuit_invite_derives_mode_from_selected_event(self) -> None:
        game = self.games.create(
            self.identity,
            {
                "B-U-game_type": "2",
                # The Challenge allocation starts with the captured Sprint
                # default, but the concrete host event is a Circuit.
                "B-U-game_mode": "0",
                "B-U-track": "",
                "B-U-race_type_sprint": "ABSTAIN",
                "B-U-race_type_circuit": "cs.8.1",
            },
            server_hosted=True,
        )
        game.properties.update({
            "B-U-game_mode": "0",
            "B-U-track": "",
            "B-U-race_type_sprint": "ABSTAIN",
            "B-U-race_type_circuit": "cs.8.1",
            "B-U-race_type_canyon_due": "ABSTAIN",
            "B-U-race_type_speedtrap": "ABSTAIN",
            "B-U-race_type_knockout": "ABSTAIN",
            "B-U-race_type_pursuit_tag": "ABSTAIN",
        })
        self.games.enter(game.gid, self.identity)

        details = self.games.invite_fields_for_persona(self.identity.persona)

        self.assertEqual(details["game_type"], "2")
        self.assertEqual(details["game_mode"], "1")
        self.assertEqual(details["track"], "cs.8.1")
        self.assertEqual(details["race_type_circuit"], "cs.8.1")
        self.assertEqual(details["race_type_sprint"], "ABSTAIN")
        self.assertEqual(details["race_type_speedtrap"], "ABSTAIN")
        self.assertEqual(details["matchmaking_state"], "0")
        self.assertEqual(details["help_type"], "0")
        self.assertEqual(details["max_online_player"], "2")

    def test_ecnl_retires_host_game_and_notifies_transport(self) -> None:
        dropped = []
        service = CarbonTheaterService(
            self.identities,
            self.games,
            clock=lambda: 1234.0,
            leave_handler=lambda gid, user_id: dropped.append((gid, user_id)),
        )
        self.connection.identity = self.identity
        game = self.games.create(self.identity)
        reply = service.dispatch(
            FESLFrame.from_fields(
                "ECNL",
                {"TID": "11", "LID": "257", "GID": game.gid},
            ),
            self.connection,
        )[0]
        self.assertEqual(
            reply.fields,
            {"TID": "11", "LID": "257", "GID": game.gid},
        )
        self.assertIsNone(self.games.get(game.gid))
        self.assertEqual(dropped, [(game.gid, self.identity.user_id)])

    def test_ecnl_retires_server_hosted_room_when_allocator_leaves(self) -> None:
        guest, _ = self.identities.login("DedicatedGuest")
        game = self.games.create(
            self.identity,
            {"B-U-game_type": "2", "B-U-max_online_player": "2"},
            server_hosted=True,
        )
        self.assertIsNotNone(self.games.enter(game.gid, self.identity))
        self.assertIsNotNone(
            self.games.enter(game.gid, guest, invite_entry=True)
        )
        service = CarbonTheaterService(self.identities, self.games)

        with self.assertLogs("carbon.theater.directory", level="INFO") as captured:
            service.dispatch(
                FESLFrame.from_fields(
                    "ECNL",
                    {"TID": "12", "LID": "257", "GID": game.gid},
                ),
                TheaterConnection(identity=self.identity),
            )

        self.assertIsNone(self.games.get(game.gid))
        self.assertIsNone(self.games.sessions.get_game(game.session.game_id))
        host_exit = "\n".join(captured.output)
        self.assertIn("Carbon directory host exited", host_exit)
        self.assertIn(f"gid={game.gid}", host_exit)
        self.assertIn(f"persona={self.identity.persona}", host_exit)
        self.assertIn("reason=theater-ecnl", host_exit)
        self.assertIn("remaining=1", host_exit)

    def test_guest_ecnl_updates_room_counts_and_notifies_transport(self) -> None:
        guest, _ = self.identities.login("LeavingGuest")
        game = self.games.create(self.identity, {"MAX-PLAYERS": "4"})
        self.assertIsNotNone(self.games.enter(game.gid, guest))
        dropped = []
        service = CarbonTheaterService(
            self.identities,
            self.games,
            leave_handler=lambda gid, user_id: dropped.append((gid, user_id)),
        )
        guest_connection = TheaterConnection(identity=guest)

        service.dispatch(
            FESLFrame.from_fields("ECNL", {"TID": "12", "LID": "257", "GID": game.gid}),
            guest_connection,
        )

        remaining = self.games.get(game.gid)
        self.assertIsNotNone(remaining)
        assert remaining is not None
        self.assertEqual(remaining.row()["AP"], "1")
        self.assertEqual(remaining.row()["QP"], "1")
        self.assertEqual(remaining.row()["JP"], "3")
        self.assertEqual(dropped, [(game.gid, guest.user_id)])

    def test_ecnl_deferred_by_active_race_keeps_guest_in_room(self) -> None:
        guest, _ = self.identities.login("RacingGuest")
        game = self.games.create(self.identity, {"MAX-PLAYERS": "2"})
        self.assertIsNotNone(self.games.enter(game.gid, guest))
        deferred = []
        service = CarbonTheaterService(
            self.identities,
            self.games,
            leave_handler=lambda gid, user_id: deferred.append((gid, user_id)) or True,
        )

        service.dispatch(
            FESLFrame.from_fields("ECNL", {"TID": "13", "LID": "257", "GID": game.gid}),
            TheaterConnection(identity=guest),
        )

        current = self.games.get(game.gid)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertIn(guest.user_id, current.participants)
        self.assertEqual(deferred, [(game.gid, guest.user_id)])
