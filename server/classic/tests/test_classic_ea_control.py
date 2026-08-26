from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from classic.core.catalog import GameId
from classic.ea.messenger import EAMessengerFrame
from classic.ea.social import SocialService
from classic.protocols.control import (
    ClassicControlContext,
    ClassicControlProfile,
    ClassicControlService,
)
from classic.protocols.frame import ClassicEAFrame
from classic.protocols.messenger import ClassicMessengerAdapter


class SharedClassicControlTests(unittest.TestCase):
    @staticmethod
    def _frame(verb: str, fields=()) -> ClassicEAFrame:
        return ClassicEAFrame.from_fields(verb, fields)

    def test_u2_and_mw_share_social_graph_but_resolve_their_own_lobby(self) -> None:
        social = SocialService(persona_provider=lambda: ("Alice", "Bob"))
        social.register_lobby(
            "u2-lobby",
            "alice-account",
            "Alice",
            "192.0.2.10",
            game_id=GameId.UNDERGROUND2.value,
        )
        social.register_lobby(
            "mw-lobby",
            "bob-account",
            "Bob",
            "192.0.2.11",
            game_id=GameId.MOST_WANTED.value,
        )
        u2 = ClassicControlService(
            social,
            profile=ClassicControlProfile.for_game(GameId.UNDERGROUND2),
        )
        mw = ClassicControlService(
            social,
            profile=ClassicControlProfile.for_game(GameId.MOST_WANTED),
        )
        alice_events: list[tuple[str, dict[str, str]]] = []
        bob_events: list[tuple[str, dict[str, str]]] = []

        def sender(events):
            def send(verb: str, fields: tuple[tuple[str, str], ...]) -> bool:
                events.append((verb, dict(fields)))
                return True
            return send

        alice = ClassicControlContext("control-a", "192.0.2.10")
        bob = ClassicControlContext("control-b", "192.0.2.11")
        alice_sender = sender(alice_events)
        bob_sender = sender(bob_events)

        self.assertTrue(u2.can_authenticate("192.0.2.10", {"PERS": "Alice"}))
        self.assertFalse(mw.can_authenticate("192.0.2.10", {"PERS": "Alice"}))
        self.assertTrue(mw.can_authenticate("192.0.2.11", {"PERS": "Bob"}))
        self.assertFalse(u2.can_authenticate("192.0.2.11", {"PERS": "Bob"}))

        self.assertEqual(
            u2.dispatch(self._frame("AUTH", (("PERS", "Alice"),)), alice, alice_sender).reason,
            "authenticated",
        )
        self.assertEqual(
            mw.dispatch(self._frame("AUTH", (("PERS", "Bob"),)), bob, bob_sender).reason,
            "authenticated",
        )

        request = u2.dispatch(
            self._frame("RADD", (("ID", "1"), ("LIST", "B"), ("USER", "Bob"))),
            alice,
            alice_sender,
        )
        self.assertEqual(request.reason, "requested")
        self.assertIn(("RNOT", {"CHNG": "A", "USER": "Alice", "ATTR": "R"}), bob_events)

        accept = mw.dispatch(
            self._frame("RRSP", (("ID", "2"), ("USER", "Alice"), ("ANSW", "Y"))),
            bob,
            bob_sender,
        )
        self.assertEqual(accept.reason, "accepted")

        message = u2.dispatch(
            self._frame("PMSG", (("ID", "3"), ("USER", "Bob"), ("TEXT", "hello"))),
            alice,
            alice_sender,
        )
        ack, trailing = ClassicEAFrame.decode_one(message.frames[0])
        self.assertEqual(trailing, b"")
        self.assertEqual(ack.fields()["DELIVERED"], "1")
        self.assertIn(
            ("PMSG", {"USER": "Alice", "FROM": "Alice", "TEXT": "hello"}),
            bob_events,
        )

    def test_social_relations_persist(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "social.json"
            social = SocialService(path)
            self.assertTrue(social.request_friend("Alice", "Bob").accepted)
            self.assertTrue(social.respond_friend("Bob", "Alice", True).accepted)
            self.assertTrue(social.set_blocked("Alice", "Eve", True).accepted)

            loaded = SocialService(path)
            self.assertEqual([row.user for row in loaded.snapshot("Alice", "B")], ["Bob"])
            self.assertEqual([row.user for row in loaded.snapshot("Alice", "I")], ["Eve"])

    def test_stock_lkey_auth_selects_the_active_u2_messenger(self) -> None:
        social = SocialService(persona_provider=lambda: ("Alice",))
        social.register_lobby(
            "u2-lobby",
            "alice-account",
            "Alice",
            "192.0.2.10",
            game_id=GameId.UNDERGROUND2.value,
            session_token="session",
        )
        u2_service = ClassicControlService(
            social,
            profile=ClassicControlProfile.for_game(GameId.UNDERGROUND2),
        )
        mw_service = ClassicControlService(
            social,
            profile=ClassicControlProfile.for_game(GameId.MOST_WANTED),
        )
        frame = EAMessengerFrame(
            "AUTH",
            0,
            b"LKEY=stock-key\nPRES=1\nPROD=NFS-CONSOLE-2005\n"
            b"USER=Alice\nVERS=1\n\x00",
        )

        self.assertTrue(
            ClassicMessengerAdapter(
                u2_service,
                GameId.UNDERGROUND2,
            ).matches(frame, ("192.0.2.10", 40000))
        )
        self.assertFalse(
            ClassicMessengerAdapter(
                mw_service,
                GameId.MOST_WANTED,
            ).matches(frame, ("192.0.2.10", 40000))
        )

    def test_stock_u2_messenger_waits_for_lobby_and_accepts_pset_first(self) -> None:
        social = SocialService()
        service = ClassicControlService(
            social,
            profile=ClassicControlProfile.for_game(GameId.UNDERGROUND2),
        )
        adapter = ClassicMessengerAdapter(service, GameId.UNDERGROUND2)
        auth = EAMessengerFrame(
            "AUTH",
            0,
            b"LKEY=stock-key\nPRES=1\nPROD=NFS-CONSOLE-2005\n"
            b"USER=Alice\nVERS=1\n\x00",
        )
        sent: list[bytes] = []
        context = adapter.open(
            ("192.0.2.10", 40000),
            lambda wire: not sent.append(wire),
            now=0.0,
        )

        self.assertTrue(adapter.matches(auth, ("192.0.2.10", 40000)))
        self.assertEqual(adapter.dispatch(auth, context, now=0.0), [])
        self.assertEqual(adapter.poll(context, now=0.1), [])

        social.register_lobby(
            "lobby-1",
            "alice-account",
            "Alice",
            "192.0.2.10",
            game_id=GameId.UNDERGROUND2.value,
            session_token="session",
        )
        replies = adapter.poll(context, now=0.5)
        self.assertEqual(len(replies), 1)
        self.assertEqual(ClassicEAFrame.decode_one(replies[0])[0].command, "AUTH")
        self.assertTrue(context.control.authenticated)

        pset = EAMessengerFrame(
            "PSET",
            0,
            b"PROD=is playing Underground 2\nRSRC=PC\nSHOW=PASS\nSTAT=online\n\x00",
        )
        one_shot = adapter.open(
            ("192.0.2.10", 40001),
            lambda _wire: True,
            now=1.0,
        )
        self.assertTrue(adapter.matches(pset, ("192.0.2.10", 40001)))
        pset_replies = adapter.dispatch(pset, one_shot, now=1.0)
        self.assertEqual(len(pset_replies), 1)
        self.assertEqual(
            ClassicEAFrame.decode_one(pset_replies[0])[0].command,
            "PSET",
        )

    def test_personaless_pset_selects_u2_with_two_claimed_same_ip_lobbies(self) -> None:
        social = SocialService()
        u2_service = ClassicControlService(
            social,
            profile=ClassicControlProfile.for_game(GameId.UNDERGROUND2),
        )
        mw_service = ClassicControlService(
            social,
            profile=ClassicControlProfile.for_game(GameId.MOST_WANTED),
        )
        for index, persona in enumerate(("Host", "Guest"), 1):
            social.register_lobby(
                f"u2-lobby-{index}",
                f"account-{index}",
                persona,
                "127.0.0.1",
                game_id=GameId.UNDERGROUND2.value,
            )
            self.assertIsNotNone(
                social.register_control(
                    f"control-{index}",
                    "127.0.0.1",
                    persona,
                    lambda _verb, _fields: True,
                    game_id=GameId.UNDERGROUND2.value,
                )
            )

        pset = EAMessengerFrame(
            "PSET",
            0,
            b"PROD=NFS\nRSRC=PC\nSHOW=PASS\nSTAT=online\n\x00",
        )
        self.assertTrue(
            ClassicMessengerAdapter(
                u2_service,
                GameId.UNDERGROUND2,
            ).matches(pset, ("127.0.0.1", 40000))
        )
        self.assertFalse(
            ClassicMessengerAdapter(
                mw_service,
                GameId.MOST_WANTED,
            ).matches(pset, ("127.0.0.1", 40000))
        )

    def test_mw_roster_includes_same_session_player_list_without_cross_title_entries(self) -> None:
        social = SocialService(persona_provider=lambda: ("Alice", "Bob", "Carol"))
        social.register_lobby("alice-mw", "alice", "Alice", "192.0.2.10", game_id=GameId.MOST_WANTED.value)
        social.register_lobby("bob-mw", "bob", "Bob", "192.0.2.11", game_id=GameId.MOST_WANTED.value)
        social.register_lobby("carol-u2", "carol", "Carol", "192.0.2.12", game_id=GameId.UNDERGROUND2.value)
        social.set_game_session("alice-mw", "Alice", GameId.MOST_WANTED.value, "game-1")
        social.set_game_session("bob-mw", "Bob", GameId.MOST_WANTED.value, "game-1")
        social.set_game_session("carol-u2", "Carol", GameId.UNDERGROUND2.value, "game-1")
        service = ClassicControlService(
            social,
            profile=ClassicControlProfile.for_game(GameId.MOST_WANTED),
        )
        context = ClassicControlContext("control-a", "192.0.2.10")
        self.assertEqual(
            service.dispatch(self._frame("AUTH", (("PERS", "Alice"),)), context, lambda *_: True).reason,
            "authenticated",
        )
        reply = service.dispatch(
            self._frame("RGET", (("LIST", "B"), ("ID", "7"))),
            context,
            lambda *_: True,
        )
        frames = [ClassicEAFrame.decode_one(wire)[0] for wire in reply.frames]
        roster = [frame.fields() for frame in frames if frame.command == "ROST"]
        self.assertEqual(roster, [{"ID": "7", "USER": "Bob", "ATTR": "D"}])
        self.assertEqual(frames[0].fields()["SIZE"], "1")


if __name__ == "__main__":
    unittest.main()
