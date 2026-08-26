from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from classic.accounts.credentials import CredentialStore
from classic.accounts.identity import IdentityStore
from classic.core.config import Endpoint
from classic.ea.directory import SessionDirectory
from classic.ea.ranking import ClassicRankingStore
from classic.games.underground2.auth import create_auth_service
from classic.protocols.auth import ClassicAuthContext
from classic.protocols.frame import ClassicEAFrame
from classic.protocols.prelogin import (
    ClassicPreloginContext,
    ClassicPreloginProfile,
    ClassicPreloginService,
)


class AdaptiveRaceEndpointTests(unittest.TestCase):
    @staticmethod
    def _service(root: Path, game_id: str):
        credentials = CredentialStore(root / f"{game_id}-auth.json")
        credentials.create_account("HostAccount", "password", persona="Host")
        credentials.create_account("GuestAccount", "password", persona="Guest")
        identities = IdentityStore(token_factory=lambda: "token")
        auth = create_auth_service(credentials, identities, verify_passwords=False)
        sessions = SessionDirectory()
        service = ClassicPreloginService(
            auth,
            profile=ClassicPreloginProfile(game_id=game_id),
            control_endpoint=Endpoint("public.example", 13505),
            sessions=sessions,
            ranking=ClassicRankingStore(root / f"{game_id}-stats.json"),
        )

        def endpoint_for_client(endpoint: Endpoint, client_ip: str) -> Endpoint:
            if client_ip == "127.0.0.1":
                return Endpoint("127.0.0.1", endpoint.port)
            if client_ip.startswith("192.168."):
                return Endpoint("192.168.1.10", endpoint.port)
            return endpoint

        service.set_endpoint_resolver(endpoint_for_client)

        host_account = credentials.resolve_account("HostAccount")
        guest_account = credentials.resolve_account("GuestAccount")
        host_identity, host_token = identities.login("HostAccount", "Host")
        guest_identity, guest_token = identities.login("GuestAccount", "Guest")
        host = ClassicPreloginContext(
            auth=ClassicAuthContext(
                connection_id="host",
                client_ip="127.0.0.1",
                account=host_account,
                identity=host_identity,
                session_token=host_token,
                lkey=host_token,
                persona="Host",
            ),
            client_address="127.0.0.1",
            authenticated=True,
            persona_selected=True,
            send_wire=lambda _wire: True,
        )
        guest = ClassicPreloginContext(
            auth=ClassicAuthContext(
                connection_id="guest",
                client_ip="198.51.100.180",
                account=guest_account,
                identity=guest_identity,
                session_token=guest_token,
                lkey=guest_token,
                persona="Guest",
            ),
            # Simulate the game's ADDR command declaring a LAN address even
            # though the actual TCP peer is public.
            client_address="192.168.1.50",
            authenticated=True,
            persona_selected=True,
            send_wire=lambda _wire: True,
        )
        service._register(host)
        service._register(guest)
        game = sessions.create_game(
            0,
            host_identity.user_id,
            capacity=2,
            min_players=2,
            host_persona="Host",
        )
        sessions.join_game(game.game_id, guest_identity.user_id, persona="Guest")
        return service, game, host_identity.user_id, guest_identity.user_id

    def test_u2_relay_host_is_adapted_per_viewer(self) -> None:
        with TemporaryDirectory() as temporary:
            service, game, host_id, guest_id = self._service(
                Path(temporary), "underground2"
            )
            public = Endpoint("203.0.113.7", 20000)
            service.set_race_relay(
                public,
                lambda current: {
                    user_id: "203.0.113.7" for user_id in current.participants
                },
            )
            host_fields = dict(service._game_fields(game, viewer_id=host_id, start=True))
            guest_fields = dict(service._game_fields(game, viewer_id=guest_id, start=True))
            self.assertEqual(host_fields["RLYHOST"], "127.0.0.1")
            self.assertEqual(host_fields["RLYPORT"], 20000)
            self.assertEqual(guest_fields["RLYHOST"], "203.0.113.7")
            self.assertEqual(guest_fields["RLYPORT"], 20000)

    def test_u2_uses_one_public_relay_port_for_six_participants(self) -> None:
        with TemporaryDirectory() as temporary:
            service, game, host_id, guest_id = self._service(
                Path(temporary), "underground2"
            )
            game.capacity = 6
            extra_ids = (900_001, 900_002, 900_003, 900_004)
            game.participants.update(extra_ids)
            service.set_race_relay(
                Endpoint("203.0.113.7", 20000),
                lambda current: {
                    user_id: f"100.64.0.{index + 1}"
                    for index, user_id in enumerate(
                        sorted(
                            current.participants,
                            key=lambda candidate: (
                                candidate != current.owner_id,
                                candidate,
                            ),
                        )
                    )
                },
                Endpoint("203.0.113.7", 20001),
                Endpoint("203.0.113.7", 20002),
                Endpoint("203.0.113.7", 20003),
                Endpoint("203.0.113.7", 20004),
                Endpoint("203.0.113.7", 20005),
            )
            ordered = sorted(
                game.participants,
                key=lambda user_id: (user_id != game.owner_id, user_id),
            )
            for user_id in ordered:
                fields = dict(
                    service._game_fields(
                        game,
                        viewer_id=user_id,
                        start=True,
                    )
                )
                self.assertEqual(fields["RLYPORT"], 20000)
            self.assertEqual(ordered[0], host_id)
            self.assertIn(guest_id, ordered)

    def test_mw_uses_one_public_relay_port_for_every_viewer(self) -> None:
        with TemporaryDirectory() as temporary:
            service, game, host_id, guest_id = self._service(
                Path(temporary), "most_wanted"
            )
            service.set_race_relay(
                Endpoint("203.0.113.7", 20000),
                lambda current: {
                    user_id: f"100.64.0.{index + 1}"
                    for index, user_id in enumerate(sorted(current.participants))
                },
                Endpoint("203.0.113.7", 20001),
            )
            host_fields = dict(
                service._mw_game_fields(game, viewer_id=host_id, active=False)
            )
            guest_fields = dict(
                service._mw_game_fields(game, viewer_id=guest_id, active=False)
            )
            self.assertEqual(host_fields["RLYHOST"], "127.0.0.1")
            self.assertEqual(host_fields["RLYPORT"], 20000)
            self.assertEqual(guest_fields["RLYHOST"], "203.0.113.7")
            self.assertEqual(guest_fields["RLYPORT"], 20000)

            third_id = 1
            while third_id in game.participants:
                third_id += 1
            game.participants.add(third_id)
            game.participant_personas[third_id] = "Third"
            game.participant_wire_ids = {
                # Reproduce a long-running lobby where the later guest owns
                # the earliest global wire ID.  OPPO slots must not follow it.
                host_id: 3,
                guest_id: 2,
                third_id: 1,
            }
            service._wire_user_ids = dict(game.participant_wire_ids)
            service._next_wire_user_id = 4
            service.set_race_relay(
                Endpoint("203.0.113.7", 20000),
                lambda current: {
                    user_id: f"100.64.0.{index + 1}"
                    for index, user_id in enumerate(current.participants)
                },
                Endpoint("203.0.113.7", 20001),
                Endpoint("203.0.113.7", 20002),
            )
            stable_guest = dict(
                service._mw_game_fields(game, viewer_id=guest_id, active=False)
            )
            third = dict(
                service._mw_game_fields(game, viewer_id=third_id, active=False)
            )
            self.assertEqual(stable_guest["RLYPORT"], 20000)
            self.assertEqual(third["RLYPORT"], 20000)
            self.assertEqual(stable_guest["OPPO0"], "Host")
            self.assertEqual(stable_guest["OPPO1"], "Guest")
            self.assertEqual(stable_guest["OPPO2"], "Third")
            self.assertEqual(stable_guest["OPID0"], 3)
            self.assertEqual(stable_guest["OPID1"], 2)
            self.assertEqual(stable_guest["OPID2"], 1)
            ordered_names = [
                name
                for name, _value in service._mw_game_fields(
                    game,
                    viewer_id=third_id,
                    active=False,
                )
            ]
            self.assertGreater(
                ordered_names.index("RLYHOST"),
                ordered_names.index("OPPARAM2"),
            )
            self.assertGreater(
                ordered_names.index("RLYPORT"),
                ordered_names.index("OPPARAM2"),
            )

    def test_news_uses_tcp_peer_after_addr_declares_private_address(self) -> None:
        with TemporaryDirectory() as temporary:
            service, _game, _host_id, guest_id = self._service(
                Path(temporary), "underground2"
            )
            guest = service._context_for_user(guest_id)
            self.assertIsNotNone(guest)
            assert guest is not None

            address_reply = service.dispatch(
                ClassicEAFrame.from_fields(
                    "addr",
                    (("ADDR", "192.168.1.50"), ("PORT", 3658)),
                ),
                guest,
            )
            self.assertEqual(address_reply.reason, "address")
            self.assertEqual(guest.client_address, "192.168.1.50")
            self.assertEqual(guest.auth.client_ip, "198.51.100.180")

            news_reply = service.dispatch(ClassicEAFrame("news"), guest)
            self.assertEqual(news_reply.reason, "news")
            frame, remaining = ClassicEAFrame.decode_one(news_reply.frames[0])
            self.assertEqual(remaining, b"")
            fields = frame.fields()
            self.assertEqual(fields["BUDDY_SERVER"], "public.example")
            self.assertIn("http://public.example", fields["NEWSURL"])


if __name__ == "__main__":
    unittest.main()
