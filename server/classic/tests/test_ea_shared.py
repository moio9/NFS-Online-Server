"""Contract checks for the cross-game EA layer."""

import socket
import struct
import unittest

from classic.ea.directory import SessionDirectory, SessionState, Visibility
from classic.ea.multiplex import _BufferedSocket
from classic.ea.text import encode_message, parse_message
from classic.protocols.race import ClassicRaceRelay


class EATextTests(unittest.TestCase):
    def test_text_message_round_trip(self) -> None:
        raw = encode_message("user", {"name": "Test Driver", "uid": 7, "flags": 1.5})
        self.assertEqual(raw, '+USER NAME="Test Driver" UID=7 FLAGS=1.500000\n')
        self.assertEqual(parse_message(raw), ("+", "USER", {"NAME": "Test Driver", "UID": 7, "FLAGS": 1.5}))


class BufferedSocketTests(unittest.TestCase):
    def test_replays_prefix_and_delegates_shutdown(self) -> None:
        server, client = socket.socketpair()
        buffered = _BufferedSocket(server, b"AUTH")
        try:
            self.assertEqual(buffered.recv(2), b"AU")
            self.assertEqual(buffered.recv(2), b"TH")
            buffered.shutdown(socket.SHUT_RDWR)
            client.settimeout(1.0)
            self.assertEqual(client.recv(1), b"")
        finally:
            server.close()
            client.close()


class SessionDirectoryTests(unittest.TestCase):
    def test_private_room_requires_membership_or_password(self) -> None:
        directory = SessionDirectory()
        room = directory.create_room(10, "race", visibility=Visibility.PRIVATE, password="go")
        self.assertFalse(directory.join_room(room.room_id, 20))
        self.assertTrue(directory.join_room(room.room_id, 20, "go"))
        self.assertEqual([item.room_id for item in directory.visible_rooms(20)], [room.room_id])

    def test_game_lifecycle_is_shared_but_wire_free(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(257, 10, capacity=2)
        self.assertEqual(game.participants, {10})
        self.assertTrue(directory.join_game(game.game_id, 20))
        self.assertFalse(directory.join_game(game.game_id, 30))
        self.assertEqual(directory.get_game(game.game_id).participants, {10, 20})

    def test_game_participant_order_does_not_follow_global_wire_ids(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 30, capacity=4)
        self.assertTrue(directory.join_game(game.game_id, 20))
        game.participant_wire_ids = {30: 3, 20: 2}
        self.assertEqual(game.ordered_participants(), (30, 20))

        # The later guest owns the lowest global lobby ID.  It must still be
        # appended as OPPO2 instead of replacing the established OPPO1.
        self.assertTrue(directory.join_game(game.game_id, 10))
        game.participant_wire_ids[10] = 1
        self.assertEqual(game.ordered_participants(), (30, 20, 10))

        # Stock compacts OPPO slots on leave and appends a returning identity
        # after every participant which stayed in the room.  Its stable lobby
        # wire ID must not pull it back into the old visual slot.
        self.assertTrue(directory.leave_game(game.game_id, 20))
        self.assertEqual(game.ordered_participants(), (30, 10))
        self.assertTrue(directory.join_game(game.game_id, 20))
        game.participant_wire_ids[20] = 2
        self.assertEqual(game.ordered_participants(), (30, 10, 20))


class ClassicRaceRelayTests(unittest.TestCase):
    @staticmethod
    def _wrapped(target: str, payload: bytes, port: int = 3658) -> bytes:
        return struct.pack("!H", port) + socket.inet_aton(target) + payload

    @staticmethod
    def _u2_identified_wrapped(
        target: str,
        source: str,
        payload: bytes,
        port: int = 3658,
    ) -> bytes:
        return (
            struct.pack("!H", port)
            + socket.inet_aton(target)
            + b"U2I1"
            + socket.inet_aton(source)
            + payload
        )

    def test_two_same_host_players_are_bound_and_relayed_both_ways(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(game.game_id, 20))
        relay = ClassicRaceRelay()
        virtual = relay.register_game(game)
        host_endpoint = ("127.0.0.1", 40001)
        guest_endpoint = ("127.0.0.1", 40002)

        first = relay.handle(
            self._wrapped(virtual[20], b"from-host"),
            host_endpoint,
        )
        self.assertEqual(first, ())
        second = relay.handle(
            self._wrapped(virtual[10], b"from-guest"),
            guest_endpoint,
        )
        self.assertEqual({target for _, target in second}, {host_endpoint, guest_endpoint})
        delivered = {target: wire for wire, target in second}
        self.assertEqual(delivered[host_endpoint][6:], b"from-guest")
        self.assertEqual(delivered[guest_endpoint][6:], b"from-host")

    def test_unknown_target_is_not_used_as_a_local_udp_proxy(self) -> None:
        relay = ClassicRaceRelay()
        self.assertEqual(
            relay.handle(
                self._wrapped("127.0.0.1", b"not-registered", 4600),
                ("127.0.0.1", 4602),
            ),
            (),
        )

    def test_public_bootstrap_is_forwarded_without_payload_changes(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(game.game_id, 20))
        relay = ClassicRaceRelay()
        relay.set_public_host("127.0.0.1")
        advertised = relay.register_game(game)
        host_endpoint = ("127.0.0.1", 3658)
        guest_endpoint = ("127.0.0.1", 44515)
        host_probe = struct.pack("<II", 5, 0)
        guest_probe = struct.pack("<II", 1, 0)

        self.assertEqual(
            relay.handle(
                self._wrapped(advertised[20], host_probe),
                host_endpoint,
            ),
            (),
        )
        replies = relay.handle(
            self._wrapped(advertised[10], guest_probe),
            guest_endpoint,
        )
        delivered = {target: wire for wire, target in replies}
        self.assertEqual(delivered[host_endpoint][6:], guest_probe)
        self.assertEqual(delivered[guest_endpoint][6:], host_probe)
        self.assertEqual(
            struct.unpack("!H", delivered[host_endpoint][:2])[0],
            3658,
        )
        self.assertEqual(
            struct.unpack("!H", delivered[guest_endpoint][:2])[0],
            3658,
        )
        self.assertEqual(
            socket.inet_ntoa(delivered[host_endpoint][2:6]),
            "127.0.0.1",
        )
        self.assertEqual(
            socket.inet_ntoa(delivered[guest_endpoint][2:6]),
            "127.0.0.1",
        )

    def test_public_u2_two_command_one_bootstrap_is_forwarded(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(game.game_id, 20))
        relay = ClassicRaceRelay()
        relay.set_public_host("127.0.0.1")
        advertised = relay.register_game(game)
        first_endpoint = ("127.0.0.1", 3658)
        second_endpoint = ("127.0.0.1", 44515)
        command_one = struct.pack("<II", 1, 0)

        self.assertEqual(
            relay.handle(
                self._wrapped(advertised[20], command_one),
                first_endpoint,
            ),
            (),
        )
        replies = relay.handle(
            self._wrapped(advertised[10], command_one),
            second_endpoint,
        )
        self.assertEqual({target for _, target in replies}, {first_endpoint, second_endpoint})
        self.assertTrue(all(wire[6:] == command_one for wire, _target in replies))

    def test_public_u2_routes_normal_payloads_after_bootstrap(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(game.game_id, 20))
        relay = ClassicRaceRelay()
        relay.set_public_host("203.0.113.30")
        advertised = relay.register_game(game)
        host_endpoint = ("198.51.100.180", 3658)
        guest_endpoint = ("198.51.100.180", 50739)
        host_probe = struct.pack("<II", 5, 0)
        guest_probe = struct.pack("<II", 1, 0)

        self.assertEqual(
            relay.handle(
                self._wrapped(advertised[20], host_probe),
                host_endpoint,
            ),
            (),
        )
        relay.handle(
            self._wrapped(advertised[10], guest_probe),
            guest_endpoint,
        )

        host_payload = struct.pack("<I", 2) + b"host-transport"
        host_replies = relay.handle(
            self._wrapped(advertised[20], host_payload),
            host_endpoint,
        )
        self.assertEqual(len(host_replies), 1)
        host_wire, host_target = host_replies[0]
        self.assertEqual(host_target, guest_endpoint)
        self.assertEqual(host_wire[6:], host_payload)
        self.assertEqual(struct.unpack("!H", host_wire[:2])[0], 3658)
        self.assertEqual(socket.inet_ntoa(host_wire[2:6]), "203.0.113.30")

        guest_payload = struct.pack("<I", 0x65) + b"guest-transport"
        guest_replies = relay.handle(
            self._wrapped(advertised[10], guest_payload),
            guest_endpoint,
        )
        self.assertEqual(len(guest_replies), 1)
        guest_wire, guest_target = guest_replies[0]
        self.assertEqual(guest_target, host_endpoint)
        self.assertEqual(guest_wire[6:], guest_payload)
        self.assertEqual(struct.unpack("!H", guest_wire[:2])[0], 3658)
        self.assertEqual(socket.inet_ntoa(guest_wire[2:6]), "203.0.113.30")

    def test_public_nonbootstrap_payload_requires_a_bound_endpoint(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(game.game_id, 20))
        relay = ClassicRaceRelay()
        relay.set_public_host("203.0.113.30")
        advertised = relay.register_game(game)

        self.assertEqual(
            relay.handle(
                self._wrapped(
                    advertised[20],
                    struct.pack("<I", 0x65) + b"unbound",
                ),
                ("198.51.100.180", 49999),
            ),
            (),
        )

    def test_virtual_registration_ignores_public_advertisement(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(game.game_id, 20))
        relay = ClassicRaceRelay(virtual_network="198.18.0.0/29")
        relay.set_public_host("127.0.0.1")

        advertised = relay.register_virtual_game(game)

        self.assertEqual(set(advertised.values()), {"198.18.0.1", "198.18.0.2"})
        self.assertNotIn("127.0.0.1", advertised.values())

    def test_u2_four_player_channels_preserve_each_peer_socket(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=4)
        for user_id in (20, 30, 40):
            self.assertTrue(directory.join_game(game.game_id, user_id))
        relay = ClassicRaceRelay(virtual_network="198.18.0.0/28")
        virtual = relay.register_u2_virtual_game(game)

        owner_to_20 = ("10.0.0.10", 41020)
        peer20_to_owner = ("10.0.0.20", 42010)
        self.assertEqual(
            relay.handle_channel(
                self._wrapped(virtual[20], b"owner-to-20"),
                owner_to_20,
                0,
            ),
            (),
        )
        first_pair = relay.handle_channel(
            self._wrapped(virtual[10], b"20-to-owner"),
            peer20_to_owner,
            1,
        )
        self.assertEqual(
            {(target, channel) for _wire, target, channel in first_pair},
            {(owner_to_20, 0), (peer20_to_owner, 1)},
        )

        owner_to_30 = ("10.0.0.10", 41030)
        peer30_to_owner = ("10.0.0.30", 43010)
        self.assertEqual(
            relay.handle_channel(
                self._wrapped(virtual[30], b"owner-to-30"),
                owner_to_30,
                0,
            ),
            (),
        )
        second_pair = relay.handle_channel(
            self._wrapped(virtual[10], b"30-to-owner"),
            peer30_to_owner,
            2,
        )
        self.assertEqual(
            {(target, channel) for _wire, target, channel in second_pair},
            {(owner_to_30, 0), (peer30_to_owner, 2)},
        )

        # The owner now has two physical UDP sockets.  A later packet from
        # player 20 must still return to the socket paired with player 20,
        # rather than the owner's most recently observed socket for player 30.
        routed_back = relay.handle_channel(
            self._wrapped(virtual[10], b"20-to-owner-again"),
            peer20_to_owner,
            1,
        )
        self.assertEqual(
            [(target, channel) for _wire, target, channel in routed_back],
            [(owner_to_20, 0)],
        )
        self.assertEqual(routed_back[0][0][6:], b"20-to-owner-again")

        peer20_to_30 = ("10.0.0.20", 42030)
        peer30_to_20 = ("10.0.0.30", 43020)
        self.assertEqual(
            relay.handle_channel(
                self._wrapped(virtual[30], b"20-to-30"),
                peer20_to_30,
                1,
            ),
            (),
        )
        cross_pair = relay.handle_channel(
            self._wrapped(virtual[20], b"30-to-20"),
            peer30_to_20,
            2,
        )
        self.assertEqual(
            {(target, channel) for _wire, target, channel in cross_pair},
            {(peer20_to_30, 1), (peer30_to_20, 2)},
        )

        owner_to_40 = ("10.0.0.10", 41040)
        peer40_to_owner = ("10.0.0.40", 44010)
        self.assertEqual(
            relay.handle_channel(
                self._wrapped(virtual[40], b"owner-to-40"),
                owner_to_40,
                0,
            ),
            (),
        )
        fourth_channel = relay.handle_channel(
            self._wrapped(virtual[10], b"40-to-owner"),
            peer40_to_owner,
            3,
        )
        self.assertEqual(
            {(target, channel) for _wire, target, channel in fourth_channel},
            {(owner_to_40, 0), (peer40_to_owner, 3)},
        )

    def test_u2_six_players_share_base_port_with_explicit_source_identity(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=6)
        for user_id in (20, 30, 40, 50, 60):
            self.assertTrue(directory.join_game(game.game_id, user_id))
        relay = ClassicRaceRelay(virtual_network="198.18.0.0/28")
        virtual = relay.register_u2_virtual_game(game)

        owner_to_20 = ("10.0.0.10", 41020)
        peer20_to_owner = ("10.0.0.20", 42010)
        self.assertEqual(
            relay.handle_channel(
                self._u2_identified_wrapped(
                    virtual[20],
                    virtual[10],
                    struct.pack("<II", 5, 0x33C54139),
                ),
                owner_to_20,
                0,
            ),
            (),
        )
        # Live U2 sends the guest's initial command 1 toward its own ADDR0.
        # The shared relay uses the explicit source identity to redirect that
        # bootstrap to the owner, matching the old channel-owned route.
        first_pair = relay.handle_channel(
            self._u2_identified_wrapped(
                virtual[20],
                virtual[20],
                struct.pack("<II", 1, 0x802CCB20),
            ),
            peer20_to_owner,
            0,
        )
        self.assertEqual(
            {(target, channel) for _wire, target, channel in first_pair},
            {(owner_to_20, 0), (peer20_to_owner, 0)},
        )

        owner_to_30 = ("10.0.0.10", 41030)
        peer30_to_owner = ("10.0.0.30", 43010)
        self.assertEqual(
            relay.handle_channel(
                self._u2_identified_wrapped(
                    virtual[30], virtual[10], b"owner-to-30"
                ),
                owner_to_30,
                0,
            ),
            (),
        )
        second_pair = relay.handle_channel(
            self._u2_identified_wrapped(
                virtual[10], virtual[30], b"30-to-owner"
            ),
            peer30_to_owner,
            0,
        )
        self.assertEqual(
            {(target, channel) for _wire, target, channel in second_pair},
            {(owner_to_30, 0), (peer30_to_owner, 0)},
        )

        routed_back = relay.handle_channel(
            self._u2_identified_wrapped(
                virtual[10], virtual[20], b"20-to-owner-again"
            ),
            peer20_to_owner,
            0,
        )
        self.assertEqual(
            [(target, channel) for _wire, target, channel in routed_back],
            [(owner_to_20, 0)],
        )
        self.assertEqual(routed_back[0][0][6:], b"20-to-owner-again")

        peer20_to_30 = ("10.0.0.20", 42030)
        peer30_to_20 = ("10.0.0.30", 43020)
        self.assertEqual(
            relay.handle_channel(
                self._u2_identified_wrapped(
                    virtual[30], virtual[20], b"20-to-30"
                ),
                peer20_to_30,
                0,
            ),
            (),
        )
        cross_pair = relay.handle_channel(
            self._u2_identified_wrapped(
                virtual[20], virtual[30], b"30-to-20"
            ),
            peer30_to_20,
            0,
        )
        self.assertEqual(
            {(target, channel) for _wire, target, channel in cross_pair},
            {(peer20_to_30, 0), (peer30_to_20, 0)},
        )

        # Exercise every remaining slot at the configured six-player maximum.
        # The owner uses a distinct native socket for each peer, but every
        # public relay response must still leave through channel zero.
        for user_id in (40, 50, 60):
            owner_endpoint = ("10.0.0.10", 41000 + user_id)
            guest_endpoint = (f"10.0.0.{user_id}", 40000 + user_id * 100 + 10)
            self.assertEqual(
                relay.handle_channel(
                    self._u2_identified_wrapped(
                        virtual[user_id],
                        virtual[10],
                        f"owner-to-{user_id}".encode(),
                    ),
                    owner_endpoint,
                    0,
                ),
                (),
            )
            paired = relay.handle_channel(
                self._u2_identified_wrapped(
                    virtual[10],
                    virtual[user_id],
                    f"{user_id}-to-owner".encode(),
                ),
                guest_endpoint,
                0,
            )
            self.assertEqual(
                {(target, channel) for _wire, target, channel in paired},
                {(owner_endpoint, 0), (guest_endpoint, 0)},
            )

        self.assertEqual(
            relay.handle_channel(
                self._u2_identified_wrapped(
                    virtual[20], "198.18.0.14", b"spoofed-source"
                ),
                ("10.0.0.99", 49999),
                0,
            ),
            (),
        )

    def test_u2_channelized_bootstrap_is_not_mw_token_translated(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=3)
        self.assertTrue(directory.join_game(game.game_id, 20))
        self.assertTrue(directory.join_game(game.game_id, 30))
        relay = ClassicRaceRelay()
        virtual = relay.register_u2_virtual_game(game)
        owner_endpoint = ("127.0.0.1", 41020)
        guest_endpoint = ("127.0.0.1", 42010)
        owner_probe = struct.pack("<II", 5, 0x33C54139)
        guest_probe = struct.pack("<II", 1, 0x802CCB20)

        self.assertEqual(
            relay.handle_channel(
                self._wrapped(virtual[20], owner_probe),
                owner_endpoint,
                0,
            ),
            (),
        )
        replies = relay.handle_channel(
            self._wrapped(virtual[10], guest_probe),
            guest_endpoint,
            1,
        )
        delivered = {
            (target, channel): wire[6:]
            for wire, target, channel in replies
        }
        self.assertEqual(delivered[(owner_endpoint, 0)], guest_probe)
        self.assertEqual(delivered[(guest_endpoint, 1)], owner_probe)

    def test_mw_wire_aliases_route_two_clients_on_one_endpoint(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(game.game_id, 20))
        game.participant_wire_ids = {10: 1, 20: 2}
        relay = ClassicRaceRelay()
        relay.register_virtual_game(game)
        shared_endpoint = ("127.0.0.1", 4602)
        owner_probe = struct.pack("<II", 5, 0)
        guest_probe = struct.pack("<II", 1, 0)

        self.assertEqual(
            relay.handle(
                self._wrapped("2.0.0.0", owner_probe),
                shared_endpoint,
            ),
            (),
        )
        replies = relay.handle(
            self._wrapped("1.0.0.0", guest_probe),
            shared_endpoint,
        )
        self.assertEqual(len(replies), 2)
        self.assertEqual({target for _, target in replies}, {shared_endpoint})
        self.assertEqual(
            {socket.inet_ntoa(wire[2:6]) for wire, _ in replies},
            {"1.0.0.0", "2.0.0.0"},
        )

    def test_unwrapped_mw_transport_queues_until_peer_is_known(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(game.game_id, 20))
        game.participant_wire_ids = {10: 1, 20: 2}
        relay = ClassicRaceRelay()
        relay.register_virtual_game(game)
        raw_probe = struct.pack("<I", 0x00010108) + bytes(60)

        self.assertEqual(
            relay.handle_channel(raw_probe, ("127.0.0.1", 4602), 0),
            (),
        )

    def test_native_mw_loopback_channels_bootstrap_shared_game_port(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(game.game_id, 20))
        game.participant_wire_ids = {10: 1, 20: 2}
        relay = ClassicRaceRelay()
        relay.register_virtual_game(game)
        host_endpoint = ("127.0.0.2", 3658)
        guest_endpoint = ("127.0.0.3", 3658)
        guest_payload = struct.pack("<II", 1, 0xF1BC)
        host_payload = b"IP=1\nPORT=3658\n"

        guest_replies = relay.handle_channel(
            guest_payload,
            guest_endpoint,
            1,
        )
        self.assertEqual(
            guest_replies,
            ((guest_payload, host_endpoint, 0),),
        )

        replies = relay.handle_channel(host_payload, host_endpoint, 0)

        delivered = {
            (target, reply_channel): wire
            for wire, target, reply_channel in replies
        }
        self.assertEqual(
            set(delivered),
            {(guest_endpoint, 1)},
        )
        self.assertEqual(delivered[(guest_endpoint, 1)], host_payload)

    def test_native_mw_six_player_channels_use_host_spokes(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=6)
        for user_id in (20, 30, 40, 50, 60):
            self.assertTrue(directory.join_game(game.game_id, user_id))
        game.participant_wire_ids = {
            user_id: index + 1
            for index, user_id in enumerate((10, 20, 30, 40, 50, 60))
        }
        relay = ClassicRaceRelay()
        relay.register_virtual_game(game)
        endpoints = {
            user_id: (f"127.0.0.{index + 2}", 3658)
            for index, user_id in enumerate((10, 20, 30, 40, 50, 60))
        }

        self.assertEqual(
            relay.handle_channel(b"from-10", endpoints[10], 0),
            (),
        )
        delivered_to_host = set()
        for channel, user_id in enumerate((20, 30, 40, 50, 60), start=1):
            replies = relay.handle_channel(
                f"from-{user_id}".encode("ascii"),
                endpoints[user_id],
                channel,
            )
            delivered_to_host.update(
                wire
                for wire, target, reply_channel in replies
                if (target, reply_channel) == (endpoints[10], 0)
            )
            self.assertEqual(
                {
                    (target, reply_channel)
                    for wire, target, reply_channel in replies
                    if wire == f"from-{user_id}".encode("ascii")
                },
                {(endpoints[10], 0)},
            )
            self.assertIn(
                (b"from-10", endpoints[user_id], channel),
                replies,
            )
        self.assertEqual(
            delivered_to_host,
            {f"from-{user_id}".encode("ascii") for user_id in (20, 30, 40, 50, 60)},
        )

    def test_native_mw_shared_guest_channel_survives_participant_reorder(
        self,
    ) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=3)
        self.assertTrue(directory.join_game(game.game_id, 30))
        game.participant_wire_ids = {10: 1, 30: 2}
        relay = ClassicRaceRelay()
        relay.register_virtual_game(game)
        host_endpoint = ("127.0.0.2", 3658)
        old_guest_endpoint = ("127.0.0.3", 3658)
        new_guest_endpoint = ("192.168.1.14", 38975)

        relay.handle_channel(b"host-before-third", host_endpoint, 0)
        relay.handle_channel(b"old-guest-before-third", old_guest_endpoint, 1)

        # A lower user id sorts before the existing guest. Both guests still
        # use MW listener 1; the endpoint binding, not participants[1], must
        # continue to identify the old guest after this reorder.
        self.assertTrue(directory.join_game(game.game_id, 20))
        game.participant_wire_ids[20] = 3
        relay.register_virtual_game(game)
        relay.handle_channel(b"new-guest", new_guest_endpoint, 2)
        old_guest_replies = relay.handle_channel(
            b"old-guest-after-third",
            old_guest_endpoint,
            1,
        )
        self.assertEqual(
            {
                (target, reply_channel)
                for wire, target, reply_channel in old_guest_replies
                if wire == b"old-guest-after-third"
            },
            {(host_endpoint, 0)},
        )

        host_replies = relay.handle_channel(
            b"host-after-third",
            host_endpoint,
            0,
        )
        self.assertEqual(
            {
                (target, reply_channel)
                for wire, target, reply_channel in host_replies
                if wire == b"host-after-third"
            },
            {(old_guest_endpoint, 1), (new_guest_endpoint, 2)},
        )

    def test_wrapped_mw_three_player_shared_public_port(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=3)
        self.assertTrue(directory.join_game(game.game_id, 20))
        game.participant_wire_ids = {10: 1, 20: 2}
        relay = ClassicRaceRelay()
        virtual = relay.register_shared_virtual_game(game)
        host_first = ("198.51.100.10", 41000)
        host_second = ("198.51.100.10", 41001)
        guest_first = ("198.51.100.20", 42000)
        guest_second = ("203.0.113.30", 43000)
        first_owner_token = 0x4A20B7FC
        second_owner_token = 0x4A21A817
        first_token = 0x4A2097FD
        second_token = 0x5B3188EC

        relay.handle_channel(self._wrapped(virtual[10], struct.pack("<II", 1, first_token)), guest_first, 0)
        relay.handle_channel(self._wrapped(virtual[20], struct.pack("<II", 5, first_owner_token)), host_first, 0)
        self.assertTrue(directory.join_game(game.game_id, 30))
        game.participant_wire_ids[30] = 3
        virtual = relay.register_shared_virtual_game(game)

        self.assertEqual(relay.handle_channel(self._wrapped(virtual[10], struct.pack("<II", 1, second_token)), guest_second, 0), ())
        replies = relay.handle_channel(self._wrapped(virtual[30], struct.pack("<II", 5, second_owner_token)), host_second, 0)
        delivered={(target,channel,struct.unpack("<II",wire[6:])) for wire,target,channel in replies if len(wire)==14}
        self.assertEqual(delivered,{(guest_second,0,(5,second_token)),(host_second,0,(1,second_owner_token))})
        self.assertFalse(any(target == host_first for _wire,target,_channel in replies))

        first_again=relay.handle_channel(self._wrapped(virtual[10],struct.pack("<II",1,first_token)),guest_first,0)
        self.assertEqual({(target,channel,struct.unpack("<II",wire[6:])) for wire,target,channel in first_again if len(wire)==14},{(host_first,0,(1,first_owner_token))})

    def test_mw_leave_rejoin_then_simultaneous_guest_bootstraps_fresh_sockets(
        self,
    ) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=3)
        self.assertTrue(directory.join_game(game.game_id, 20))
        game.participant_wire_ids = {10: 1, 20: 2}
        relay = ClassicRaceRelay()
        virtual = relay.register_shared_virtual_game(game)
        old_guest_endpoint = ("192.168.1.150", 57334)
        old_host_endpoint = ("192.168.1.150", 53360)
        shared_guest_token = 0xAC571018

        relay.handle_channel(
            self._wrapped(
                virtual[10],
                struct.pack("<II", 1, shared_guest_token),
            ),
            old_guest_endpoint,
            0,
        )
        relay.handle_channel(
            self._wrapped(
                virtual[20],
                struct.pack("<II", 5, 0xDCB54997),
            ),
            old_host_endpoint,
            0,
        )

        # GLEA keeps the owner's relay but must retire guest 20's old socket.
        self.assertTrue(directory.leave_game(game.game_id, 20))
        game.participant_wire_ids = {10: 1}
        relay.register_shared_virtual_game(game)

        # Two GJOIs complete almost together. The newest guest emits command 1
        # first, and both local clients intentionally use the same token/IP.
        self.assertTrue(directory.join_game(game.game_id, 20))
        game.participant_wire_ids[20] = 2
        relay.register_shared_virtual_game(game)
        self.assertTrue(directory.join_game(game.game_id, 30))
        game.participant_wire_ids[30] = 3
        virtual = relay.register_shared_virtual_game(game)

        returning_endpoint = ("192.168.1.150", 42467)
        newest_endpoint = ("192.168.1.150", 40464)
        self.assertEqual(
            relay.handle_channel(
                self._wrapped(
                    virtual[10],
                    struct.pack("<II", 1, shared_guest_token),
                ),
                newest_endpoint,
                0,
            ),
            (),
        )
        self.assertEqual(
            relay.handle_channel(
                self._wrapped(
                    virtual[10],
                    struct.pack("<II", 1, shared_guest_token),
                ),
                returning_endpoint,
                0,
            ),
            (),
        )

        host_to_newest = ("192.168.1.150", 45898)
        newest_replies = relay.handle_channel(
            self._wrapped(
                virtual[30],
                struct.pack("<II", 5, 0xDCB56996),
            ),
            host_to_newest,
            0,
        )
        host_to_returning = ("192.168.1.150", 53361)
        returning_replies = relay.handle_channel(
            self._wrapped(
                virtual[20],
                struct.pack("<II", 5, 0xDCB57996),
            ),
            host_to_returning,
            0,
        )

        self.assertIn(
            (newest_endpoint, 0),
            {(target, channel) for _wire, target, channel in newest_replies},
        )
        self.assertIn(
            (returning_endpoint, 0),
            {(target, channel) for _wire, target, channel in returning_replies},
        )
        self.assertFalse(
            any(
                target == old_guest_endpoint
                for _wire, target, _channel in newest_replies + returning_replies
            )
        )

    def test_wrapped_mw_three_player_uses_targeted_host_spokes(
        self,
    ) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=3)
        self.assertTrue(directory.join_game(game.game_id, 20))
        self.assertTrue(directory.join_game(game.game_id, 30))
        game.participant_wire_ids = {10: 1, 20: 2, 30: 3}
        relay = ClassicRaceRelay()
        virtual = relay.register_virtual_game(game)
        host_endpoint = ("127.0.0.2", 3658)
        first_guest_endpoint = ("192.168.1.14", 49981)
        second_guest_endpoint = ("127.0.0.3", 3658)

        # Both guests identify themselves by addressing the owner. Their
        # streams must never be reflected to the other guest.
        self.assertEqual(
            relay.handle_channel(
                self._wrapped(virtual[10], b"from-first-guest"),
                first_guest_endpoint,
                1,
            ),
            (),
        )
        second_guest_replies = relay.handle_channel(
            self._wrapped(virtual[10], b"from-second-guest"),
            second_guest_endpoint,
            2,
        )
        self.assertEqual(second_guest_replies, ())

        # Binding the owner flushes both queued guest probes to it, while the
        # owner's current packet follows only its encoded first-guest target.
        host_replies = relay.handle_channel(
            self._wrapped(virtual[20], b"from-host"),
            host_endpoint,
            0,
        )
        self.assertEqual(
            {
                (wire[6:], target, channel)
                for wire, target, channel in host_replies
            },
            {
                (b"from-first-guest", host_endpoint, 0),
                (b"from-second-guest", host_endpoint, 0),
                (b"from-host", first_guest_endpoint, 1),
            },
        )

    def test_wrapped_mw_three_player_bootstrap_translates_each_spoke_token(
        self,
    ) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=3)
        self.assertTrue(directory.join_game(game.game_id, 20))
        self.assertTrue(directory.join_game(game.game_id, 30))
        game.participant_wire_ids = {10: 1, 20: 2, 30: 3}
        relay = ClassicRaceRelay()
        virtual = relay.register_virtual_game(game)
        host_endpoint = ("127.0.0.2", 3658)
        first_guest_endpoint = ("127.0.0.3", 3658)
        second_guest_endpoint = ("127.0.0.4", 3658)
        owner_token = 0x33C54139
        first_guest_token = 0x802CCB20
        second_guest_token = 0x91DDEC31

        self.assertEqual(
            relay.handle_channel(
                self._wrapped(
                    virtual[10],
                    struct.pack("<II", 1, first_guest_token),
                ),
                first_guest_endpoint,
                1,
            ),
            (),
        )
        relay.handle_channel(
            self._wrapped(
                virtual[10],
                struct.pack("<II", 1, second_guest_token),
            ),
            second_guest_endpoint,
            2,
        )
        first_replies = relay.handle_channel(
            self._wrapped(
                virtual[20],
                    struct.pack("<II", 5, owner_token),
            ),
            host_endpoint,
            0,
        )
        second_replies = relay.handle_channel(
            self._wrapped(
                virtual[30],
                struct.pack("<II", 5, owner_token),
            ),
            host_endpoint,
            0,
        )

        delivered = {
            (target, reply_channel, struct.unpack("<II", wire[6:])[0]):
                struct.unpack("<II", wire[6:])[1]
            for wire, target, reply_channel in first_replies + second_replies
            if len(wire) == 14
        }
        self.assertEqual(
            delivered[(first_guest_endpoint, 1, 5)],
            first_guest_token,
        )
        self.assertEqual(
            delivered[(second_guest_endpoint, 2, 5)],
            second_guest_token,
        )
        host_guest_tokens = {
            struct.unpack("<II", wire[6:])[1]
            for wire, target, reply_channel in first_replies + second_replies
            if (
                (target, reply_channel) == (host_endpoint, 0)
                and len(wire) == 14
                and struct.unpack("<II", wire[6:])[0] == 1
            )
        }
        self.assertEqual(
            host_guest_tokens,
            {owner_token},
        )

    def test_mw_late_guest_bootstrap_is_translated_without_history_replay(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=3)
        self.assertTrue(directory.join_game(game.game_id, 20))
        game.participant_wire_ids = {10: 1, 20: 2}
        relay = ClassicRaceRelay()
        virtual = relay.register_virtual_game(game)
        host_endpoint = ("127.0.0.2", 3658)
        first_guest_endpoint = ("127.0.0.3", 3658)
        late_guest_endpoint = ("192.168.1.14", 49981)
        owner_token = 0x33C54139
        first_guest_token = 0x802CCB20
        late_guest_token = 0x91DDEC31

        relay.handle_channel(
            self._wrapped(
                virtual[10],
                struct.pack("<II", 1, first_guest_token),
            ),
            first_guest_endpoint,
            1,
        )
        relay.handle_channel(
            self._wrapped(
                virtual[20],
                struct.pack("<II", 5, owner_token),
            ),
            host_endpoint,
            0,
        )
        owner_two = struct.pack("<II", 2, owner_token)
        owner_transport = bytes.fromhex(
            "00010000ff0000000134124c0134124c0000000040"
        )
        relay.handle_channel(
            self._wrapped(virtual[20], owner_two),
            host_endpoint,
            0,
        )
        relay.handle_channel(
            self._wrapped(virtual[20], owner_transport),
            host_endpoint,
            0,
        )

        self.assertTrue(directory.join_game(game.game_id, 30))
        game.participant_wire_ids[30] = 3
        virtual = relay.register_virtual_game(game)
        replies = relay.handle_channel(
            self._wrapped(
                virtual[10],
                struct.pack("<II", 1, late_guest_token),
            ),
            late_guest_endpoint,
            2,
        )

        self.assertEqual(
            [
                wire[6:]
                for wire, target, channel in replies
                if (target, channel) == (late_guest_endpoint, 2)
            ],
            [],
        )
        self.assertEqual(
            [
                wire[6:]
                for wire, target, channel in replies
                if (target, channel) == (first_guest_endpoint, 1)
            ],
            [],
        )
        host_payloads = [
            wire[6:]
            for wire, target, channel in replies
            if (target, channel) == (host_endpoint, 0)
        ]
        self.assertEqual(
            host_payloads,
            [struct.pack("<II", 1, owner_token)],
        )

    def test_mw_wrapped_channels_exchange_packets_on_viewer_ports(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(game.game_id, 20))
        game.participant_wire_ids = {10: 1, 20: 2}
        relay = ClassicRaceRelay()
        virtual = relay.register_virtual_game(game)
        host_endpoint = ("127.0.0.1", 45469)
        guest_endpoint = ("127.0.0.1", 3658)

        self.assertEqual(
            relay.handle_channel(
                self._wrapped(virtual[20], b"from-host"),
                host_endpoint,
                0,
            ),
            (),
        )
        replies = relay.handle_channel(
            self._wrapped(virtual[10], b"from-guest"),
            guest_endpoint,
            1,
        )

        delivered = {
            (target, reply_channel): wire
            for wire, target, reply_channel in replies
        }
        self.assertEqual(
            set(delivered),
            {(host_endpoint, 0), (guest_endpoint, 1)},
        )
        self.assertEqual(delivered[(host_endpoint, 0)][6:], b"from-guest")
        self.assertEqual(delivered[(guest_endpoint, 1)][6:], b"from-host")
        self.assertEqual(
            socket.inet_ntoa(delivered[(host_endpoint, 0)][2:6]),
            virtual[20],
        )
        self.assertEqual(
            socket.inet_ntoa(delivered[(guest_endpoint, 1)][2:6]),
            virtual[10],
        )
        self.assertEqual(
            struct.unpack("!H", delivered[(host_endpoint, 0)][:2])[0],
            3658,
        )
        self.assertEqual(
            struct.unpack("!H", delivered[(guest_endpoint, 1)][:2])[0],
            3658,
        )

    def test_mw_wrapped_channel_corrects_observed_self_targets(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(game.game_id, 20))
        relay = ClassicRaceRelay()
        virtual = relay.register_virtual_game(game)
        host_endpoint = ("127.0.0.1", 45469)
        guest_endpoint = ("127.0.0.1", 3658)

        self.assertEqual(
            relay.handle_channel(
                self._wrapped(
                    virtual[10],
                    b"MWRG-seed",
                    host_endpoint[1],
                ),
                host_endpoint,
                0,
            ),
            (),
        )
        replies = relay.handle_channel(
            self._wrapped(virtual[20], struct.pack("<II", 5, 0)),
            guest_endpoint,
            1,
        )

        delivered = {
            (target, reply_channel): wire
            for wire, target, reply_channel in replies
        }
        self.assertEqual(
            set(delivered),
            {(host_endpoint, 0), (guest_endpoint, 1)},
        )
        self.assertEqual(
            delivered[(host_endpoint, 0)][6:],
            struct.pack("<II", 5, 0),
        )
        self.assertEqual(delivered[(guest_endpoint, 1)][6:], b"MWRG-seed")

    def test_mw_stale_channel_does_not_steal_bound_peer_endpoint(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(game.game_id, 20))
        relay = ClassicRaceRelay()
        virtual = relay.register_virtual_game(game)
        host_endpoint = ("127.0.0.1", 38569)
        guest_endpoint = ("127.0.0.1", 3658)

        self.assertEqual(
            relay.handle_channel(
                self._wrapped(virtual[20], b"from-host"),
                host_endpoint,
                0,
            ),
            (),
        )
        relay.handle_channel(
            self._wrapped(virtual[10], b"from-guest"),
            guest_endpoint,
            1,
        )

        crossed = relay.handle_channel(
            self._wrapped(virtual[10], b"guest-on-stale-channel"),
            guest_endpoint,
            0,
        )
        self.assertEqual(
            [(target, channel) for _wire, target, channel in crossed],
            [(host_endpoint, 0)],
        )
        self.assertEqual(crossed[0][0][6:], b"guest-on-stale-channel")

        host_after = relay.handle_channel(
            self._wrapped(virtual[20], b"host-after-crossed-packet"),
            host_endpoint,
            0,
        )
        self.assertEqual(
            [(target, channel) for _wire, target, channel in host_after],
            [(guest_endpoint, 1)],
        )
        self.assertEqual(host_after[0][0][6:], b"host-after-crossed-packet")

    def test_mw_wrapped_channels_demangle_each_recipient_token(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(game.game_id, 20))
        relay = ClassicRaceRelay()
        virtual = relay.register_virtual_game(game)
        host_endpoint = ("127.0.0.1", 45469)
        guest_endpoint = ("127.0.0.1", 3658)
        guest_probe = struct.pack("<II", 1, 0x802CCB20)

        self.assertEqual(
            relay.handle_channel(
                self._wrapped(virtual[20], guest_probe),
                guest_endpoint,
                1,
            ),
            (),
        )
        replies = relay.handle_channel(
            self._wrapped(virtual[20], b"MWRG-seed"),
            host_endpoint,
            0,
        )

        host_payloads = [
            wire[6:]
            for wire, target, reply_channel in replies
            if (target, reply_channel) == (host_endpoint, 0)
        ]
        guest_payloads = [
            wire[6:]
            for wire, target, reply_channel in replies
            if (target, reply_channel) == (guest_endpoint, 1)
        ]
        self.assertEqual(
            host_payloads,
            [guest_probe],
        )
        self.assertEqual(
            guest_payloads,
            [b"MWRG-seed"],
        )

        native_host_five = relay.handle_channel(
            self._wrapped(virtual[20], struct.pack("<II", 5, 0x33C54139)),
            host_endpoint,
            0,
        )
        host_controls = [
            wire[6:]
            for wire, target, reply_channel in native_host_five
            if (target, reply_channel) == (host_endpoint, 0)
        ]
        guest_controls = [
            wire[6:]
            for wire, target, reply_channel in native_host_five
            if (target, reply_channel) == (guest_endpoint, 1)
        ]
        self.assertEqual(
            host_controls,
            [],
        )
        self.assertEqual(
            guest_controls,
            [
                struct.pack("<II", 5, 0x802CCB20),
            ],
        )
        translated_guest_probe = relay.handle_channel(
            self._wrapped(virtual[10], guest_probe),
            guest_endpoint,
            1,
        )
        self.assertEqual(
            [wire[6:] for wire, target, channel in translated_guest_probe
             if (target, channel) == (host_endpoint, 0)],
            [struct.pack("<II", 1, 0x33C54139)],
        )
        native_host_two = relay.handle_channel(
            self._wrapped(virtual[20], struct.pack("<II", 2, 0x33C54139)),
            host_endpoint,
            0,
        )
        self.assertEqual(
            [wire[6:] for wire, _target, _channel in native_host_two],
            [struct.pack("<II", 2, 0x802CCB20)],
        )
        self.assertEqual(
            relay.drain_mw_settled_links(),
            ((game.game_id, 20),),
        )
        self.assertEqual(relay.drain_mw_settled_links(), ())

        post_transition_five = relay.handle_channel(
            self._wrapped(virtual[20], struct.pack("<II", 5, 0x33C54139)),
            host_endpoint,
            0,
        )
        self.assertEqual(
            [wire[6:] for wire, _target, _channel in post_transition_five],
            [struct.pack("<II", 5, 0x802CCB20)],
        )

    def test_mw_wrapped_channels_demangle_after_owner_token_arrives_first(self) -> None:
        directory = SessionDirectory()
        game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(game.game_id, 20))
        relay = ClassicRaceRelay()
        virtual = relay.register_virtual_game(game)
        host_endpoint = ("127.0.0.1", 44182)
        guest_endpoint = ("127.0.0.1", 3658)

        self.assertEqual(
            relay.handle_channel(
                self._wrapped(virtual[20], b"MWRG-guest"),
                guest_endpoint,
                1,
            ),
            (),
        )
        seeded = relay.handle_channel(
            self._wrapped(virtual[10], b"MWRG-host"),
            host_endpoint,
            0,
        )
        self.assertEqual(len(seeded), 2)

        natural_host_probe = relay.handle_channel(
            self._wrapped(
                virtual[20],
                struct.pack("<II", 5, 0x33C54139),
            ),
            host_endpoint,
            0,
        )
        self.assertEqual(
            [wire[6:] for wire, _target, _channel in natural_host_probe],
            [struct.pack("<II", 5, 0x33C54139)],
        )

        guest_probe = struct.pack("<II", 1, 0x802CCB20)
        replies = relay.handle_channel(
            self._wrapped(virtual[20], guest_probe),
            guest_endpoint,
            1,
        )
        host_payloads = [
            wire[6:]
            for wire, target, reply_channel in replies
            if (target, reply_channel) == (host_endpoint, 0)
        ]
        guest_payloads = [
            wire[6:]
            for wire, target, reply_channel in replies
            if (target, reply_channel) == (guest_endpoint, 1)
        ]
        self.assertEqual(
            host_payloads,
            [struct.pack("<II", 1, 0x33C54139)],
        )
        self.assertEqual(guest_payloads, [])

        normalized_host_probe = relay.handle_channel(
            self._wrapped(
                virtual[20],
                struct.pack("<II", 5, 0x33C54139),
            ),
            host_endpoint,
            0,
        )
        normalized_host_payloads = [
            wire[6:]
            for wire, target, reply_channel in normalized_host_probe
            if (target, reply_channel) == (host_endpoint, 0)
        ]
        normalized_guest_payloads = [
            wire[6:]
            for wire, target, reply_channel in normalized_host_probe
            if (target, reply_channel) == (guest_endpoint, 1)
        ]
        self.assertEqual(
            normalized_host_payloads,
            [],
        )
        self.assertEqual(
            normalized_guest_payloads,
            [
                struct.pack("<II", 5, 0x802CCB20),
            ],
        )

        native_host_two = relay.handle_channel(
            self._wrapped(
                virtual[20],
                struct.pack("<II", 2, 0x33C54139),
            ),
            host_endpoint,
            0,
        )
        self.assertEqual(
            [wire[6:] for wire, _target, _channel in native_host_two],
            [struct.pack("<II", 2, 0x802CCB20)],
        )

    def test_reused_host_endpoint_rebinds_to_new_game(self) -> None:
        directory = SessionDirectory()
        relay = ClassicRaceRelay()
        host_endpoint = ("127.0.0.1", 3658)

        first_game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(first_game.game_id, 20))
        relay.set_public_host("127.0.0.1")
        first_advertised = relay.register_game(first_game)
        relay.handle(
            self._wrapped(first_advertised[20], struct.pack("<II", 5, 0)),
            host_endpoint,
        )

        second_game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(second_game.game_id, 20))
        second_advertised = relay.register_game(second_game)
        self.assertEqual(
            relay.handle(
                self._wrapped(second_advertised[20], struct.pack("<II", 5, 0)),
                host_endpoint,
            ),
            (),
        )
        replies = relay.handle(
            self._wrapped(second_advertised[10], struct.pack("<II", 1, 0)),
            ("127.0.0.1", 44515),
        )
        self.assertEqual(len(replies), 2)
        self.assertEqual(
            {struct.unpack("<II", wire[6:]) for wire, _ in replies},
            {(1, 0), (5, 0)},
        )

    def test_mw_postrace_handoff_rebinds_two_player_room(self) -> None:
        directory = SessionDirectory()
        previous = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(previous.game_id, 20))
        previous.participant_wire_ids = {10: 1, 20: 2}

        relay = ClassicRaceRelay()
        advertised = relay.register_shared_virtual_game(previous)
        old_guest = ("203.0.113.20", 42000)
        old_host = ("203.0.113.10", 41000)
        self.assertEqual(
            relay.handle_channel(
                self._wrapped(
                    advertised[10],
                    struct.pack("<II", 1, 0x1111),
                ),
                old_guest,
                0,
            ),
            (),
        )
        relay.handle_channel(
            self._wrapped(
                advertised[20],
                struct.pack("<II", 5, 0xAAAA),
            ),
            old_host,
            0,
        )

        replacement = directory.create_game(0, 10, capacity=2)
        replacement.participant_order = list(previous.participant_order)
        self.assertEqual(relay.handoff_game(previous, replacement), advertised)

        # Host-first post-race: the guest has not completed GJOI for the new
        # room yet, so its old gameplay/post-race socket must remain usable.
        old_guest_payload = relay.handle_channel(
            self._wrapped(advertised[10], b"old-guest-to-host"),
            old_guest,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in old_guest_payload},
            {old_host},
        )
        old_host_payload = relay.handle_channel(
            self._wrapped(advertised[20], b"old-host-to-guest"),
            old_host,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in old_host_payload},
            {old_guest},
        )

        self.assertTrue(directory.join_game(replacement.game_id, 20))
        replacement.participant_wire_ids = {10: 1, 20: 2}
        self.assertEqual(
            relay.register_shared_virtual_game(replacement),
            advertised,
        )

        new_guest = ("203.0.113.20", 52000)
        guest_bootstrap = relay.handle_channel(
            self._wrapped(
                advertised[10],
                struct.pack("<II", 1, 0x2222),
            ),
            new_guest,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in guest_bootstrap},
            {old_host},
        )
        bootstrap = relay.handle_channel(
            self._wrapped(
                advertised[20],
                struct.pack("<II", 5, 0xBBBB),
            ),
            old_host,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in bootstrap},
            {new_guest},
        )

        host_payload = relay.handle_channel(
            self._wrapped(advertised[20], b"host-to-guest"),
            old_host,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in host_payload},
            {new_guest},
        )
        guest_payload = relay.handle_channel(
            self._wrapped(advertised[10], b"guest-to-host"),
            new_guest,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in guest_payload},
            {old_host},
        )
        self.assertTrue(relay.unregister_game(replacement))

    def test_mw_postrace_handoff_rebinds_three_player_spokes(self) -> None:
        directory = SessionDirectory()
        previous = directory.create_game(0, 10, capacity=4)
        self.assertTrue(directory.join_game(previous.game_id, 20))
        previous.participant_wire_ids = {10: 1, 20: 2}
        relay = ClassicRaceRelay()
        advertised = relay.register_shared_virtual_game(previous)

        old_host_one = ("192.0.2.10", 41001)
        old_host_two = ("192.0.2.10", 41002)
        old_guest_one = ("192.0.2.55", 42001)
        old_guest_two = ("192.0.2.55", 43001)

        # Establish the first owner/guest spoke while the room still contains
        # two players, then add the late third player exactly as retail MW does.
        self.assertEqual(
            relay.handle_channel(
                self._wrapped(
                    advertised[10],
                    struct.pack("<II", 1, 0x1111),
                ),
                old_guest_one,
                0,
            ),
            (),
        )
        relay.handle_channel(
            self._wrapped(
                advertised[20],
                struct.pack("<II", 5, 0xAAAA),
            ),
            old_host_one,
            0,
        )
        self.assertTrue(directory.join_game(previous.game_id, 30))
        previous.participant_wire_ids[30] = 3
        advertised = relay.register_shared_virtual_game(previous)
        self.assertEqual(
            relay.handle_channel(
                self._wrapped(
                    advertised[10],
                    struct.pack("<II", 1, 0x2222),
                ),
                old_guest_two,
                0,
            ),
            (),
        )
        relay.handle_channel(
            self._wrapped(
                advertised[30],
                struct.pack("<II", 5, 0xBBBB),
            ),
            old_host_two,
            0,
        )

        replacement = directory.create_game(0, 10, capacity=4)
        replacement.participant_order = list(previous.participant_order)
        handed_off = relay.handoff_game(previous, replacement)
        self.assertEqual(handed_off, advertised)

        token = relay._game_tokens[id(replacement)]
        self.assertNotIn(id(previous), relay._game_tokens)
        self.assertEqual(relay._identity_to_endpoint[(token, 10)], old_host_two)
        self.assertEqual(relay._identity_to_endpoint[(token, 20)], old_guest_one)
        self.assertEqual(relay._identity_to_endpoint[(token, 30)], old_guest_two)

        # Host-first post-race: both guests are still on their old sockets when
        # the owner creates the replacement room. Each old directed spoke must
        # remain alive until that specific guest performs GJOI + command 1.
        first_old_guest_payload = relay.handle_channel(
            self._wrapped(advertised[10], b"first-old-to-host"),
            old_guest_one,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in first_old_guest_payload},
            {old_host_one},
        )
        second_old_guest_payload = relay.handle_channel(
            self._wrapped(advertised[10], b"second-old-to-host"),
            old_guest_two,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in second_old_guest_payload},
            {old_host_two},
        )
        first_old_host_payload = relay.handle_channel(
            self._wrapped(advertised[20], b"host-to-first-old"),
            old_host_one,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in first_old_host_payload},
            {old_guest_one},
        )
        second_old_host_payload = relay.handle_channel(
            self._wrapped(advertised[30], b"host-to-second-old"),
            old_host_two,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in second_old_host_payload},
            {old_guest_two},
        )

        # Both guests rejoin before the replacement room starts its UDP
        # bootstrap. The virtual .1/.2/.3 identities stay stable, but every
        # participant uses a fresh physical source port. All clients share one
        # IP here so the relay must use GJOI order, matching the Wine setup from
        # the captured failure.
        self.assertTrue(directory.join_game(replacement.game_id, 20))
        replacement.participant_wire_ids = {10: 1, 20: 2}
        relay.register_shared_virtual_game(replacement)
        self.assertTrue(directory.join_game(replacement.game_id, 30))
        replacement.participant_wire_ids[30] = 3
        replacement_addresses = relay.register_shared_virtual_game(replacement)
        self.assertEqual(replacement_addresses, advertised)

        new_guest_one = ("192.0.2.55", 52001)
        new_guest_two = ("192.0.2.55", 53001)
        shared_guest_token = 0xD3A8DC1D

        # Guest command 1 packets arrive first. Retail retains the old owner
        # spokes across the post-race return and emits no fresh host packet on
        # its own. Each command 1 must reach the corresponding preserved spoke
        # immediately, while fresh guest sockets are assigned in GJOI order.
        first_guest_bootstrap = relay.handle_channel(
            self._wrapped(
                replacement_addresses[10],
                struct.pack("<II", 1, shared_guest_token),
            ),
            new_guest_one,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in first_guest_bootstrap},
            {old_host_one},
        )

        # Rebinding guest 20 must not tear down guest 30's old spoke. This is
        # the partial host-first window seen with three/four retail clients.
        second_still_old = relay.handle_channel(
            self._wrapped(advertised[10], b"second-still-old"),
            old_guest_two,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in second_still_old},
            {old_host_two},
        )

        second_guest_bootstrap = relay.handle_channel(
            self._wrapped(
                replacement_addresses[10],
                struct.pack("<II", 1, shared_guest_token),
            ),
            new_guest_two,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in second_guest_bootstrap},
            {old_host_two},
        )

        first_bootstrap = relay.handle_channel(
            self._wrapped(
                replacement_addresses[20],
                struct.pack("<II", 5, 0xCCCC),
            ),
            old_host_one,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in first_bootstrap},
            {new_guest_one},
        )
        second_bootstrap = relay.handle_channel(
            self._wrapped(
                replacement_addresses[30],
                struct.pack("<II", 5, 0xDDDD),
            ),
            old_host_two,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in second_bootstrap},
            {new_guest_two},
        )

        routes = (
            (
                self._wrapped(replacement_addresses[20], b"host-to-first"),
                old_host_one,
                new_guest_one,
            ),
            (
                self._wrapped(replacement_addresses[30], b"host-to-second"),
                old_host_two,
                new_guest_two,
            ),
            (
                self._wrapped(replacement_addresses[10], b"first-to-host"),
                new_guest_one,
                old_host_one,
            ),
            (
                self._wrapped(replacement_addresses[10], b"second-to-host"),
                new_guest_two,
                old_host_two,
            ),
        )
        for wire, source, expected_target in routes:
            replies = relay.handle_channel(wire, source, 0)
            self.assertEqual(
                {target for _response, target, _channel in replies},
                {expected_target},
            )

        # A second consecutive host-first return must preserve the sockets that
        # were learned for the first replacement, not resurrect generation 1.
        second_replacement = directory.create_game(0, 10, capacity=4)
        second_handoff = relay.handoff_game(replacement, second_replacement)
        self.assertEqual(second_handoff, replacement_addresses)
        first_generation_two = relay.handle_channel(
            self._wrapped(replacement_addresses[10], b"gen2-first-old"),
            new_guest_one,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in first_generation_two},
            {old_host_one},
        )
        second_generation_two = relay.handle_channel(
            self._wrapped(replacement_addresses[10], b"gen2-second-old"),
            new_guest_two,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in second_generation_two},
            {old_host_two},
        )

        self.assertFalse(relay.unregister_game(previous))
        self.assertFalse(relay.unregister_game(replacement))
        self.assertTrue(relay.unregister_game(second_replacement))

    def test_mw_postrace_handoff_rebinds_four_players_in_gjoi_order(self) -> None:
        directory = SessionDirectory()
        previous = directory.create_game(0, 10, capacity=4)
        for user_id in (20, 30, 40):
            self.assertTrue(directory.join_game(previous.game_id, user_id))
        previous.participant_wire_ids = {10: 1, 20: 2, 30: 3, 40: 4}

        relay = ClassicRaceRelay()
        advertised = relay.register_shared_virtual_game(previous)
        replacement = directory.create_game(0, 10, capacity=4)
        replacement.participant_order = list(previous.participant_order)
        self.assertEqual(relay.handoff_game(previous, replacement), advertised)

        # The guests deliberately return in a different order from the old
        # room. register_game must preserve that staged GJOI order so three
        # same-IP command-1 packets can still be assigned deterministically.
        join_order = (30, 20, 40)
        for user_id in join_order:
            self.assertTrue(directory.join_game(replacement.game_id, user_id))
            replacement.participant_wire_ids[user_id] = user_id // 10
            relay.register_shared_virtual_game(replacement)

        token = relay._game_tokens[id(replacement)]
        shared_guest_token = 0xD3A8DC1D
        guest_endpoints = {
            30: ("198.51.100.77", 53030),
            20: ("198.51.100.77", 53020),
            40: ("198.51.100.77", 53040),
        }
        for index, user_id in enumerate(join_order, start=1):
            self.assertEqual(
                relay.handle_channel(
                    self._wrapped(
                        advertised[10],
                        struct.pack("<II", 1, shared_guest_token),
                    ),
                    guest_endpoints[user_id],
                    0,
                ),
                (),
            )
            self.assertEqual(
                relay._identity_to_endpoint[(token, user_id)],
                guest_endpoints[user_id],
            )

        for index, user_id in enumerate(join_order, start=1):
            host_spoke = ("198.51.100.10", 54000 + index)
            replies = relay.handle_channel(
                self._wrapped(
                    advertised[user_id],
                    struct.pack("<II", 5, 0x6000 + index),
                ),
                host_spoke,
                0,
            )
            self.assertEqual(
                {target for _response, target, _channel in replies},
                {guest_endpoints[user_id], host_spoke},
            )

        self.assertTrue(relay.unregister_game(replacement))

    def test_mw_postrace_handoff_drops_guest_absent_at_next_race_start(self) -> None:
        directory = SessionDirectory()
        previous = directory.create_game(0, 10, capacity=4)
        self.assertTrue(directory.join_game(previous.game_id, 20))
        self.assertTrue(directory.join_game(previous.game_id, 30))
        previous.participant_wire_ids = {10: 1, 20: 2, 30: 3}

        relay = ClassicRaceRelay()
        advertised = relay.register_shared_virtual_game(previous)
        replacement = directory.create_game(0, 10, capacity=4)
        self.assertEqual(relay.handoff_game(previous, replacement), advertised)

        # Only guest 20 returns. Once the next race starts, guest 30 can no
        # longer join that generation and its retained old identity must be
        # retired without disturbing the returning guest.
        self.assertTrue(directory.join_game(replacement.game_id, 20))
        replacement.participant_wire_ids = {10: 1, 20: 2}
        relay.register_shared_virtual_game(replacement)
        self.assertTrue(
            directory.set_state(replacement.game_id, SessionState.ACTIVE)
        )
        next_addresses = relay.register_shared_virtual_game(replacement)

        token = relay._game_tokens[id(replacement)]
        self.assertEqual(set(next_addresses), {10, 20})
        self.assertEqual(
            relay._participants[token],
            ((token, 10), (token, 20)),
        )
        self.assertNotIn((token, 30), relay._identity_to_virtual)
        self.assertNotIn((token, 30), relay._mw_handoff_candidates.get(token, ()))

        new_guest = ("198.51.100.20", 52020)
        new_host = ("198.51.100.10", 51020)
        self.assertEqual(
            relay.handle_channel(
                self._wrapped(
                    next_addresses[10],
                    struct.pack("<II", 1, 0x2222),
                ),
                new_guest,
                0,
            ),
            (),
        )
        bootstrap = relay.handle_channel(
            self._wrapped(
                next_addresses[20],
                struct.pack("<II", 5, 0xBBBB),
            ),
            new_host,
            0,
        )
        self.assertEqual(
            {target for _response, target, _channel in bootstrap},
            {new_guest, new_host},
        )
        self.assertNotIn(token, relay._mw_handoff_candidates)
        self.assertNotIn(token, relay._mw_handoff_rebind_order)
        self.assertTrue(relay.unregister_game(replacement))

    def test_virtual_addresses_are_unique_across_registered_games(self) -> None:
        directory = SessionDirectory()
        relay = ClassicRaceRelay(virtual_network="198.18.0.0/29")

        first = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(first.game_id, 20))
        first_addresses = set(relay.register_game(first).values())

        second = directory.create_game(0, 30, capacity=2)
        self.assertTrue(directory.join_game(second.game_id, 40))
        second_addresses = set(relay.register_game(second).values())

        self.assertTrue(first_addresses.isdisjoint(second_addresses))

    def test_unregister_game_removes_routes_and_pending_packets(self) -> None:
        directory = SessionDirectory()
        relay = ClassicRaceRelay()
        game = directory.create_game(0, 10, capacity=2)
        self.assertTrue(directory.join_game(game.game_id, 20))
        advertised = relay.register_virtual_game(game)
        host_endpoint = ("127.0.0.1", 41000)

        self.assertEqual(
            relay.handle_channel(
                self._wrapped(advertised[20], b"pending-host-packet"),
                host_endpoint,
                0,
            ),
            (),
        )
        self.assertTrue(relay.unregister_game(game))
        self.assertFalse(relay.unregister_game(game))
        self.assertEqual(
            relay.handle_channel(
                self._wrapped(advertised[10], b"late-guest-packet"),
                ("127.0.0.1", 42000),
                1,
            ),
            (),
        )
