from __future__ import annotations

import unittest

from carbon.accounts.identity import Identity, IdentityStore
from carbon.core.config import Endpoint
from carbon.fesl.service import CarbonEndpoints, CarbonFESLService
from carbon.rebroadcaster.service import CarbonRebroadcasterService
from carbon.theater.directory import CarbonGame, CarbonGameDirectory, CarbonParticipant
from carbon.transport.prototunnel import TunnelDatagram, TunnelPacket


EKEY = b"9181081919"


def ticket_confirmation(ticket: str) -> bytes:
    body = bytes.fromhex("01808004800000") + bytes((len(ticket),)) + ticket.encode("ascii")
    return TunnelDatagram(
        5,
        (
            TunnelPacket(1, bytes.fromhex("00000002dcf7b035")),
            TunnelPacket(1, bytes.fromhex("00000100000000ff")),
            TunnelPacket(1, bytes.fromhex("00000100000000ff") + body + bytes((0x04,))),
        ),
    ).encode(EKEY)


class CarbonAccountPolicyFrameTests(unittest.TestCase):
    def test_policy_frame_distinguishes_native_ban_and_disable_errors(self) -> None:
        service = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1"),
            IdentityStore(token_factory=lambda: "unused-token"),
        )

        banned = service.account_policy_frame("ban", transaction=0x80001234)

        self.assertEqual(banned.command, "acct")
        self.assertEqual(banned.transaction, 0x80001234)
        self.assertEqual(banned.fields["TXN"], "Login")
        self.assertEqual(banned.fields["errorCode"], "103")
        self.assertEqual(
            banned.fields["localizedMessage"],
            '"This account has been banned. Contact Customer Support."',
        )

        disabled = service.account_policy_frame("disable")
        self.assertEqual(disabled.fields["errorCode"], "102")
        self.assertEqual(
            disabled.fields["localizedMessage"],
            '"The account has been disabled. Contact Customer Support."',
        )
        kicked = service.account_policy_frame("kick")
        self.assertEqual(kicked.fields["errorCode"], "102")
        self.assertEqual(kicked.fields["localizedMessage"], disabled.fields["localizedMessage"])
        with self.assertRaises(ValueError):
            service.account_policy_frame("enable")


class CarbonAccountTransportCleanupTests(unittest.TestCase):
    @staticmethod
    def _identities() -> tuple[Identity, Identity]:
        store = IdentityStore(token_factory=lambda: "token")
        host, _ = store.login("HostAccount", "Host")
        guest, _ = store.login("GuestAccount", "Guest")
        return host, guest

    @staticmethod
    def _bind(
        service: CarbonRebroadcasterService,
        games: CarbonGameDirectory,
        game: CarbonGame,
        participant: CarbonParticipant,
        address: tuple[str, int],
    ) -> None:
        service.handle_datagram(
            ticket_confirmation(games.ticket(game, participant)),
            address,
        )
        if service.binding(address) is None:
            raise AssertionError("test participant did not bind to ProtoTunnel")

    def test_guest_policy_removes_only_guest_transport_and_membership(self) -> None:
        host, guest = self._identities()
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        game = games.create(host)
        guest_participant = games.enter(game.gid, guest)
        self.assertIsNotNone(guest_participant)
        host_participant = game.participants[host.user_id]
        service = CarbonRebroadcasterService(games)
        host_addr = ("192.0.2.10", 1042)
        guest_addr = ("192.0.2.11", 5042)
        self._bind(service, games, game, host_participant, host_addr)
        self._bind(service, games, game, guest_participant, guest_addr)

        affected = service.force_disconnect_user(
            guest.user_id,
            reason="account-banned",
        )

        self.assertEqual(affected, 1)
        self.assertIsNotNone(service.binding(host_addr))
        self.assertIsNone(service.binding(guest_addr))
        preserved = games.get(game.gid)
        self.assertIs(preserved, game)
        self.assertEqual(set(preserved.participants), {host.user_id})
        self.assertEqual(service.session_endpoints(game.gid), (host_addr,))
        self.assertEqual(service.peers(host_addr), ())
        self.assertEqual(
            service.force_disconnect_user(guest.user_id, reason="account-banned"),
            0,
        )

    def test_client_host_policy_retires_room_and_all_bound_endpoints(self) -> None:
        host, guest = self._identities()
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        game = games.create(host)
        guest_participant = games.enter(game.gid, guest)
        self.assertIsNotNone(guest_participant)
        service = CarbonRebroadcasterService(games)
        host_addr = ("192.0.2.20", 1042)
        guest_addr = ("192.0.2.21", 5042)
        self._bind(service, games, game, game.participants[host.user_id], host_addr)
        self._bind(service, games, game, guest_participant, guest_addr)

        affected = service.force_disconnect_user(
            host.user_id,
            reason="account-disabled",
        )

        self.assertEqual(affected, 1)
        self.assertIsNone(games.get(game.gid))
        self.assertIsNone(service.binding(host_addr))
        self.assertIsNone(service.binding(guest_addr))
        self.assertEqual(service.session_endpoints(game.gid), ())
        self.assertEqual(
            service.force_disconnect_user(host.user_id, reason="account-disabled"),
            0,
        )

    def test_dedicated_allocator_policy_retires_server_hosted_room(self) -> None:
        allocator, guest = self._identities()
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        game = games.create(
            allocator,
            {
                "B-U-game_type": "1",
                "B-U-matchmaking_state": "1",
                "B-U-max_online_player": "8",
            },
            server_hosted=True,
        )
        allocator_participant = games.enter(game.gid, allocator)
        guest_participant = games.enter(game.gid, guest)
        self.assertIsNotNone(allocator_participant)
        self.assertIsNotNone(guest_participant)
        service = CarbonRebroadcasterService(games)
        allocator_addr = ("192.0.2.30", 1042)
        guest_addr = ("192.0.2.31", 5042)
        self._bind(service, games, game, allocator_participant, allocator_addr)
        self._bind(service, games, game, guest_participant, guest_addr)

        affected = service.force_disconnect_user(
            allocator.user_id,
            reason="account-banned",
        )

        self.assertEqual(affected, 1)
        self.assertIsNone(games.get(game.gid))
        self.assertIsNone(service.binding(allocator_addr))
        self.assertIsNone(service.binding(guest_addr))

    def test_stale_udp_binding_is_removed_after_theater_membership_is_gone(self) -> None:
        host, guest = self._identities()
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        game = games.create(host)
        guest_participant = games.enter(game.gid, guest)
        self.assertIsNotNone(guest_participant)
        service = CarbonRebroadcasterService(games)
        host_addr = ("192.0.2.40", 1042)
        guest_addr = ("192.0.2.41", 5042)
        self._bind(service, games, game, game.participants[host.user_id], host_addr)
        self._bind(service, games, game, guest_participant, guest_addr)
        self.assertTrue(games.leave(game.gid, guest.user_id))
        self.assertIsNotNone(service.binding(guest_addr))

        affected = service.force_disconnect_user(
            guest.user_id,
            reason="account-banned",
        )

        self.assertEqual(affected, 1)
        self.assertIsNone(service.binding(guest_addr))
        self.assertIsNotNone(service.binding(host_addr))
        self.assertIs(games.get(game.gid), game)
        self.assertEqual(set(game.participants), {host.user_id})


if __name__ == "__main__":
    unittest.main()
