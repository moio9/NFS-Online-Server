"""Real-capture regression checks for Carbon ProtoTunnel and CommUDP."""

import unittest

from carbon.transport.commudp import (
    CommUDPActive,
    CommUDPControl,
    CommUDPType,
    parse_channel_one,
    parse_session_ticket,
)
from carbon.accounts.identity import IdentityStore
from carbon.core.config import Endpoint
from carbon.rebroadcaster.service import (
    CarbonRebroadcasterService,
    EndpointWireState,
)
from carbon.theater.directory import CarbonGameDirectory
from carbon.transport.prototunnel import (
    ProtoTunnelError,
    TunnelDatagram,
    TunnelPacket,
    cipher_region_size,
    decode_datagram,
)
from carbon.transport.rc4 import rc4_xor
from carbon.rebroadcaster.handshake import (
    EndpointHandshake,
    HandshakeRole,
    HandshakeStage,
    hash_sar_decimal,
)


EKEY = b"9181081919"
RANKED_EKEY = b"5290806491"


def _uncached_rc4(data: bytes, key: bytes, *, skip: int = 0) -> bytes:
    """Small reference implementation for absolute-offset cache regressions."""
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 0xFF
        state[i], state[j] = state[j], state[i]
    i = 0
    j = 0
    output = bytearray()
    for position in range(skip + len(data)):
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        if position >= skip:
            output.append(
                data[position - skip] ^ state[(state[i] + state[j]) & 0xFF]
            )
    return bytes(output)


class CarbonTransportCaptureTests(unittest.TestCase):
    @staticmethod
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

    def test_invite_create_connect_packet_round_trips_exactly(self) -> None:
        # create&invitejoin.pcapng frame 435, client -> rebroadcaster.
        raw = bytes.fromhex("00005cc5de2eb7abc2a6cea5146dc038618c927b")
        datagram = decode_datagram(raw, EKEY)
        self.assertEqual(datagram.offset_words, 0)
        self.assertEqual([(packet.channel, packet.payload.hex()) for packet in datagram.packets], [
            (7, "000000071000"),
            (1, "00000001dcf7b035"),
        ])
        control = parse_channel_one(datagram.packets[1])
        self.assertEqual(control, CommUDPControl(CommUDPType.CONNECT, 0xDCF7B035))
        self.assertEqual(datagram.encode(EKEY), raw)

        handshake = EndpointHandshake()
        reply = handshake.accept(datagram)
        self.assertIsNotNone(reply)
        self.assertEqual(handshake.connection_id, 0xDCF7B035)
        self.assertIs(handshake.stage, HandshakeStage.CONNECT_SENT)
        self.assertEqual(
            reply.encode(EKEY).hex(),
            "0005b3623213fa0a68dcfdd54cfdbaed8aa2b34bee8fa1b4d842ce529bbc",
        )
        confirmation = decode_datagram(
            bytes.fromhex(
                "0005b3843213f8fa68dcfdd1010b0ad88aa06ebc5ebaa14bd84313a52b8977f0"
                "6d879c2bea2b2419d8c9bd1d18dfb5fecf791f2cd4649a34b1bb1f2bd64d83"
            ),
            EKEY,
        )
        self.assertIsNone(handshake.accept(confirmation))
        self.assertIs(handshake.stage, HandshakeStage.ESTABLISHED)
        active = [parse_channel_one(packet) for packet in confirmation.packets]
        ticket = next(
            parse_session_ticket(item.game_manager)
            for item in active
            if isinstance(item, CommUDPActive) and item.game_manager is not None
        )
        self.assertEqual(ticket, "2589946333")

    def test_decode_accepts_reconstructed_offset_after_header_wrap(self) -> None:
        # Live client UDP/1042 reaches this after a busy race: the wire header
        # contains 0x070f but RC4 is actually at 0x1070f words.
        low_offset = 0x070F
        full_offset = 0x1070F
        packets = (TunnelPacket(1, bytes.fromhex("0000000006000003d4")),) * 4
        headers = bytes((0x00, 0x91)) * len(packets)
        bodies = b"".join(packet.payload for packet in packets)
        raw = low_offset.to_bytes(2, "big") + rc4_xor(
            headers + bodies,
            EKEY,
            skip=full_offset * 4,
        )
        with self.assertRaises(ProtoTunnelError):
            decode_datagram(raw, EKEY)
        decoded = decode_datagram(raw, EKEY, stream_offset_words=full_offset)
        self.assertEqual(decoded.offset_words, low_offset)
        self.assertEqual(decoded.packets, packets)

    def test_encode_keeps_rc4_stream_continuous_after_header_wrap(self) -> None:
        full_offset = 0x1070F
        packets = (TunnelPacket(1, bytes.fromhex("0000000006000003d4")),)

        raw = TunnelDatagram(full_offset, packets).encode(EKEY)

        self.assertEqual(raw[:2], bytes.fromhex("070f"))
        decoded = decode_datagram(raw, EKEY, stream_offset_words=full_offset)
        self.assertEqual(decoded.packets, packets)

    def test_rc4_checkpoint_resume_is_byte_exact_after_wrap_and_rewind(self) -> None:
        payload = bytes(range(64))
        offsets = (
            0x1070F * 4,
            (0x1070F * 4) + 0x180,
            (0x1070F * 4) - 0x80,
            (0x1070F * 4) + 0x240,
        )

        # Exercise forward reuse, then a rewind within the retained checkpoint
        # window. Every result must still match an RC4 stream rebuilt at zero.
        for offset in offsets:
            self.assertEqual(
                rc4_xor(payload, EKEY, skip=offset),
                _uncached_rc4(payload, EKEY, skip=offset),
            )

    def test_mixed_channels_use_native_identity_encrypted_clear_order(self) -> None:
        packets = (
            TunnelPacket(1, b"encrypted-lobby"),
            TunnelPacket(0, b"clear-diagnostic"),
            TunnelPacket(7, bytes.fromhex("000000071001")),
        )
        raw = TunnelDatagram(9, packets).encode(EKEY)
        decoded = decode_datagram(raw, EKEY, stream_offset_words=9)
        native_headers = bytes((
            0x00, 0x67,
            0x00, 0xF1,
            0x01, 0x00,
        ))
        expected = (
            bytes.fromhex("0009")
            + rc4_xor(
                native_headers + packets[2].payload + packets[0].payload,
                EKEY,
                skip=9 * 4,
            )
            + packets[1].payload
        )
        self.assertEqual(raw, expected)
        self.assertEqual(
            decoded.packets,
            (
                packets[2],
                packets[0],
                packets[1],
            ),
        )

        # Channel 0 is copied verbatim on the wire. The encrypted channels and
        # descriptor table remain opaque. Native send prepends channel 7, then
        # stable-partitions encrypted virtual packets before clear packets.
        self.assertIn(b"clear-diagnostic", raw)
        self.assertTrue(raw.endswith(b"clear-diagnostic"))
        self.assertNotIn(b"encrypted-lobby", raw)
        self.assertNotIn(bytes.fromhex("000000071001"), raw)

    def test_native_send_limits_reject_invalid_batches(self) -> None:
        with self.assertRaises(ProtoTunnelError):
            TunnelDatagram(
                0,
                tuple(TunnelPacket(1, bytes((index,))) for index in range(9)),
            ).encode(EKEY)
        with self.assertRaises(ProtoTunnelError):
            TunnelDatagram(0, (TunnelPacket(1, b"x" * 996),)).encode(EKEY)
        with self.assertRaises(ProtoTunnelError):
            TunnelDatagram(0, (TunnelPacket(7, b"short"),)).encode(EKEY)
        with self.assertRaises(ProtoTunnelError):
            TunnelDatagram(
                0,
                tuple(TunnelPacket(1, b"x" * 123) for _ in range(8)),
            ).encode(EKEY)

    def test_service_offset_ignores_clear_payload_suffix(self) -> None:
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        service = CarbonRebroadcasterService(games)
        destination = ("198.51.100.20", 20000)
        packets = (
            TunnelPacket(1, b"encrypted-lobby"),
            TunnelPacket(0, b"clear-diagnostic"),
        )
        service._wire[destination] = EndpointWireState(
            tunnel_key=EKEY,
            next_offset_words=9,
        )
        replies: list[tuple[bytes, tuple[str, int]]] = []

        service._append_datagram(replies, TunnelDatagram(9, packets), destination)

        cipher_bytes = cipher_region_size(packets)
        self.assertEqual(
            service._wire[destination].next_offset_words,
            9 + ((cipher_bytes + 3) // 4),
        )
        self.assertTrue(replies[0][0].endswith(b"clear-diagnostic"))

    def test_ranked_dedicated_handshake_matches_official_capture(self) -> None:
        # rankedcrash.pcapng frames 556-557: the first dedicated participant
        # sends the 0x37 leg and nfsdevserver answers directly with ID 0/state
        # 0x35 plus the combined CommUDP Type2+Type1 response.
        incoming = bytes.fromhex("0000596fe7f4e5594a2a865783fe8f46627bba62")
        expected = bytes.fromhex(
            "00051a430dc691d80d224f2a2043c402661f3b07bec91b623ca1e05fab1b"
        )
        handshake = EndpointHandshake(
            server_tunnel_id=hash_sar_decimal(5),
            dedicated=True,
        )
        reply = handshake.accept(decode_datagram(incoming, RANKED_EKEY))
        self.assertIsNotNone(reply)
        self.assertEqual(reply.encode(RANKED_EKEY), expected)
        self.assertIs(handshake.role, HandshakeRole.HOST)

        # rankedcrash frames 668-669 use the same dedicated response for the
        # later 0x10 client-initiated leg.
        incoming = bytes.fromhex("0000596fe7f4e5594a2aa15783fe8f46627bba34")
        expected = bytes.fromhex(
            "00051a430dc691d80d224f2a2043c402661f3b07be9f1b623ca1e05fab4d"
        )
        handshake = EndpointHandshake(
            server_tunnel_id=hash_sar_decimal(5),
            dedicated=True,
        )
        reply = handshake.accept(decode_datagram(incoming, RANKED_EKEY))
        self.assertIsNotNone(reply)
        self.assertEqual(reply.encode(RANKED_EKEY), expected)

    def test_rebroadcaster_selects_dedicated_handshake_from_egam_port(self) -> None:
        identities = IdentityStore(token_factory=lambda: "token.")
        identity, _ = identities.login("RankedDriver")
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        game = games.create(
            identity,
            {"B-U-game_type": "0", "B-U-matchmaking_state": "1"},
            server_hosted=True,
        )
        participant = games.enter(
            game.gid,
            identity,
            internal_ip="192.168.1.8",
            internal_port=1042,
        )
        self.assertIsNotNone(participant)
        service = CarbonRebroadcasterService(games)
        request = TunnelDatagram(
            0,
            (
                TunnelPacket(7, bytes.fromhex("000000073700")),
                TunnelPacket(1, bytes.fromhex("00000001258f639d")),
            ),
        ).encode(EKEY)
        replies = service.handle_datagram(request, ("127.0.0.1", 1042))
        self.assertEqual(len(replies), 1)
        response = decode_datagram(replies[0][0], EKEY)
        self.assertEqual(response.offset_words, 5)
        self.assertEqual(response.packets[0], TunnelPacket(7, bytes.fromhex("002768fb6001")))
        controls = [parse_channel_one(packet) for packet in response.packets[1:]]
        self.assertEqual(
            controls,
            [
                CommUDPControl(CommUDPType.CONNECT_ACK, 0x258F639D),
                CommUDPControl(CommUDPType.CONNECT, 0x258F639D),
            ],
        )

    def test_rebroadcaster_disambiguates_same_port_dedicated_clients_by_ip(self) -> None:
        """Two stock UDP/1042 clients must retain the dedicated HUID.

        Live 05:23:37 regression: simultaneous participants at
        192.168.1.14:1042 and 192.168.1.150:1042 made the old port-only hint
        ambiguous.  Both endpoints were then mislabeled client-hosted and got
        HUID 51 instead of the dedicated server identity.
        """
        identities = IdentityStore(token_factory=lambda: "token.")
        games = CarbonGameDirectory(Endpoint("192.168.1.150", 19118))
        addresses = (
            ("192.168.1.14", 1042),
            ("192.168.1.150", 1042),
        )
        for index, address in enumerate(addresses, start=1):
            identity, _ = identities.login(f"Driver{index}")
            game = games.create(
                identity,
                {"B-U-game_type": "1", "B-U-matchmaking_state": "1"},
                server_hosted=True,
            )
            participant = games.enter(
                game.gid,
                identity,
                internal_ip=address[0],
                internal_port=address[1],
            )
            self.assertIsNotNone(participant)

        service = CarbonRebroadcasterService(games)
        request = TunnelDatagram(
            0,
            (
                TunnelPacket(7, bytes.fromhex("00de85014c00")),
                TunnelPacket(1, bytes.fromhex("00000001b6ee1fb9")),
            ),
        ).encode(EKEY)

        for address in addresses:
            replies = service.handle_datagram(request, address)
            self.assertEqual(len(replies), 1)
            response = decode_datagram(replies[0][0], EKEY)
            self.assertEqual(
                response.packets[0],
                TunnelPacket(7, bytes.fromhex("002768fb6001")),
            )
            self.assertTrue(service._endpoints[address].dedicated)

    def test_rebroadcaster_prefers_latest_egam_on_endpoint_handover(self) -> None:
        """A stale room member must not downgrade a reused UDP/1042 socket.

        Live 02:19:51 regression: the previous room still contained an
        unbound participant at 192.168.1.150:1042 when the same client accepted
        a new invite.  Both exact endpoint hints were considered ambiguous, so
        the dedicated joiner received the client-hosted HUID 51 and repeated
        CONNECT forever without sending its ticket.
        """
        identities = IdentityStore(token_factory=lambda: "token.")
        games = CarbonGameDirectory(Endpoint("192.168.1.150", 19118))
        address = ("192.168.1.150", 1042)

        old_identity, _ = identities.login("OldDriver")
        old_game = games.create(
            old_identity,
            {"B-U-game_type": "2", "B-U-matchmaking_state": "0"},
            server_hosted=True,
        )
        old_participant = games.enter(
            old_game.gid,
            old_identity,
            internal_ip=address[0],
            internal_port=address[1],
        )
        self.assertIsNotNone(old_participant)

        service = CarbonRebroadcasterService(games)
        old_resolution = games.resolve_ticket(
            games.ticket(old_game, old_participant)
        )
        self.assertIsNotNone(old_resolution)
        self.assertTrue(service._bind(address, old_resolution))

        new_identity, _ = identities.login("NewDriver")
        new_game = games.create(
            new_identity,
            {"B-U-game_type": "2", "B-U-matchmaking_state": "0"},
            server_hosted=True,
        )
        new_participant = games.enter(
            new_game.gid,
            new_identity,
            internal_ip=address[0],
            internal_port=address[1],
        )
        self.assertIsNotNone(new_participant)
        self.assertGreater(new_participant.entered_at, old_participant.entered_at)

        request = TunnelDatagram(
            0,
            (
                TunnelPacket(7, bytes.fromhex("00d0b385aa00")),
                TunnelPacket(1, bytes.fromhex("00000001b6ac4b49")),
            ),
        ).encode(EKEY)
        replies = service.handle_datagram(request, address)

        self.assertEqual(len(replies), 1)
        response = decode_datagram(replies[0][0], EKEY)
        self.assertEqual(
            response.packets[0],
            TunnelPacket(7, bytes.fromhex("002768fb6001")),
        )
        self.assertTrue(service._endpoints[address].dedicated)
        self.assertEqual(
            service._endpoints[address].server_tunnel_id,
            hash_sar_decimal(games.server_huid),
        )

    def test_rebroadcaster_promotes_unbound_handshake_after_late_egam(self) -> None:
        """A pre-EGAM CONNECT must not permanently cache client-hosted HUID 51.

        Live 02:33:29 regression: retransmissions from the previous client
        session reached the freshly restarted server before any game existed.
        The endpoint was therefore born client-hosted and remained so when a
        valid dedicated EGAM arrived one minute later.
        """
        identities = IdentityStore(token_factory=lambda: "token.")
        games = CarbonGameDirectory(Endpoint("192.168.1.150", 19118))
        service = CarbonRebroadcasterService(games)
        address = ("192.168.1.150", 1042)

        stale_connect = TunnelDatagram(
            0,
            (
                TunnelPacket(7, bytes.fromhex("00d0b385aa00")),
                TunnelPacket(1, bytes.fromhex("00000001b69c4b49")),
            ),
        ).encode(EKEY)
        stale_replies = service.handle_datagram(stale_connect, address)
        self.assertEqual(len(stale_replies), 1)
        self.assertFalse(service._endpoints[address].dedicated)
        self.assertEqual(service._endpoints[address].server_tunnel_id, 0x691)

        identity, _ = identities.login("InvitedDriver")
        game = games.create(
            identity,
            {"B-U-game_type": "2", "B-U-matchmaking_state": "0"},
            server_hosted=True,
        )
        participant = games.enter(
            game.gid,
            identity,
            internal_ip=address[0],
            internal_port=address[1],
        )
        self.assertIsNotNone(participant)

        invite_connect = TunnelDatagram(
            0,
            (
                TunnelPacket(7, bytes.fromhex("00d0b385aa00")),
                TunnelPacket(1, bytes.fromhex("00000001b1cc4b49")),
            ),
        ).encode(EKEY)
        replies = service.handle_datagram(invite_connect, address)

        self.assertEqual(len(replies), 1)
        response = decode_datagram(replies[0][0], EKEY)
        self.assertEqual(
            response.packets[0],
            TunnelPacket(7, bytes.fromhex("002768fb6001")),
        )
        self.assertTrue(service._endpoints[address].dedicated)
        self.assertEqual(
            service._endpoints[address].server_tunnel_id,
            hash_sar_decimal(games.server_huid),
        )

    def test_live_ranked_huid_hash_drives_channel7_identity(self) -> None:
        # V689/V690 live failure: EGEG advertised HUID=799270239 while the
        # handshake replied with capture-specific hashes for HUID 51 or 5.
        # Carbon therefore ignored an otherwise valid Type2+Type1 response.
        self.assertEqual(hash_sar_decimal(5), 0x00000035)
        self.assertEqual(hash_sar_decimal(51), 0x00000691)
        self.assertEqual(hash_sar_decimal(799270239), 0x2768FB60)
        self.assertEqual(hash_sar_decimal(637968184), 0x4E8308BE)

        incoming = TunnelDatagram(
            0,
            (
                TunnelPacket(7, bytes.fromhex("004e8308be00")),
                TunnelPacket(1, bytes.fromhex("00000001258f639d")),
            ),
        ).encode(EKEY)
        handshake = EndpointHandshake(
            server_tunnel_id=hash_sar_decimal(799270239),
            dedicated=True,
        )
        reply = handshake.accept(decode_datagram(incoming, EKEY))
        self.assertIsNotNone(reply)
        self.assertEqual(reply.packets[0], TunnelPacket(7, bytes.fromhex("002768fb6001")))
        self.assertEqual(
            reply.encode(EKEY).hex(),
            "0005b3623213fa0a68fb9528bdfdbaed8aa24a333d27a1b4d842372a4814",
        )

    def test_invite_create_host_bootstrap_decodes_real_gm_messages(self) -> None:
        # create&invitejoin.pcapng frame 440, rebroadcaster -> host.
        raw = bytes.fromhex(
            "00129bf81b650f2b034ec7e9f9f6d7b17b60aa080e4632a1dbc97ff3ce58977d"
            "c2dfc64a65286512ad31c6ad3a9f3e9d0754c535480d3fe327376703cd8652ea"
            "35e9eaeb20d31326573705a145ac3eeca1f2fbc9df0b5a3b577e55db2da8c79"
            "c9a2ffbbfc7f297f2d0243358df095d23688b9646d87b8c2f32410ebeddf822c"
            "979aab5848d5ab17f012f2f78b5f12fe092212b227a4262"
        )
        datagram = decode_datagram(raw, EKEY)
        active = [parse_channel_one(packet) for packet in datagram.packets if packet.channel == 1]
        active = [packet for packet in active if isinstance(packet, CommUDPActive)]
        self.assertEqual([packet.sequence for packet in active], [0x10000101, 0x102])
        self.assertEqual([packet.game_manager.message_type for packet in active], [0x02, 0x03])
        self.assertEqual(datagram.encode(EKEY), raw)

    def test_invite_joiner_handshake_matches_all_capture_legs(self) -> None:
        incoming = [
            "00005cc5de2eb7abc2a6e9a5146dc038618c922d",  # frame 746
            "0005b3623213fa0a68dcfdd4eafdbaed8aa2b34beed9a1b4d842ce529bea",  # 748
            (
                "000c778e6c861e5e6a2b2411360b354721eb82cdfc4a1d2e7b5b99c91e841fd4"
                "03cf47ed78f6d639c95112b1b6720492e8fb7c8e3b5a958037ddc64ab12a29"
            ),  # 750
        ]
        expected = [
            "00005cc5de2eb7abc2a74fa5146dc038618c922d",  # frame 747
            "0005b3623213fa0a68dcfdd54cfdbaed8aa2b34beed9a1b4d842ce529bea",  # 749
            "000c778e6c861c2f6a29f8e45a9f852420eb83cdfcb5",  # 751
        ]
        handshake = EndpointHandshake()
        for request, response in zip(incoming, expected):
            reply = handshake.accept(decode_datagram(bytes.fromhex(request), EKEY))
            self.assertIsNotNone(reply)
            self.assertEqual(reply.encode(EKEY).hex(), response)
        self.assertIs(handshake.role, HandshakeRole.JOINER)
        self.assertIs(handshake.stage, HandshakeStage.ESTABLISHED)

    def test_joiner_bootstrap_contains_two_rosters(self) -> None:
        # create&invitejoin.pcapng frame 758, rebroadcaster -> joiner.
        raw = bytes.fromhex(
            "00173190de297bb2de58967cc2dfc74a64aaed162cb143afc730de97655fb435"
            "480d3fa32a376702cd8650eb31e30a3b075870f62c4769c03c89416ca1f3f9c"
            "9df0a00b9d47e0f5b2da8c1ecf7da82dab57297f2d0243358858f5d23688b96"
            "46d87b05bb32410ebeddf8224179aab5848d5ab17f81ef877937f53d5c8aaa7b"
            "267aca66b9af6136d08ff42b18def88ab9ddb59aceee03e1945634257b1017954"
            "3ba30c372a69cbf16c89fdccf0882c06b7ef4bd3cf394c444e71e2d24e5b68e"
            "54c174193dfede05707cceb47cc47c39b6875c1f4ec87adebbbabf5b69acee824"
            "43abdc06c5b21ddd7637a039d3227f854dde8eb3ecb"
        )
        datagram = decode_datagram(raw, EKEY)
        active = [parse_channel_one(packet) for packet in datagram.packets if packet.channel == 1]
        active = [packet for packet in active if isinstance(packet, CommUDPActive)]
        self.assertEqual([packet.game_manager.message_type for packet in active], [0x02, 0x03, 0x03])
        self.assertEqual(datagram.encode(EKEY), raw)

    def test_rebroadcaster_binds_a_ticket_to_its_exact_participant(self) -> None:
        identities = IdentityStore(token_factory=lambda: "token.")
        identity, _ = identities.login("Driver")
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        game = games.create(identity)
        participant = game.participants[identity.user_id]
        ticket = games.ticket(game, participant)
        addr = ("192.0.2.10", 1042)
        service = CarbonRebroadcasterService(games)
        service.handle_datagram(self.ticket_confirmation(ticket), addr)

        binding = service.binding(addr)
        self.assertIsNotNone(binding)
        self.assertIs(binding.game, game)
        self.assertIs(binding.participant, participant)
        self.assertEqual(service.stats().endpoints_bound, 1)

    def test_new_type1_on_reused_udp_address_resets_room_transport(self) -> None:
        identities = IdentityStore(token_factory=lambda: "token.")
        identity, _ = identities.login("Driver")
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        game = games.create(identity)
        participant = game.participants[identity.user_id]
        ticket = games.ticket(game, participant)
        addr = ("192.0.2.10", 1042)
        service = CarbonRebroadcasterService(games)
        service.handle_datagram(self.ticket_confirmation(ticket), addr)
        self.assertIsNotNone(service.binding(addr))

        reconnect = TunnelDatagram(
            0,
            (
                TunnelPacket(7, bytes.fromhex("000000071000")),
                TunnelPacket(1, bytes.fromhex("00000001dcf7b035")),
            ),
        ).encode(EKEY)
        offer = service.handle_datagram(reconnect, addr)
        self.assertEqual(len(offer), 1)
        self.assertIsNone(service.binding(addr))

        service.handle_datagram(self.ticket_confirmation(ticket), addr)
        rebound = service.binding(addr)
        self.assertIsNotNone(rebound)
        self.assertEqual(rebound.game.gid, game.gid)

    def test_type3_disconnect_retires_host_game(self) -> None:
        identities = IdentityStore(token_factory=lambda: "token.")
        identity, _ = identities.login("Driver")
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        game = games.create(identity)
        participant = game.participants[identity.user_id]
        addr = ("192.0.2.10", 1042)
        service = CarbonRebroadcasterService(games)
        service.handle_datagram(
            self.ticket_confirmation(games.ticket(game, participant)),
            addr,
        )

        disconnect = TunnelDatagram(
            30,
            (TunnelPacket(1, bytes.fromhex("00000003dcf7b035")),),
        ).encode(EKEY)
        self.assertEqual(service.handle_datagram(disconnect, addr), [])
        self.assertIsNone(service.binding(addr))
        self.assertIsNone(games.get(game.gid))

    def test_rebroadcaster_pairs_only_participants_from_the_same_game(self) -> None:
        identities = IdentityStore(token_factory=lambda: "token.")
        host, _ = identities.login("Host")
        guest, _ = identities.login("Guest")
        outsider, _ = identities.login("Outsider")
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        game = games.create(host)
        guest_participant = games.enter(game.gid, guest)
        other_game = games.create(outsider)
        host_participant = game.participants[host.user_id]
        outsider_participant = other_game.participants[outsider.user_id]
        service = CarbonRebroadcasterService(games)
        host_addr = ("192.0.2.10", 1042)
        guest_addr = ("192.0.2.11", 55277)
        outsider_addr = ("192.0.2.12", 1042)

        service.handle_datagram(self.ticket_confirmation(games.ticket(game, host_participant)), host_addr)
        service.handle_datagram(self.ticket_confirmation(games.ticket(game, guest_participant)), guest_addr)
        service.handle_datagram(
            self.ticket_confirmation(games.ticket(other_game, outsider_participant)),
            outsider_addr,
        )

        self.assertEqual(service.session_endpoints(game.gid), (host_addr, guest_addr))
        self.assertEqual(service.peers(host_addr), (guest_addr,))
        self.assertEqual(service.peers(guest_addr), (host_addr,))
        self.assertEqual(service.peers(outsider_addr), ())
