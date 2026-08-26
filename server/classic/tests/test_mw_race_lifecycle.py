from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
import struct

from classic.accounts.credentials import CredentialStore
from classic.accounts.identity import IdentityStore
from classic.core.config import Endpoint
from classic.ea.directory import GameSession, SessionDirectory, SessionState
from classic.ea.ranking import ClassicRankingStore
from classic.games.most_wanted.auth import create_auth_service
from classic.protocols.auth import ClassicAuthContext
from classic.protocols.frame import ClassicEAFrame
from classic.protocols.prelogin import (
    ClassicPreloginContext,
    ClassicPreloginProfile,
    ClassicPreloginService,
    ClassicUserset,
)


class MostWantedRaceLifecycleTests(unittest.TestCase):
    def test_join_serial_lease_admits_next_guest_without_udp_success(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ClassicPreloginService(
                create_auth_service(
                    CredentialStore(root / "auth.json"),
                    IdentityStore(),
                    verify_passwords=False,
                ),
                profile=ClassicPreloginProfile(game_id="most_wanted"),
                control_endpoint=Endpoint("127.0.0.1", 13505),
                sessions=SessionDirectory(),
                ranking=ClassicRankingStore(root / "stats.json"),
            )

            self.assertTrue(
                service._mw_reserve_join_serial_slot(
                    7,
                    101,
                    timeout=0.2,
                    lease_seconds=0.05,
                )
            )
            started = time.monotonic()
            self.assertTrue(
                service._mw_reserve_join_serial_slot(
                    7,
                    202,
                    timeout=0.2,
                    lease_seconds=0.05,
                )
            )
            self.assertGreaterEqual(time.monotonic() - started, 0.04)
            self.assertEqual(service._mw_join_serial_unstable[7], {202})
            self.assertFalse(service.notify_mw_transport_settled(7, 101))

    def test_simultaneous_gjoi_waits_for_first_guest_settled_lt(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = CredentialStore(root / "auth.json")
            for account_name, persona in (
                ("SerialHostAccount", "SerialHost"),
                ("SerialFirstAccount", "SerialFirst"),
                ("SerialSecondAccount", "SerialSecond"),
            ):
                credentials.create_account(
                    account_name,
                    "password",
                    persona=persona,
                )
            identities = IdentityStore()
            auth = create_auth_service(
                credentials,
                identities,
                verify_passwords=False,
            )
            sessions = SessionDirectory()
            service = ClassicPreloginService(
                auth,
                profile=ClassicPreloginProfile(game_id="most_wanted"),
                control_endpoint=Endpoint("127.0.0.1", 13505),
                sessions=sessions,
                ranking=ClassicRankingStore(root / "stats.json"),
            )

            def make_context(account_name: str, persona: str, suffix: int):
                account = credentials.resolve_account(account_name)
                identity, token = identities.login(account_name, persona)
                return ClassicPreloginContext(
                    auth=ClassicAuthContext(
                        connection_id=f"mw-serial-{suffix}",
                        account=account,
                        identity=identity,
                        session_token=token,
                        lkey=token,
                        persona=persona,
                    ),
                    authenticated=True,
                    persona_selected=True,
                    client_address=f"127.0.0.{suffix}",
                )

            host = make_context("SerialHostAccount", "SerialHost", 1)
            first = make_context("SerialFirstAccount", "SerialFirst", 2)
            second = make_context("SerialSecondAccount", "SerialSecond", 3)
            host_id = host.auth.identity.user_id
            first_id = first.auth.identity.user_id
            second_id = second.auth.identity.user_id
            game = sessions.create_game(
                0,
                host_id,
                capacity=4,
                min_players=2,
                name="SerialHost",
                host_persona="SerialHost",
                host_address=host.client_address,
            )
            host.lobby_game_id = game.game_id
            with service._connections_lock:
                service._connections.update(
                    {
                        context.auth.identity.user_id: context
                        for context in (host, first, second)
                    }
                )
            first_join = service.dispatch(
                ClassicEAFrame.from_fields(
                    "gjoi",
                    (("IDENT", game.game_id),),
                ),
                first,
            )
            self.assertEqual(first_join.reason, "game_joined")
            self.assertIn(first_id, game.participants)
            self.assertEqual(
                service._mw_join_serial_unstable[game.game_id],
                {first_id},
            )

            result: list[object] = []

            def join_second() -> None:
                result.append(
                    service.dispatch(
                        ClassicEAFrame.from_fields(
                            "gjoi",
                            (("IDENT", game.game_id),),
                        ),
                        second,
                    )
                )

            worker = Thread(target=join_second)
            worker.start()
            time.sleep(0.05)
            self.assertTrue(worker.is_alive())
            self.assertNotIn(second_id, game.participants)

            service._dispatch_auxiliary(
                first,
                {"TEXT": "SCF%3d0%0aLT%3d0%0aV%3d20%0a"},
            )
            worker.join(1.0)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].reason, "game_joined")
            self.assertIn(second_id, game.participants)
            self.assertEqual(
                service._mw_join_serial_unstable[game.game_id],
                {second_id},
            )
            self.assertTrue(
                service.notify_mw_transport_settled(game.game_id, second_id)
            )
            self.assertNotIn(game.game_id, service._mw_join_serial_unstable)
            self.assertFalse(
                service.notify_mw_transport_settled(game.game_id, second_id)
            )

    def test_postrace_handoff_guests_join_before_either_settles_lt(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = CredentialStore(root / "auth.json")
            for account_name, persona in (
                ("ReturnHostAccount", "ReturnHost"),
                ("ReturnFirstAccount", "ReturnFirst"),
                ("ReturnSecondAccount", "ReturnSecond"),
            ):
                credentials.create_account(
                    account_name,
                    "password",
                    persona=persona,
                )
            identities = IdentityStore()
            auth = create_auth_service(
                credentials,
                identities,
                verify_passwords=False,
            )
            sessions = SessionDirectory()
            service = ClassicPreloginService(
                auth,
                profile=ClassicPreloginProfile(game_id="most_wanted"),
                control_endpoint=Endpoint("127.0.0.1", 13505),
                sessions=sessions,
                ranking=ClassicRankingStore(root / "stats.json"),
            )

            def make_context(account_name: str, persona: str, suffix: int):
                account = credentials.resolve_account(account_name)
                identity, token = identities.login(account_name, persona)
                return ClassicPreloginContext(
                    auth=ClassicAuthContext(
                        connection_id=f"mw-postrace-return-{suffix}",
                        account=account,
                        identity=identity,
                        session_token=token,
                        lkey=token,
                        persona=persona,
                    ),
                    authenticated=True,
                    persona_selected=True,
                    client_address=f"127.0.1.{suffix}",
                )

            host = make_context("ReturnHostAccount", "ReturnHost", 1)
            first = make_context("ReturnFirstAccount", "ReturnFirst", 2)
            second = make_context("ReturnSecondAccount", "ReturnSecond", 3)
            host_id = host.auth.identity.user_id
            first_id = first.auth.identity.user_id
            second_id = second.auth.identity.user_id
            replacement = sessions.create_game(
                0,
                host_id,
                capacity=4,
                min_players=2,
                name="ReturnHost",
                host_persona="ReturnHost",
                host_address=host.client_address,
            )
            # GCRE's successful race-transport handoff seeds the old slot order
            # before the returning guests are members of the replacement game.
            replacement.participant_order = [host_id, first_id, second_id]
            service._mw_postrace_handoff_returners[replacement.game_id] = {
                first_id,
                second_id,
            }
            host.lobby_game_id = replacement.game_id
            first_wire: list[bytes] = []
            second_wire: list[bytes] = []
            first.send_wire = lambda wire: first_wire.append(wire) or True
            second.send_wire = lambda wire: second_wire.append(wire) or True
            with service._connections_lock:
                service._connections.update(
                    {
                        context.auth.identity.user_id: context
                        for context in (host, first, second)
                    }
                )

            first_join = service.dispatch(
                ClassicEAFrame.from_fields(
                    "gjoi",
                    (("IDENT", replacement.game_id),),
                ),
                first,
            )
            second_join = service.dispatch(
                ClassicEAFrame.from_fields(
                    "gjoi",
                    (("IDENT", replacement.game_id),),
                ),
                second,
            )

            self.assertEqual(first_join.reason, "game_joined")
            self.assertEqual(second_join.reason, "game_joined")
            self.assertEqual(
                replacement.participants,
                {host_id, first_id, second_id},
            )
            self.assertNotIn(
                replacement.game_id,
                service._mw_join_serial_unstable,
            )

            callback_probe = service.dispatch(
                ClassicEAFrame.from_fields(
                    "GSET",
                    (("NAME", "ReturnHost"),),
                    reserved=0xFFFFC635,
                ),
                host,
            )
            self.assertEqual(callback_probe.reason, "game_settings")
            self.assertEqual(len(callback_probe.frames), 1)
            self.assertEqual(
                first.mw_join_pending_game_id,
                replacement.game_id,
            )
            self.assertEqual(
                second.mw_join_pending_game_id,
                replacement.game_id,
            )

            # Retail commits simultaneous post-race returners from the
            # owner's lowercase GSET.  Requiring the two pending guests to
            # resolve one another through ONLN deadlocks their G=0 rows.
            room_commit = service.dispatch(
                ClassicEAFrame.from_fields(
                    "gset",
                    (("NAME", "ReturnHost"), ("MINSIZE", 2)),
                ),
                host,
            )
            decoded = [
                ClassicEAFrame.decode_one(wire)[0]
                for wire in room_commit.frames
            ]
            self.assertEqual(room_commit.reason, "game_settings_room_commit")
            self.assertEqual(
                [frame.command for frame in decoded],
                ["gset", "+usm", "+usm", "+mgm"],
            )
            self.assertEqual(
                [frame.fields()["N"] for frame in decoded[1:3]],
                ["ReturnFirst", "ReturnSecond"],
            )
            self.assertEqual(
                [frame.fields()["G"] for frame in decoded[1:3]],
                [str(replacement.game_id), str(replacement.game_id)],
            )
            self.assertIsNotNone(room_commit.after_send)
            room_commit.after_send()
            self.assertEqual(first.mw_join_pending_game_id, 0)
            self.assertEqual(second.mw_join_pending_game_id, 0)
            for wires in (first_wire, second_wire):
                peer_frames = [
                    ClassicEAFrame.decode_one(wire)[0] for wire in wires
                ]
                self.assertIn("+who", [frame.command for frame in peer_frames])
                self.assertEqual(peer_frames[-1].command, "+mgm")
                self.assertEqual(peer_frames[-1].fields()["COUNT"], "3")

    def test_pending_guest_onln_is_not_redirected_to_simultaneous_joiner(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = CredentialStore(root / "auth.json")
            for account_name, persona in (
                ("HostAccount", "Host"),
                ("FirstGuestAccount", "FirstGuest"),
                ("SecondGuestAccount", "SecondGuest"),
                ("WaitingGuestAccount", "WaitingGuest"),
            ):
                credentials.create_account(
                    account_name,
                    "password",
                    persona=persona,
                )
            identities = IdentityStore()
            auth = create_auth_service(
                credentials,
                identities,
                verify_passwords=False,
            )
            sessions = SessionDirectory()
            service = ClassicPreloginService(
                auth,
                profile=ClassicPreloginProfile(game_id="most_wanted"),
                control_endpoint=Endpoint("127.0.0.1", 13505),
                sessions=sessions,
                ranking=ClassicRankingStore(root / "stats.json"),
            )

            def make_context(account_name: str, persona: str, suffix: int):
                account = credentials.resolve_account(account_name)
                identity, token = identities.login(account_name, persona)
                return ClassicPreloginContext(
                    auth=ClassicAuthContext(
                        connection_id=f"mw-simultaneous-{suffix}",
                        account=account,
                        identity=identity,
                        session_token=token,
                        lkey=token,
                        persona=persona,
                    ),
                    authenticated=True,
                    persona_selected=True,
                    client_address=f"127.0.0.{suffix}",
                )

            host = make_context("HostAccount", "Host", 1)
            first = make_context("FirstGuestAccount", "FirstGuest", 2)
            second = make_context("SecondGuestAccount", "SecondGuest", 3)
            waiting = make_context("WaitingGuestAccount", "WaitingGuest", 4)
            host_id = host.auth.identity.user_id
            first_id = first.auth.identity.user_id
            second_id = second.auth.identity.user_id
            waiting_id = waiting.auth.identity.user_id
            game = sessions.create_game(
                0,
                host_id,
                capacity=4,
                min_players=2,
                name="Host",
                host_persona="Host",
                host_address=host.client_address,
            )
            self.assertTrue(sessions.join_game(game.game_id, first_id))
            self.assertTrue(sessions.join_game(game.game_id, second_id))
            for context in (host, first, second):
                user_id = context.auth.identity.user_id
                context.lobby_game_id = game.game_id
                game.participant_personas[user_id] = context.auth.persona
                game.participant_addresses[user_id] = context.client_address
            userset = ClassicUserset(
                userset_id=1,
                owner_id=host_id,
                owner_persona="Host",
                name="026.Host",
                game_id=game.game_id,
                members={host_id, first_id, second_id, waiting_id},
            )
            service._usersets[userset.userset_id] = userset
            for context in (host, first, second, waiting):
                context.userset_id = userset.userset_id
            first.mw_join_pending_game_id = game.game_id
            second.mw_join_pending_game_id = game.game_id
            first.mw_staged_onln_target_ids[game.game_id] = {second_id}
            with service._connections_lock:
                service._connections.update(
                    {
                        context.auth.identity.user_id: context
                        for context in (host, first, second, waiting)
                    }
                )

            requested_host = ClassicEAFrame.from_fields(
                "onln",
                (("PERS", "Host"),),
            )
            pending_reply = service.dispatch(requested_host, first)
            pending_online, remainder = ClassicEAFrame.decode_one(
                pending_reply.frames[0]
            )
            self.assertEqual(remainder, b"")
            self.assertEqual(pending_online.fields()["N"], "Host")
            self.assertEqual(
                pending_online.fields()["G"],
                str(game.game_id),
            )
            self.assertNotIn(first_id, second.mw_join_pending_viewer_ids)
            self.assertEqual(
                first.mw_staged_onln_target_ids[game.game_id],
                {second_id},
            )

            # Once the first guest is established, the same staged row has
            # the normal third-join meaning.  A lookup for another old guest
            # which remains in the host userset must still be honored: that
            # member is waiting to return to the replacement post-race game.
            first.mw_join_pending_game_id = 0
            waiting_reply = service.dispatch(
                ClassicEAFrame.from_fields(
                    "onln",
                    (("PERS", "WaitingGuest"),),
                ),
                first,
            )
            waiting_online, remainder = ClassicEAFrame.decode_one(
                waiting_reply.frames[0]
            )
            self.assertEqual(remainder, b"")
            self.assertEqual(waiting_online.fields()["N"], "WaitingGuest")
            self.assertEqual(waiting_online.fields()["G"], "0")
            self.assertNotIn(first_id, second.mw_join_pending_viewer_ids)
            self.assertEqual(
                first.mw_staged_onln_target_ids[game.game_id],
                {second_id},
            )

            # A lookup for an already established participant is the retail
            # staged-join trigger and may still bind to the new guest.
            staged_reply = service.dispatch(requested_host, first)
            staged_online, remainder = ClassicEAFrame.decode_one(
                staged_reply.frames[0]
            )
            self.assertEqual(remainder, b"")
            self.assertEqual(staged_online.fields()["N"], "SecondGuest")
            self.assertEqual(staged_online.fields()["G"], "0")
            self.assertIn(first_id, second.mw_join_pending_viewer_ids)

    def test_ready_ce_remains_client_owned(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = CredentialStore(root / "auth.json")
            auth = create_auth_service(
                credentials,
                IdentityStore(),
                verify_passwords=False,
            )
            sessions = SessionDirectory()
            service = ClassicPreloginService(
                auth,
                profile=ClassicPreloginProfile(game_id="most_wanted"),
                control_endpoint=Endpoint("127.0.0.1", 13505),
                sessions=sessions,
                ranking=ClassicRankingStore(root / "stats.json"),
            )
            game = sessions.create_game(0, 10, capacity=4, min_players=2)
            self.assertTrue(sessions.join_game(game.game_id, 20))
            self.assertTrue(sessions.join_game(game.game_id, 30))
            host_two_player = "SCF%3d0%0aCE%3d3,1%0aV%3d20%0a"
            service._mw_record_auxiliary(game, 10, host_two_player)
            game.participant_aux[10] = host_two_player

            self.assertEqual(
                service._mw_ready_state(game).auxiliary[10].get("CE"),
                "3,1",
            )
            self.assertEqual(game.participant_aux[10], host_two_player)

    def test_new_game_auxiliary_drops_retired_ce_without_forcing_state(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = CredentialStore(root / "auth.json")
            credentials.create_account(
                "HostAccount",
                "password",
                persona="Host",
            )
            identities = IdentityStore(token_factory=lambda: "host-token")
            auth = create_auth_service(
                credentials,
                identities,
                verify_passwords=False,
            )
            sessions = SessionDirectory()
            service = ClassicPreloginService(
                auth,
                profile=ClassicPreloginProfile(game_id="most_wanted"),
                control_endpoint=Endpoint("127.0.0.1", 13505),
                sessions=sessions,
                ranking=ClassicRankingStore(root / "stats.json"),
            )
            account = credentials.resolve_account("HostAccount")
            identity, token = identities.login("HostAccount", "Host")
            context = ClassicPreloginContext(
                auth=ClassicAuthContext(
                    connection_id="mw-host",
                    account=account,
                    identity=identity,
                    session_token=token,
                    lkey=token,
                    persona="Host",
                ),
                authenticated=True,
                persona_selected=True,
                client_address="127.0.0.1",
            )
            stale = (
                '"CN%3d123%0aLT%3d0%0aCE%3d3,3,5%0a'
                'SCF%3d0%0aV%3d20%0a"'
            )
            service._participant_aux[identity.user_id] = stale

            reply = service.dispatch(
                ClassicEAFrame.from_fields(
                    "gcre",
                    (("MAXSIZE", 4), ("MINSIZE", 2)),
                ),
                context,
            )

            self.assertEqual(reply.reason, "game_created")
            game = sessions.get_game(context.lobby_game_id)
            self.assertIsNotNone(game)
            inherited = game.participant_aux[identity.user_id]
            self.assertNotIn("CE%3d", inherited)
            self.assertIn("CN%3d123", inherited)
            self.assertIn("SCF%3d0", inherited)
            self.assertTrue(inherited.startswith('"'))
            self.assertTrue(inherited.endswith('"'))
            self.assertNotIn(
                "CE%3d",
                service._participant_aux[identity.user_id],
            )

    def test_guest_joining_new_game_drops_stale_ce_but_same_game_keeps_it(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = CredentialStore(root / "auth.json")
            for account_name, persona in (
                ("HostAccount", "Host"),
                ("GuestAccount", "Guest"),
            ):
                credentials.create_account(
                    account_name,
                    "password",
                    persona=persona,
                )
            tokens = iter(("host-token", "guest-token"))
            identities = IdentityStore(token_factory=lambda: next(tokens))
            auth = create_auth_service(
                credentials,
                identities,
                verify_passwords=False,
            )
            sessions = SessionDirectory()
            service = ClassicPreloginService(
                auth,
                profile=ClassicPreloginProfile(game_id="most_wanted"),
                control_endpoint=Endpoint("127.0.0.1", 13505),
                sessions=sessions,
                ranking=ClassicRankingStore(root / "stats.json"),
            )

            def context_for(account_name: str, persona: str):
                account = credentials.resolve_account(account_name)
                identity, token = identities.login(account_name, persona)
                return ClassicPreloginContext(
                    auth=ClassicAuthContext(
                        connection_id=f"mw-{persona.casefold()}",
                        account=account,
                        identity=identity,
                        session_token=token,
                        lkey=token,
                        persona=persona,
                    ),
                    authenticated=True,
                    persona_selected=True,
                    client_address="127.0.0.1",
                )

            host = context_for("HostAccount", "Host")
            guest = context_for("GuestAccount", "Guest")
            host_id = host.auth.identity.user_id
            guest_id = guest.auth.identity.user_id
            game = sessions.create_game(
                0,
                host_id,
                capacity=4,
                min_players=2,
                name="007.Host",
                host_persona="Host",
                host_address="127.0.0.1",
            )
            stale = "CN%3d456%0aCE%3d3,3,5%0aSCF%3d0%0aV%3d20%0a"
            service._participant_aux[guest_id] = stale

            joined = service.dispatch(
                ClassicEAFrame.from_fields(
                    "gjoi",
                    (("IDENT", game.game_id),),
                ),
                guest,
            )

            self.assertEqual(joined.reason, "game_joined")
            inherited = game.participant_aux[guest_id]
            self.assertNotIn("CE%3d", inherited)
            self.assertIn("CN%3d456", inherited)

            current = "CN%3d456%0aCE%3d3,1%0aSCF%3d0%0aV%3d20%0a"
            service._participant_aux[guest_id] = current
            game.participant_aux[guest_id] = current
            repeated = service.dispatch(
                ClassicEAFrame.from_fields(
                    "gjoi",
                    (("IDENT", game.game_id),),
                ),
                guest,
            )

            self.assertEqual(repeated.reason, "game_joined_repeat")
            self.assertEqual(game.participant_aux[guest_id], current)

    def test_open_room_guest_leave_preserves_other_guest_transport(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = CredentialStore(root / "auth.json")
            for account_name, persona in (
                ("HostAccount", "Host"),
                ("GuestOneAccount", "GuestOne"),
                ("GuestTwoAccount", "GuestTwo"),
            ):
                credentials.create_account(
                    account_name,
                    "password",
                    persona=persona,
                )
            tokens = iter(("host-token", "guest-one-token", "guest-two-token"))
            identities = IdentityStore(token_factory=lambda: next(tokens))
            auth = create_auth_service(
                credentials,
                identities,
                verify_passwords=False,
            )
            sessions = SessionDirectory()
            service = ClassicPreloginService(
                auth,
                profile=ClassicPreloginProfile(game_id="most_wanted"),
                control_endpoint=Endpoint("127.0.0.1", 13505),
                sessions=sessions,
                ranking=ClassicRankingStore(root / "stats.json"),
            )

            def context_for(account_name: str, persona: str):
                account = credentials.resolve_account(account_name)
                identity, token = identities.login(account_name, persona)
                return ClassicPreloginContext(
                    auth=ClassicAuthContext(
                        connection_id=f"mw-{persona.casefold()}",
                        account=account,
                        identity=identity,
                        session_token=token,
                        lkey=token,
                        persona=persona,
                    ),
                    authenticated=True,
                    persona_selected=True,
                )

            host = context_for("HostAccount", "Host")
            guest_one = context_for("GuestOneAccount", "GuestOne")
            guest_two = context_for("GuestTwoAccount", "GuestTwo")
            host_id = host.auth.identity.user_id
            guest_one_id = guest_one.auth.identity.user_id
            guest_two_id = guest_two.auth.identity.user_id
            game = sessions.create_game(
                0,
                host_id,
                capacity=4,
                min_players=2,
                host_persona="Host",
            )
            self.assertTrue(
                sessions.join_game(
                    game.game_id,
                    guest_one_id,
                    persona="GuestOne",
                )
            )
            self.assertTrue(
                sessions.join_game(
                    game.game_id,
                    guest_two_id,
                    persona="GuestTwo",
                )
            )
            game.participant_race_addresses = {
                host_id: "100.64.0.1",
                guest_one_id: "100.64.0.2",
                guest_two_id: "100.64.0.3",
            }
            for context in (host, guest_one, guest_two):
                context.lobby_game_id = game.game_id
            host_wire: list[bytes] = []
            guest_one_wire: list[bytes] = []
            host.send_wire = lambda wire: not host_wire.append(wire)
            guest_one.send_wire = lambda wire: not guest_one_wire.append(wire)
            service._connections = {
                host_id: host,
                guest_one_id: guest_one,
                guest_two_id: guest_two,
            }
            retired: list[int] = []
            synchronized: list[tuple[int, ...]] = []

            def register_remaining(current: GameSession) -> dict[int, str]:
                synchronized.append(current.ordered_participants())
                return dict(current.participant_race_addresses)

            service.set_race_relay(
                Endpoint("127.0.0.1", 20000),
                register_remaining,
                unregistrar=lambda current: not retired.append(current.game_id),
            )

            reply = service.dispatch(
                ClassicEAFrame.from_fields("glea", ()),
                guest_two,
            )

            self.assertEqual(reply.reason, "game_left")
            self.assertEqual(retired, [])
            self.assertTrue(synchronized)
            self.assertEqual(
                set(synchronized),
                {(host_id, guest_one_id)},
            )
            remaining = sessions.get_game(game.game_id)
            self.assertIs(remaining, game)
            self.assertEqual(remaining.state, SessionState.OPEN)
            self.assertEqual(remaining.participants, {host_id, guest_one_id})
            self.assertEqual(
                remaining.participant_race_addresses,
                {
                    host_id: "100.64.0.1",
                    guest_one_id: "100.64.0.2",
                },
            )
            self.assertEqual(guest_two.lobby_game_id, 0)
            for wire in (host_wire, guest_one_wire):
                managed = ClassicEAFrame.decode_one(wire[-1])[0]
                self.assertEqual(managed.command, "+mgm")
                self.assertEqual(managed.fields()["COUNT"], "2")
                self.assertEqual(managed.fields()["ADDR0"], "100.64.0.1")
                self.assertEqual(managed.fields()["ADDR1"], "100.64.0.2")

    def test_open_room_guest_disconnect_does_not_retire_owner_race_route(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = CredentialStore(root / "auth.json")
            credentials.create_account(
                "GuestAccount",
                "password",
                persona="Guest",
            )
            identities = IdentityStore(token_factory=lambda: "guest-token")
            auth = create_auth_service(
                credentials,
                identities,
                verify_passwords=False,
            )
            sessions = SessionDirectory()
            service = ClassicPreloginService(
                auth,
                profile=ClassicPreloginProfile(game_id="most_wanted"),
                control_endpoint=Endpoint("127.0.0.1", 13505),
                sessions=sessions,
                ranking=ClassicRankingStore(root / "stats.json"),
            )
            account = credentials.resolve_account("GuestAccount")
            identity, token = identities.login("GuestAccount", "Guest")
            context = ClassicPreloginContext(
                auth=ClassicAuthContext(
                    connection_id="mw-guest",
                    account=account,
                    identity=identity,
                    session_token=token,
                    lkey=token,
                    persona="Guest",
                ),
                authenticated=True,
                persona_selected=True,
            )
            owner_id = identity.user_id + 1
            game = sessions.create_game(
                0,
                owner_id,
                capacity=2,
                min_players=2,
                host_persona="Host",
            )
            self.assertTrue(
                sessions.join_game(
                    game.game_id,
                    identity.user_id,
                    persona="Guest",
                )
            )
            game.participant_race_addresses = {
                owner_id: "100.64.0.1",
                identity.user_id: "100.64.0.2",
            }
            context.lobby_game_id = game.game_id
            retired: list[int] = []
            service.set_race_relay(
                Endpoint("127.0.0.1", 20000),
                lambda current: dict(current.participant_race_addresses),
                unregistrar=lambda current: not retired.append(current.game_id),
            )

            service.release(context)

            preserved = sessions.get_game(game.game_id)
            self.assertIs(preserved, game)
            self.assertEqual(preserved.state, SessionState.OPEN)
            self.assertEqual(preserved.participants, {owner_id})
            self.assertEqual(
                preserved.participant_race_addresses,
                {owner_id: "100.64.0.1"},
            )
            self.assertEqual(retired, [])
            self.assertEqual(context.lobby_game_id, 0)

    def test_active_guest_passive_disconnect_preserves_room_and_reconnects(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = CredentialStore(root / "auth.json")
            credentials.create_account(
                "HostAccount",
                "password",
                persona="Host",
            )
            credentials.create_account(
                "GuestAccount",
                "password",
                persona="Guest",
            )
            tokens = iter(("host-token", "guest-token", "guest-reconnect-token"))
            identities = IdentityStore(token_factory=lambda: next(tokens))
            auth = create_auth_service(
                credentials,
                identities,
                verify_passwords=False,
            )
            sessions = SessionDirectory()
            service = ClassicPreloginService(
                auth,
                profile=ClassicPreloginProfile(game_id="most_wanted"),
                control_endpoint=Endpoint("127.0.0.1", 13505),
                sessions=sessions,
                ranking=ClassicRankingStore(root / "stats.json"),
            )

            def context_for(account_name: str, persona: str):
                account = credentials.resolve_account(account_name)
                identity, token = identities.login(account_name, persona)
                return ClassicPreloginContext(
                    auth=ClassicAuthContext(
                        connection_id=f"mw-{persona.casefold()}-{token}",
                        account=account,
                        identity=identity,
                        session_token=token,
                        lkey=token,
                        persona=persona,
                    ),
                    authenticated=True,
                    persona_selected=True,
                    client_address="127.0.0.1",
                )

            host = context_for("HostAccount", "Host")
            guest = context_for("GuestAccount", "Guest")
            host_id = host.auth.identity.user_id
            guest_id = guest.auth.identity.user_id
            game = sessions.create_game(
                0,
                host_id,
                capacity=3,
                min_players=2,
                host_persona="Host",
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
            game.participant_race_addresses = {
                host_id: "100.64.0.1",
                guest_id: "100.64.0.2",
            }
            game.participant_wire_ids = {host_id: 1, guest_id: 2}
            userset = ClassicUserset(
                1,
                host_id,
                "Host",
                "026.Host",
                game_id=game.game_id,
                members={host_id, guest_id},
            )
            service._usersets[userset.userset_id] = userset
            host.lobby_game_id = guest.lobby_game_id = game.game_id
            host.userset_id = guest.userset_id = userset.userset_id
            host.send_wire = lambda _wire: True
            guest.send_wire = lambda _wire: True
            service._connections = {host_id: host, guest_id: guest}
            retired: list[int] = []
            service.set_race_relay(
                Endpoint("127.0.0.1", 20000),
                lambda current: dict(current.participant_race_addresses),
                unregistrar=lambda current: not retired.append(current.game_id),
            )

            service.release(guest)

            preserved = sessions.get_game(game.game_id)
            self.assertIs(preserved, game)
            self.assertEqual(preserved.state, SessionState.ACTIVE)
            self.assertEqual(preserved.participants, {host_id, guest_id})
            self.assertEqual(
                preserved.participant_race_addresses,
                {host_id: "100.64.0.1", guest_id: "100.64.0.2"},
            )
            self.assertEqual(userset.members, {host_id, guest_id})
            self.assertEqual(retired, [])
            self.assertNotIn(guest_id, service._connections)
            self.assertEqual(guest.lobby_game_id, 0)
            self.assertEqual(guest.userset_id, 0)

            reconnected = context_for("GuestAccount", "Guest")
            reconnected.send_wire = lambda _wire: True
            service._register(reconnected)

            self.assertEqual(reconnected.auth.identity.user_id, guest_id)
            self.assertEqual(reconnected.userset_id, userset.userset_id)
            self.assertEqual(reconnected.lobby_game_id, game.game_id)
            self.assertIs(service._connections[guest_id], reconnected)

    def test_active_guest_ulea_detaches_only_guest_with_stock_actor_shape(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = CredentialStore(root / "auth.json")
            credentials.create_account(
                "HostAccount",
                "password",
                persona="Host",
            )
            credentials.create_account(
                "GuestAccount",
                "password",
                persona="Guest",
            )
            tokens = iter(("host-token", "guest-token"))
            identities = IdentityStore(token_factory=lambda: next(tokens))
            auth = create_auth_service(
                credentials,
                identities,
                verify_passwords=False,
            )
            sessions = SessionDirectory()
            service = ClassicPreloginService(
                auth,
                profile=ClassicPreloginProfile(game_id="most_wanted"),
                control_endpoint=Endpoint("127.0.0.1", 13505),
                sessions=sessions,
                ranking=ClassicRankingStore(root / "stats.json"),
            )

            def context_for(account_name: str, persona: str):
                account = credentials.resolve_account(account_name)
                identity, token = identities.login(account_name, persona)
                return ClassicPreloginContext(
                    auth=ClassicAuthContext(
                        connection_id=f"mw-{persona.casefold()}",
                        account=account,
                        identity=identity,
                        session_token=token,
                        lkey=token,
                        persona=persona,
                    ),
                    authenticated=True,
                    persona_selected=True,
                    client_address="127.0.0.1",
                )

            host = context_for("HostAccount", "Host")
            guest = context_for("GuestAccount", "Guest")
            host_id = host.auth.identity.user_id
            guest_id = guest.auth.identity.user_id
            game = sessions.create_game(
                0,
                host_id,
                capacity=2,
                min_players=2,
                host_persona="Host",
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
            game.participant_race_addresses = {
                host_id: "100.64.0.1",
                guest_id: "100.64.0.2",
            }
            game.participant_wire_ids = {host_id: 1, guest_id: 2}
            userset = ClassicUserset(
                1,
                host_id,
                "Host",
                "026.Host",
                game_id=game.game_id,
                members={host_id, guest_id},
            )
            service._usersets[userset.userset_id] = userset
            host.lobby_game_id = guest.lobby_game_id = game.game_id
            host.userset_id = guest.userset_id = userset.userset_id
            host_wire: list[bytes] = []
            host.send_wire = lambda wire: not host_wire.append(wire)
            service._connections = {host_id: host, guest_id: guest}
            retired: list[int] = []
            service.set_race_relay(
                Endpoint("127.0.0.1", 20000),
                lambda current: dict(current.participant_race_addresses),
                unregistrar=lambda current: not retired.append(current.game_id),
            )

            reply = service.dispatch(
                ClassicEAFrame.from_fields(
                    "ulea",
                    (("NAME", userset.name),),
                ),
                guest,
            )
            actor = [
                ClassicEAFrame.decode_one(wire)[0]
                for wire in reply.frames
            ]
            self.assertEqual(reply.reason, "userset_left")
            self.assertEqual(
                [frame.command for frame in actor],
                ["+who", "+mgm", "ulea", "+who"],
            )
            self.assertEqual(actor[1].fields()["COUNT"], "1")
            self.assertEqual(actor[-1].fields()["US"], "")

            remaining = sessions.get_game(game.game_id)
            self.assertIs(remaining, game)
            self.assertEqual(remaining.state, SessionState.ACTIVE)
            self.assertEqual(remaining.participants, {host_id})
            self.assertEqual(
                remaining.participant_race_addresses,
                {host_id: "100.64.0.1"},
            )
            self.assertEqual(userset.members, {host_id})
            self.assertEqual(guest.lobby_game_id, 0)
            self.assertEqual(guest.userset_id, 0)
            self.assertEqual(retired, [])

            peer = [
                ClassicEAFrame.decode_one(wire)[0]
                for wire in host_wire
            ]
            self.assertEqual(
                [frame.command for frame in peer],
                ["+ust", "+usm", "+mgm"],
            )
            self.assertEqual(peer[0].fields()["C"], "1")
            self.assertEqual(
                peer[1].fields()["I"],
                str(service._mw_wire_user_id(guest_id)),
            )
            self.assertEqual(peer[1].fields()["S"], "0")
            self.assertNotIn("N", peer[1].fields())
            self.assertEqual(peer[2].fields()["COUNT"], "1")

    def test_owner_gcre_hands_active_transport_to_replacement(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = CredentialStore(root / "auth.json")
            credentials.create_account(
                "HostAccount",
                "password",
                persona="Host",
            )
            identities = IdentityStore(token_factory=lambda: "host-token")
            auth = create_auth_service(
                credentials,
                identities,
                verify_passwords=False,
            )
            sessions = SessionDirectory()
            service = ClassicPreloginService(
                auth,
                profile=ClassicPreloginProfile(game_id="most_wanted"),
                control_endpoint=Endpoint("127.0.0.1", 13505),
                sessions=sessions,
                ranking=ClassicRankingStore(root / "stats.json"),
            )
            account = credentials.resolve_account("HostAccount")
            identity, token = identities.login("HostAccount", "Host")
            host = ClassicPreloginContext(
                auth=ClassicAuthContext(
                    connection_id="mw-host",
                    account=account,
                    identity=identity,
                    session_token=token,
                    lkey=token,
                    persona="Host",
                ),
                authenticated=True,
                persona_selected=True,
                client_address="127.0.0.1",
            )

            old_game = sessions.create_game(
                0,
                identity.user_id,
                capacity=4,
                min_players=2,
                name="Host",
                host_persona="Host",
                host_address="127.0.0.1",
            )
            guest_one = identity.user_id + 1
            guest_two = identity.user_id + 2
            self.assertTrue(sessions.join_game(old_game.game_id, guest_one))
            self.assertTrue(sessions.join_game(old_game.game_id, guest_two))
            sessions.set_state(old_game.game_id, SessionState.ACTIVE)
            old_addresses = {
                identity.user_id: "100.64.0.1",
                guest_one: "100.64.0.2",
                guest_two: "100.64.0.3",
            }
            old_game.participant_race_addresses = dict(old_addresses)
            host.lobby_game_id = old_game.game_id

            handoffs: list[tuple[int, int]] = []
            retired: list[int] = []

            def handoff(previous, replacement):
                handoffs.append((previous.game_id, replacement.game_id))
                return dict(old_addresses)

            service.set_race_relay(
                Endpoint("127.0.0.1", 20000),
                lambda current: dict(current.participant_race_addresses),
                unregistrar=lambda current: not retired.append(current.game_id),
                handoff=handoff,
            )

            reply = service.dispatch(
                ClassicEAFrame.from_fields(
                    "gcre",
                    (
                        ("NAME", "Host-2"),
                        ("MAXSIZE", 4),
                        ("MINSIZE", 2),
                    ),
                ),
                host,
            )

            self.assertEqual(reply.reason, "game_created")
            replacement = sessions.get_game(host.lobby_game_id)
            self.assertIsNotNone(replacement)
            self.assertEqual(replacement.game_id, 2)
            self.assertEqual(handoffs, [(1, 2)])
            self.assertEqual(retired, [])
            self.assertIsNone(sessions.get_game(old_game.game_id))
            self.assertEqual(old_game.participant_race_addresses, {})
            self.assertEqual(
                replacement.participant_race_addresses,
                old_addresses,
            )

    def test_gjoi_callback_returns_token_game_usr_and_gam(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = CredentialStore(root / "auth.json")
            identities = IdentityStore()
            auth = create_auth_service(
                credentials,
                identities,
                verify_passwords=False,
            )
            sessions = SessionDirectory()
            service = ClassicPreloginService(
                auth,
                profile=ClassicPreloginProfile(game_id="most_wanted"),
                control_endpoint=Endpoint("127.0.0.1", 13505),
                sessions=sessions,
                ranking=ClassicRankingStore(root / "stats.json"),
            )

            contexts = []
            for index, persona in enumerate(("CallbackHost", "CallbackGuest")):
                account_name = f"{persona}Account"
                credentials.create_account(
                    account_name,
                    "password",
                    persona=persona,
                )
                account = credentials.resolve_account(account_name)
                identity, token = identities.login(account_name, persona)
                contexts.append(
                    ClassicPreloginContext(
                        auth=ClassicAuthContext(
                            connection_id=f"mw-callback-{index}",
                            account=account,
                            identity=identity,
                            session_token=token,
                            lkey=token,
                            persona=persona,
                            client_ip=f"127.0.0.{index + 1}",
                        ),
                        authenticated=True,
                        persona_selected=True,
                        client_address=f"127.0.0.{index + 1}",
                        client_port=4000 + index,
                        send_wire=lambda _wire: True,
                    )
                )

            host, guest = contexts
            host_id = host.auth.identity.user_id
            guest_id = guest.auth.identity.user_id
            game = sessions.create_game(
                0,
                host_id,
                capacity=4,
                min_players=2,
                include_owner=True,
                host_persona="CallbackHost",
                host_address="127.0.0.1",
            )
            game.name = "CallbackHost"
            self.assertTrue(
                sessions.join_game(
                    game.game_id,
                    guest_id,
                    persona="CallbackGuest",
                    address="127.0.0.2",
                )
            )
            host.lobby_game_id = game.game_id
            guest.lobby_game_id = game.game_id
            with service._connections_lock:
                service._connections[host_id] = host
                service._connections[guest_id] = guest
            guest_wire_id = service._mw_wire_user_id(guest_id)
            token_word = 0xFFFFE218
            packet = ClassicEAFrame.from_fields(
                "GJOI",
                (
                    ("CALLUSER", guest_wire_id),
                    ("CALLPING", 575),
                    ("CALLADDR", "127.0.0.2"),
                    ("NAME", "CallbackHost"),
                ),
                reserved=token_word,
                separator="\t",
                final_separator=False,
            )

            reply = service.dispatch(packet, host)
            self.assertEqual(reply.reason, "game_joined_callback")
            self.assertEqual(len(reply.frames), 3)
            self.assertEqual(
                reply.frames[0][:4],
                struct.pack(">I", token_word),
            )
            token_game, remainder = ClassicEAFrame.decode_one(
                b"GJOI" + reply.frames[0][4:]
            )
            self.assertEqual(remainder, b"")
            self.assertEqual(token_game.fields()["IDENT"], str(game.game_id))
            self.assertEqual(token_game.fields()["COUNT"], "2")
            self.assertEqual(token_game.fields()["OPPO1"], "CallbackGuest")

            usr, remainder = ClassicEAFrame.decode_one(reply.frames[1])
            self.assertEqual(remainder, b"")
            self.assertEqual(usr.command, "+usr")
            self.assertEqual(usr.fields()["IDENT"], str(guest_wire_id))
            self.assertEqual(usr.fields()["NAME"], "CallbackGuest")
            self.assertEqual(usr.fields()["GAME"], str(game.game_id))
            self.assertEqual(usr.fields()["ADDR"], "127.0.0.2")

            gam, remainder = ClassicEAFrame.decode_one(reply.frames[2])
            self.assertEqual(remainder, b"")
            self.assertEqual(gam.command, "+gam")
            self.assertEqual(gam.fields()["IDENT"], str(game.game_id))
            self.assertEqual(gam.fields()["COUNT"], "2")

    def test_three_player_session_frames_keep_join_slot_order(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = CredentialStore(root / "auth.json")
            identities = IdentityStore()
            auth = create_auth_service(
                credentials,
                identities,
                verify_passwords=False,
            )
            sessions = SessionDirectory()
            service = ClassicPreloginService(
                auth,
                profile=ClassicPreloginProfile(game_id="most_wanted"),
                control_endpoint=Endpoint("127.0.0.1", 13505),
                sessions=sessions,
                ranking=ClassicRankingStore(root / "stats.json"),
            )

            contexts = []
            for index in range(3):
                account_name = f"Order{index}Account"
                persona = f"Order{index}"
                credentials.create_account(
                    account_name,
                    "password",
                    persona=persona,
                )
                account = credentials.resolve_account(account_name)
                identity, token = identities.login(account_name, persona)
                contexts.append(
                    ClassicPreloginContext(
                        auth=ClassicAuthContext(
                            connection_id=f"mw-order-{index}",
                            account=account,
                            identity=identity,
                            session_token=token,
                            lkey=token,
                            persona=persona,
                        ),
                        authenticated=True,
                        persona_selected=True,
                        client_address=f"127.0.0.{index + 1}",
                    )
                )

            by_id = {context.auth.identity.user_id: context for context in contexts}
            ordered_ids = sorted(by_id)
            owner_id = ordered_ids[1]
            guest_ids = [user_id for user_id in ordered_ids if user_id != owner_id]
            first_guest_id = max(guest_ids)
            third_player_id = min(guest_ids)
            owner = by_id[owner_id]

            game = sessions.create_game(
                0,
                owner_id,
                capacity=4,
                min_players=2,
                name="OrderRoom",
                host_persona=owner.auth.persona,
                host_address=owner.client_address,
            )
            self.assertTrue(sessions.join_game(game.game_id, first_guest_id))
            self.assertTrue(sessions.join_game(game.game_id, third_player_id))
            for user_id, context in by_id.items():
                game.participant_personas[user_id] = context.auth.persona
                game.participant_addresses[user_id] = context.client_address
                game.participant_aux[user_id] = "SCF%3d0%0aV%3d20%0a"
            with service._connections_lock:
                service._connections.update(by_id)

            expected_ids = (owner_id, first_guest_id, third_player_id)
            expected_names = [by_id[user_id].auth.persona for user_id in expected_ids]
            self.assertEqual(game.ordered_participants(), expected_ids)

            def usm_names(frames: tuple[bytes, ...]) -> list[str]:
                names: list[str] = []
                for wire in frames:
                    frame, remainder = ClassicEAFrame.decode_one(wire)
                    self.assertEqual(remainder, b"")
                    if frame.command == "+usm":
                        names.append(frame.fields()["N"])
                return names

            self.assertEqual(
                usm_names(service._mw_start_frames(owner, game, 1234)),
                expected_names,
            )
            self.assertEqual(
                usm_names(service._mw_ready_refresh_frames(owner, game)),
                expected_names,
            )
            self.assertEqual(
                usm_names(service._mw_postrace_room_frames(owner, game)),
                expected_names,
            )

            for context in contexts:
                context.lobby_game_id = game.game_id
            delivered: dict[int, list[bytes]] = {
                user_id: [] for user_id in expected_ids
            }
            for user_id, context in by_id.items():
                context.send_wire = (
                    lambda wire, recipient=user_id: not delivered[
                        recipient
                    ].append(wire)
                )

            actor_id = third_player_id
            actor = by_id[actor_id]
            egs = service.dispatch(
                ClassicEAFrame.from_fields(
                    "mesg",
                    (("TEXT", "42"), ("ATTR", "EGS")),
                ),
                actor,
            )
            self.assertEqual(
                [ClassicEAFrame.decode_one(wire)[0].command for wire in egs.frames],
                ["mesg", "+msg"],
            )
            for peer_id in expected_ids:
                if peer_id == actor_id:
                    continue
                peer_frames = [
                    ClassicEAFrame.decode_one(wire)[0]
                    for wire in delivered[peer_id]
                ]
                self.assertEqual(
                    [frame.command for frame in peer_frames],
                    ["+msg"],
                )
                self.assertEqual(peer_frames[0].fields()["N"], actor.auth.persona)

            for wires in delivered.values():
                wires.clear()
            auxiliary = service.dispatch(
                ClassicEAFrame.from_fields(
                    "auxi",
                    (("TEXT", "SCF%3d0%0aLT%3d23%0aV%3d20%0a"),),
                ),
                actor,
            )
            actor_frames = [
                ClassicEAFrame.decode_one(wire)[0] for wire in auxiliary.frames
            ]
            self.assertEqual(
                [frame.command for frame in actor_frames],
                ["auxi", "+who", "+usm"],
            )
            self.assertEqual(actor_frames[1].fields()["N"], actor.auth.persona)
            self.assertEqual(actor_frames[2].fields()["N"], actor.auth.persona)
            for peer_id in expected_ids:
                if peer_id == actor_id:
                    continue
                peer_frames = [
                    ClassicEAFrame.decode_one(wire)[0]
                    for wire in delivered[peer_id]
                ]
                self.assertEqual(
                    [frame.command for frame in peer_frames],
                    ["+usm"],
                )
                self.assertEqual(peer_frames[0].fields()["N"], actor.auth.persona)


if __name__ == "__main__":
    unittest.main()
