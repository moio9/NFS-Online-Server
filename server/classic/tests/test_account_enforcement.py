from __future__ import annotations

from hashlib import md5
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from classic.accounts.credentials import CredentialStore
from classic.accounts.identity import IdentityStore
from classic.core.config import Endpoint
from classic.ea.directory import SessionDirectory, SessionState
from classic.ea.ranking import ClassicRankingStore
from classic.games.most_wanted.auth import create_auth_service
from classic.games.underground2.auth import (
    create_auth_service as create_u2_auth_service,
)
from classic.protocols.auth import ClassicAuthContext, ERROR_IMST
from classic.protocols.frame import ClassicEAFrame
from classic.protocols.prelogin import (
    ClassicPreloginContext,
    ClassicPreloginProfile,
    ClassicPreloginService,
    ClassicUserset,
)


class ClassicAccountPolicyFrameTests(unittest.TestCase):
    def test_live_policy_frame_is_a_valid_stock_signed_auth_rejection(self) -> None:
        service = create_auth_service(
            CredentialStore(),
            IdentityStore(token_factory=lambda: "unused-token"),
            verify_passwords=False,
        )

        for action in ("ban", "kick"):
            with self.subTest(action=action):
                wire = service.account_policy_frame(action)
                frame, remainder = ClassicEAFrame.decode_one(wire)

                self.assertEqual(remainder, b"")
                self.assertEqual(frame.command, "auth")
                self.assertEqual(frame.reserved, ERROR_IMST)
                self.assertEqual(len(frame.payload), 9)
                self.assertEqual(frame.payload[:1], b"\x00")
                self.assertEqual(frame.payload[-8:], md5(wire[:-8]).digest()[:8])
        with self.assertRaises(ValueError):
            service.account_policy_frame("unban")


class MostWantedAccountEnforcementTests(unittest.TestCase):
    @staticmethod
    def _decoded_commands(wires: list[bytes]) -> list[str]:
        return [ClassicEAFrame.decode_one(wire)[0].command for wire in wires]

    def _make_service(
        self,
        root: Path,
    ) -> tuple[
        ClassicPreloginService,
        SessionDirectory,
        ClassicPreloginContext,
        ClassicPreloginContext,
        list[bytes],
        list[bytes],
    ]:
        credentials = CredentialStore(root / "auth.json")
        credentials.create_account("HostAccount", "password", persona="Host")
        credentials.create_account("GuestAccount", "password", persona="Guest")
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

        host_wires: list[bytes] = []
        guest_wires: list[bytes] = []

        def context_for(
            account_name: str,
            persona: str,
            wires: list[bytes],
        ) -> ClassicPreloginContext:
            account = credentials.resolve_account(account_name)
            identity, token = identities.login(account_name, persona)
            context = ClassicPreloginContext(
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
                send_wire=lambda wire: not wires.append(wire),
            )
            service._register(context)
            return context

        host = context_for("HostAccount", "Host", host_wires)
        guest = context_for("GuestAccount", "Guest", guest_wires)
        return service, sessions, host, guest, host_wires, guest_wires

    @staticmethod
    def _make_active_game_and_userset(
        service: ClassicPreloginService,
        sessions: SessionDirectory,
        host: ClassicPreloginContext,
        guest: ClassicPreloginContext,
    ) -> tuple[int, ClassicUserset]:
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
        if not sessions.join_game(
            game.game_id,
            guest_id,
            persona="Guest",
            address="127.0.0.2",
        ):
            raise AssertionError("failed to create test game membership")
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
        return game.game_id, userset

    def test_restricting_guest_closes_race_and_preserves_clean_owner_userset(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, sessions, host, guest, host_wires, _guest_wires = (
                self._make_service(root)
            )
            game_id, userset = self._make_active_game_and_userset(
                service,
                sessions,
                host,
                guest,
            )
            retired: list[int] = []
            service.set_race_relay(
                Endpoint("127.0.0.1", 20000),
                lambda game: dict(game.participant_race_addresses),
                unregistrar=lambda game: not retired.append(game.game_id),
            )

            result = service.enforce_account_policy(
                (guest.auth.identity.user_id,),
                reason="account-banned",
            )

            self.assertEqual(result.games_closed, 1)
            self.assertEqual(result.usersets_deleted, 0)
            self.assertEqual(result.userset_members_removed, 1)
            self.assertEqual(result.contexts_reset, 1)
            self.assertIsNone(sessions.get_game(game_id))
            self.assertEqual(retired, [game_id])
            self.assertIs(service._usersets[userset.userset_id], userset)
            self.assertEqual(userset.members, {host.auth.identity.user_id})
            self.assertEqual(userset.game_id, 0)
            self.assertEqual(host.lobby_game_id, 0)
            self.assertEqual(host.userset_id, userset.userset_id)
            self.assertEqual(guest.lobby_game_id, 0)
            self.assertEqual(guest.userset_id, 0)
            self.assertEqual(
                self._decoded_commands(host_wires),
                ["+who", "+usm", "+mgm", "+ust"],
            )

            host_wire_count = len(host_wires)
            service.enforce_account_policy(
                (guest.auth.identity.user_id,),
                reason="account-banned",
            )
            self.assertEqual(retired, [game_id])
            self.assertEqual(len(host_wires), host_wire_count)

    def test_restricting_owner_deletes_userset_and_resets_survivor(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, sessions, host, guest, _host_wires, guest_wires = (
                self._make_service(root)
            )
            game_id, userset = self._make_active_game_and_userset(
                service,
                sessions,
                host,
                guest,
            )
            retired: list[int] = []
            service.set_race_relay(
                Endpoint("127.0.0.1", 20000),
                lambda game: dict(game.participant_race_addresses),
                unregistrar=lambda game: not retired.append(game.game_id),
            )

            result = service.enforce_account_policy(
                (host.auth.identity.user_id,),
                reason="account-disabled",
            )

            self.assertEqual(result.games_closed, 1)
            self.assertEqual(result.usersets_deleted, 1)
            self.assertEqual(result.userset_members_removed, 1)
            self.assertIsNone(sessions.get_game(game_id))
            self.assertNotIn(userset.userset_id, service._usersets)
            self.assertEqual(retired, [game_id])
            self.assertEqual(host.lobby_game_id, 0)
            self.assertEqual(host.userset_id, 0)
            self.assertEqual(guest.lobby_game_id, 0)
            self.assertEqual(guest.userset_id, 0)
            self.assertEqual(
                self._decoded_commands(guest_wires),
                ["+who", "+usm", "+mgm", "+ust"],
            )


class Underground2AccountEnforcementTests(unittest.TestCase):
    @staticmethod
    def _decoded_commands(wires: list[bytes]) -> list[str]:
        return [ClassicEAFrame.decode_one(wire)[0].command for wire in wires]

    def test_restricting_guest_closes_u2_race_and_removes_room_membership(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = CredentialStore(root / "auth.json")
            credentials.create_account("HostAccount", "password", persona="Host")
            credentials.create_account("GuestAccount", "password", persona="Guest")
            tokens = iter(("host-token", "guest-token"))
            identities = IdentityStore(token_factory=lambda: next(tokens))
            auth = create_u2_auth_service(
                credentials,
                identities,
                verify_passwords=False,
            )
            sessions = SessionDirectory()
            service = ClassicPreloginService(
                auth,
                profile=ClassicPreloginProfile(game_id="underground2"),
                control_endpoint=Endpoint("127.0.0.1", 13505),
                sessions=sessions,
                ranking=ClassicRankingStore(root / "stats.json"),
            )
            host_wires: list[bytes] = []

            def context_for(
                account_name: str,
                persona: str,
                wires: list[bytes],
            ) -> ClassicPreloginContext:
                account = credentials.resolve_account(account_name)
                identity, token = identities.login(account_name, persona)
                context = ClassicPreloginContext(
                    auth=ClassicAuthContext(
                        connection_id=f"u2-{persona.casefold()}",
                        account=account,
                        identity=identity,
                        session_token=token,
                        lkey=token,
                        persona=persona,
                    ),
                    authenticated=True,
                    persona_selected=True,
                    client_address="127.0.0.1",
                    send_wire=lambda wire: not wires.append(wire),
                )
                service._register(context)
                return context

            host = context_for("HostAccount", "Host", host_wires)
            guest = context_for("GuestAccount", "Guest", [])
            host_id = host.auth.identity.user_id
            guest_id = guest.auth.identity.user_id
            room = sessions.create_room(host_id, "Lobby", capacity=4)
            self.assertTrue(sessions.join_room(room.room_id, guest_id))
            host.u2_room_id = guest.u2_room_id = room.room_id
            host.u2_room_name = guest.u2_room_name = room.name
            game = sessions.create_game(
                room.room_id,
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
            host.lobby_game_id = guest.lobby_game_id = game.game_id
            retired: list[int] = []
            service.set_race_relay(
                Endpoint("127.0.0.1", 20000),
                lambda active_game: dict(active_game.participant_race_addresses),
                unregistrar=lambda active_game: not retired.append(
                    active_game.game_id
                ),
            )

            result = service.enforce_account_policy(
                (guest_id,),
                reason="account-banned",
            )

            self.assertEqual(result.games_closed, 1)
            self.assertEqual(result.contexts_reset, 1)
            self.assertIsNone(sessions.get_game(game.game_id))
            self.assertEqual(retired, [game.game_id])
            self.assertEqual(host.lobby_game_id, 0)
            self.assertEqual(guest.lobby_game_id, 0)
            self.assertEqual(host.u2_room_id, room.room_id)
            self.assertEqual(guest.u2_room_id, 0)
            visible = sessions.visible_rooms(host_id)
            self.assertEqual(len(visible), 1)
            self.assertEqual(visible[0].members, {host_id})
            self.assertEqual(
                self._decoded_commands(host_wires),
                ["+who", "+mgm", "+sst"],
            )

            service.enforce_account_policy((guest_id,), reason="account-banned")
            self.assertEqual(retired, [game.game_id])



if __name__ == "__main__":
    unittest.main()
