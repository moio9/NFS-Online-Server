"""GameManager bootstrap and reciprocal 0x0185 publication tests."""

import struct
import unittest
from unittest.mock import patch

from carbon.accounts.identity import IdentityStore
from carbon.core.config import Endpoint
from carbon.gamemanager.player_codec import decode_player_data
from carbon.gamemanager.race_session import (
    RaceSessionCodecError,
    decode_session_attributes,
    descriptor,
    descriptor_bundle,
    logical_type,
    named_state,
    session_attributes,
    start_timer,
)
from carbon.gamemanager.protocol import OLMessageType
from carbon.gamemanager.race_state import (
    GameRaceState,
    RacePhase,
    RoomAccess,
)
from carbon.gamemanager.session_codec import (
    encode_active,
    encode_empty_active_ack,
    encode_host_hello,
)
from carbon.gamemanager.session_object import (
    first_block_identity,
    is_capture_complete,
    iter_session_object_blocks,
    rewrite_for_receiver,
)
from carbon.rebroadcaster.service import (
    CarbonRebroadcasterService,
    EndpointWireState,
    ReadyEpoch,
    ReadyStage,
)
from carbon.theater.directory import CarbonGameDirectory
from carbon.transport.commudp import (
    CommUDPActive,
    game_manager_body,
    parse_channel_one,
)
from carbon.transport.prototunnel import (
    TunnelDatagram,
    TunnelPacket,
    decode_datagram,
)


EKEY = b"9181081919"


def _control(kind: int, connection_id: int) -> bytes:
    return int(kind).to_bytes(4, "big") + int(connection_id).to_bytes(4, "big")


def _client_identity(state: int, kind: int) -> bytes:
    return bytes.fromhex("00000007") + bytes((state, kind))


def _ticket_active(ticket: str) -> bytes:
    encoded = ticket.encode("ascii")
    body = bytes.fromhex("01808004800000") + bytes((len(encoded),)) + encoded
    footer = bytes.fromhex("010203040102030400000000")
    return bytes.fromhex("00000100000000ff") + body + footer + b"\x44"


def _active_messages(raw: bytes) -> list[CommUDPActive]:
    datagram = decode_datagram(raw, EKEY)
    result = []
    for packet in datagram.packets:
        parsed = parse_channel_one(packet)
        if isinstance(parsed, CommUDPActive):
            result.append(parsed)
    return result


def _aggregate_logical_records(active: CommUDPActive) -> tuple[bytes, ...]:
    """Unpack newest-to-oldest records from one CommUDP aggregate."""
    history_count = (int(active.sequence) >> 28) & 0x0F
    remaining = bytes(active.payload[8:])
    appended_oldest_first: list[bytes] = []
    for _ in range(history_count):
        if not remaining:
            raise AssertionError("truncated CommUDP aggregate")
        length = remaining[-1]
        if length <= 0 or len(remaining) < length + 1:
            raise AssertionError("invalid CommUDP aggregate record length")
        record = remaining[-1 - length:-1]
        appended_oldest_first.append(record)
        remaining = remaining[:-1 - length]
    records = (remaining, *reversed(appended_oldest_first))
    for record in records:
        if not record or record[-1] != 0x04:
            raise AssertionError("expected per-record NetGameLink 0x04 trailer")
    return tuple(record[:-1] for record in records)


def _session_block(player_id: int, name: str, offset: int) -> bytes:
    size = 96 if offset == 0 else 48
    body = bytearray(size)
    body[:5] = bytes.fromhex("000000001e")
    body[5:9] = (1).to_bytes(4, "big")
    body[9:13] = (0x454).to_bytes(4, "big")
    body[13:17] = int(offset).to_bytes(4, "big")
    body[17:21] = (0x1E4).to_bytes(4, "big")
    body[21:23] = min(size - 23, 0x1E4).to_bytes(2, "big")
    if offset == 0:
        body[31:35] = int(player_id).to_bytes(4, "big")
        body[39:43] = (0xFFFFFFFF).to_bytes(4, "big")
        encoded = name.encode("ascii")
        body[43:47] = bytes.fromhex("22012b18")
        body[48] = len(encoded)
        body[49:49 + len(encoded)] = encoded
    return bytes(body)


def _native_session_block(player_id: int, name: str, offset: int) -> bytes:
    chunk_size = min(0x1E4, 0x454 - int(offset))
    body = bytearray(23 + chunk_size)
    body[:5] = bytes.fromhex("000000001e")
    body[5:9] = (1).to_bytes(4, "big")
    body[9:13] = (0x454).to_bytes(4, "big")
    body[13:17] = int(offset).to_bytes(4, "big")
    body[17:21] = (0x1E4).to_bytes(4, "big")
    body[21:23] = chunk_size.to_bytes(2, "big")
    if offset == 0:
        body[31:35] = int(player_id).to_bytes(4, "big")
        body[39:43] = (0xFFFFFFFF).to_bytes(4, "big")
        encoded = name.encode("ascii")
        body[43:47] = bytes.fromhex("22012b18")
        body[48] = len(encoded)
        body[49:49 + len(encoded)] = encoded
    return bytes(body)


class CarbonSessionCodecTests(unittest.TestCase):
    def test_descriptor_encodes_only_explicit_runtime_fields_in_both_native_copies(self) -> None:
        capture_clock = struct.unpack(">f", bytes.fromhex("4060a0a7"))[0]
        body = descriptor(0x64, capture_clock, room_tick_ms=0x12345678)
        self.assertEqual(
            body.hex(),
            "000000000022012b18000000644060a0a70000000012345678",
        )
        bundled = descriptor_bundle(
            0x64,
            capture_clock,
            room_tick_ms=0x12345678,
        )
        self.assertEqual(bundled.count(body), 1)

    def test_descriptor_rejects_missing_or_zero_room_tick(self) -> None:
        with self.assertRaises(TypeError):
            descriptor(0x64, 1.0)  # type: ignore[call-arg]
        with self.assertRaises(RaceSessionCodecError):
            descriptor(0x64, 1.0, room_tick_ms=0)

    def test_descriptor_accepts_room_stable_server_tick(self) -> None:
        body = descriptor(0xC8, 12.39546, room_tick_ms=0x27FC21B5)
        self.assertEqual(int.from_bytes(body[21:25], "big"), 0x27FC21B5)
        bundled = descriptor_bundle(
            0xC8,
            12.39546,
            room_tick_ms=0x27FC21B5,
        )
        self.assertIn(body, bundled)

    def test_server_footer_uses_client_tick_and_server_clock(self) -> None:
        client = bytes.fromhex("00496925004969250000d402")
        with patch(
            "carbon.rebroadcaster.service.time.monotonic",
            return_value=0x27FC9325 / 1000.0,
        ):
            footer = CarbonRebroadcasterService._server_footer_from_client(client)
        self.assertEqual(footer.hex(), "0049692527fc932500000000")

    def test_same_millisecond_real_client_footer_advances_server_tick(self) -> None:
        client = bytes.fromhex("009bf6ea009bf6ea00007202")
        with patch.object(
            CarbonRebroadcasterService,
            "_server_tick_ms",
            return_value=0x009BF6EA,
        ):
            footer = CarbonRebroadcasterService._server_footer_from_client(client)
        self.assertEqual(footer.hex(), "009bf6ea009bf6eb00000000")
        self.assertEqual(
            (int.from_bytes(footer[4:8], "big") - int.from_bytes(footer[:4], "big"))
            & 0xFFFFFFFF,
            1,
        )

    def test_distinct_real_client_footer_is_preserved(self) -> None:
        client = bytes.fromhex("04d91f9404d91f940000d402")
        with patch.object(
            CarbonRebroadcasterService,
            "_server_tick_ms",
            return_value=0x04D91F95,
        ):
            footer = CarbonRebroadcasterService._server_footer_from_client(client)
        self.assertEqual(footer.hex(), "04d91f9404d91f9500000000")

    def test_zero_client_tick_fallback_never_duplicates_server_tick(self) -> None:
        with patch.object(
            CarbonRebroadcasterService,
            "_server_tick_ms",
            return_value=0x002BA274,
        ):
            footer = CarbonRebroadcasterService._server_footer_from_client(
                bytes(12)
            )
        self.assertEqual(
            footer.hex(),
            "002ba273002ba27400000000",
        )

    def test_transport_epoch_footer_keeps_nonzero_clock_delta(self) -> None:
        service = CarbonRebroadcasterService.__new__(CarbonRebroadcasterService)
        addr = ("127.0.0.1", 49653)
        service._wire = {
            addr: EndpointWireState(fallback_client_tick_ms=0x002B6200)
        }
        with patch.object(
            CarbonRebroadcasterService,
            "_server_tick_ms",
            return_value=0x002BA274,
        ):
            footer = service._footer_for(addr, object())  # type: ignore[arg-type]
        self.assertEqual(footer.hex(), "002b6200002ba27400000000")
        self.assertNotEqual(footer[:4], footer[4:8])

    def test_same_millisecond_transport_epoch_is_moved_back_one_tick(self) -> None:
        service = CarbonRebroadcasterService.__new__(CarbonRebroadcasterService)
        addr = ("127.0.0.1", 49653)
        service._wire = {
            addr: EndpointWireState(fallback_client_tick_ms=0x002BA274)
        }
        with patch.object(
            CarbonRebroadcasterService,
            "_server_tick_ms",
            return_value=0x002BA274,
        ):
            footer = service._footer_for(addr, object())  # type: ignore[arg-type]
        self.assertEqual(footer.hex(), "002ba273002ba27400000000")

    def test_host_hello_matches_capture_layout(self) -> None:
        footer = bytes.fromhex("02fdafe00b638b7100000000")
        self.assertEqual(
            encode_host_hello(1, capacity=8, footer=footer).hex(),
            "0182800480808100000000010180000000000000000000000800010004"
            "02fdafe00b638b7100000000400d",
        )
        self.assertEqual(
            encode_empty_active_ack(0x100, 0x100, footer=footer).hex(),
            "000001000000010002fdafe00b638b710000000040",
        )

    def test_challenge_helper_aggregate_matches_frame_2794_byte_for_byte(self) -> None:
        helper_latency = bytes.fromhex("00000000120000005a41e80000")
        host_latency = bytes.fromhex("00000000120000005744000000")
        state7 = bytes.fromhex("000000001c000000000007")
        attributes = bytes.fromhex(
            "000000001d150000183239385f70726f645f7365727665722b3232303132623138"
            "010001300200013203000130040001300500000600013107000132080001320900"
            "01310a00000b0001310c0001310d00000e000557482d45550f0007414253544149"
            "4e10000663732e322e321100074142535441494e1200074142535441494e130007"
            "4142535441494e1400074142535441494e"
        )
        timer = bytes.fromhex("000000001b00000000414a5b2344143989")
        payload = CarbonRebroadcasterService._commudp_aggregate_payload(
            (helper_latency, host_latency, state7, attributes, timer)
        )
        expected_active = bytes.fromhex(
            "4000012100000111"
            "00000000120000005a41e8000004"
            "00000000120000005744000000040e"
            "000000001c000000000007040c"
            "000000001d150000183239385f70726f645f7365727665722b3232303132623138"
            "010001300200013203000130040001300500000600013107000132080001320900"
            "01310a00000b0001310c0001310d00000e000557482d45550f0007414253544149"
            "4e10000663732e322e321100074142535441494e1200074142535441494e130007"
            "4142535441494e1400074142535441494e0496"
            "000000001b00000000414a5b23441439890412"
        )
        self.assertEqual(encode_active(0x40000121, 0x111, payload), expected_active)
        self.assertEqual(len(expected_active), 220)

    def test_server_hosted_hello_matches_ranked_capture_layout(self) -> None:
        footer = bytes.fromhex("009f72a71e1a008800000000")
        self.assertEqual(
            encode_host_hello(
                1,
                capacity=8,
                footer=footer,
                server_hosted=True,
            ).hex(),
            "0182800480808100000000000082000000000000000000000800010004"
            "009f72a71e1a008800000000400d",
        )

    def test_session_object_rewrite_preserves_identity_and_changes_receiver_fields(self) -> None:
        blocks = [
            _session_block(87, "Guest", 0),
            _session_block(87, "Guest", 0x1E4),
            _session_block(87, "Guest", 0x3C8),
        ]
        self.assertTrue(is_capture_complete(blocks))
        rewritten = rewrite_for_receiver(blocks, remote_object_id=2, remote_slot=1)
        self.assertEqual({int.from_bytes(item[5:9], "big") for item in rewritten}, {2})
        self.assertEqual({int.from_bytes(item[13:17], "big") for item in rewritten}, {0, 0x1E4, 0x3C8})
        first = next(item for item in rewritten if int.from_bytes(item[13:17], "big") == 0)
        self.assertEqual(int.from_bytes(first[35:39], "big"), 1)
        self.assertEqual(int.from_bytes(first[39:43], "big"), 1)
        self.assertEqual(first_block_identity(rewritten), (2, 87, "Guest"))

    def test_embedded_final_session_chunk_is_extracted_from_active_game_compound(self) -> None:
        compound = (
            bytes.fromhex(
                "000000001c0006706c617965720000000d002dd831002dd8320001840244"
                "000000001c0006706c61796572000000050412"
            )
            + _session_block(87, "Guest", 0x3C8)
        )
        blocks = tuple(iter_session_object_blocks(compound))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].object_id, 1)
        self.assertEqual(blocks[0].total_size, 0x454)
        self.assertEqual(blocks[0].offset, 0x3C8)
        self.assertEqual(blocks[0].raw, _session_block(87, "Guest", 0x3C8))


class CarbonGameManagerFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identities = IdentityStore(token_factory=lambda: "token.")
        self.host, _ = self.identities.login("Host")
        self.guest, _ = self.identities.login("Guest")
        self.games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        self.game = self.games.create(self.host, {"MAX-PLAYERS": "8"})
        self.host_participant = self.game.participants[self.host.user_id]
        self.guest_participant = self.games.enter(
            self.game.gid,
            self.guest,
            internal_ip="192.168.1.11",
            internal_port=55277,
        )
        assert self.guest_participant is not None
        self.service = CarbonRebroadcasterService(self.games)
        self.host_addr = ("192.0.2.10", 1042)
        self.guest_addr = ("192.0.2.11", 55277)

    def _bind_host(self) -> list[tuple[bytes, tuple[str, int]]]:
        connection_id = 0xDCF7B035
        first = TunnelDatagram(
            0,
            (
                TunnelPacket(7, _client_identity(0x10, 0)),
                TunnelPacket(1, _control(1, connection_id)),
            ),
        ).encode(EKEY)
        connect_replies = self.service.handle_datagram(first, self.host_addr)
        self.assertEqual(len(connect_replies), 1)
        self.assertEqual(decode_datagram(connect_replies[0][0], EKEY).offset_words, 5)

        ticket = self.games.ticket(self.game, self.host_participant)
        confirmation = TunnelDatagram(
            5,
            (
                TunnelPacket(1, _control(2, connection_id)),
                TunnelPacket(1, bytes.fromhex("00000100000000ff")),
                TunnelPacket(1, _ticket_active(ticket)),
            ),
        ).encode(EKEY)
        replies = self.service.handle_datagram(confirmation, self.host_addr)
        barrier_ack = TunnelDatagram(
            21,
            (TunnelPacket(1, bytes.fromhex("0000010100000100")),),
        ).encode(EKEY)
        replies.extend(self.service.handle_datagram(barrier_ack, self.host_addr))
        return replies

    def _bind_guest(self) -> list[tuple[bytes, tuple[str, int]]]:
        connection_id = 0xDCF7B063
        if self.game.server_hosted:
            # rankedcrash frames 668-671: a later dedicated participant uses
            # one client Type1 leg, receives the combined Type2+Type1 reply,
            # then sends the ticket at RC4 word offset 5.
            first = TunnelDatagram(
                0,
                (
                    TunnelPacket(7, _client_identity(0x10, 0)),
                    TunnelPacket(1, _control(1, connection_id)),
                ),
            ).encode(EKEY)
            reply = self.service.handle_datagram(first, self.guest_addr)
            self.assertEqual(decode_datagram(reply[0][0], EKEY).offset_words, 5)
            confirmation_offset = 5
            barrier_offset = 21
        else:
            first = TunnelDatagram(
                0,
                (
                    TunnelPacket(7, _client_identity(0x37, 0)),
                    TunnelPacket(1, _control(1, connection_id)),
                ),
            ).encode(EKEY)
            offer = self.service.handle_datagram(first, self.guest_addr)
            self.assertEqual(decode_datagram(offer[0][0], EKEY).offset_words, 0)

            second = TunnelDatagram(
                5,
                (
                    TunnelPacket(7, _client_identity(0x37, 1)),
                    TunnelPacket(1, _control(2, connection_id)),
                    TunnelPacket(1, _control(1, connection_id)),
                ),
            ).encode(EKEY)
            reply = self.service.handle_datagram(second, self.guest_addr)
            self.assertEqual(decode_datagram(reply[0][0], EKEY).offset_words, 5)
            confirmation_offset = 12
            barrier_offset = 23

        ticket = self.games.ticket(self.game, self.guest_participant)
        confirmation = TunnelDatagram(
            confirmation_offset,
            (
                TunnelPacket(1, _control(2, connection_id)),
                TunnelPacket(1, bytes.fromhex("00000100000000ff")),
                TunnelPacket(1, _ticket_active(ticket)),
            ),
        ).encode(EKEY)
        replies = self.service.handle_datagram(confirmation, self.guest_addr)
        barrier_ack = TunnelDatagram(
            barrier_offset,
            (TunnelPacket(1, bytes.fromhex("0000010100000100")),),
        ).encode(EKEY)
        replies.extend(self.service.handle_datagram(barrier_ack, self.guest_addr))
        return replies

    def test_host_ticket_emits_ack_then_hosthello_and_local_roster(self) -> None:
        replies = self._bind_host()
        self.assertEqual([decode_datagram(raw, EKEY).offset_words for raw, _ in replies], [12, 18])
        bootstrap = _active_messages(replies[1][0])
        self.assertEqual([item.game_manager.message_type for item in bootstrap], [0x02, 0x03])
        roster = bootstrap[1].game_manager.body
        player, consumed = decode_player_data(roster, 2)
        self.assertEqual(player.player_id, 1)
        self.assertEqual(player.name, "Host")
        self.assertEqual(player.state, 3)
        self.assertEqual(consumed, len(roster))

    def test_guest_bootstrap_stops_wire_retry_then_waits_for_player_publish(self) -> None:
        replies = self._bind_guest()
        window = next(
            item
            for item in self.service.confirmations.pending(self.guest_addr)
            if item.label == "session-host-bootstrap"
        )
        self.assertTrue(window.application_confirmation)
        self.assertIn((window.records[0], self.guest_addr), replies)
        wire = self.service._wire[self.guest_addr]
        transport_state = (
            wire.next_server_sequence,
            wire.next_offset_words,
        )

        self.service.confirmations.acknowledge(
            self.guest_addr,
            window.final_sequence,
        )
        retried = self.service.confirmations.poll(
            now=window.retry.retry_not_before,
        )

        self.assertEqual(retried, [])
        self.assertTrue(window.transport_acknowledged)
        self.assertEqual(window.retry.retries_sent, 0)
        self.assertEqual(
            (wire.next_server_sequence, wire.next_offset_words),
            transport_state,
        )
        publish = TunnelDatagram(
            28,
            (
                TunnelPacket(
                    1,
                    bytes.fromhex("0000010100000103")
                    + bytes.fromhex("018404"),
                ),
            ),
        ).encode(EKEY)
        self.service.handle_datagram(publish, self.guest_addr)
        self.assertFalse(any(
            item.label == "session-host-bootstrap"
            for item in self.service.confirmations.pending(self.guest_addr)
        ))

    def test_ranked_dedicated_first_player_gets_server_hosted_bootstrap(self) -> None:
        self.game = self.games.create(
            self.host,
            {"B-U-matchmaking_state": "1", "B-U-game_type": "0"},
            server_hosted=True,
        )
        participant = self.games.enter(
            self.game.gid,
            self.host,
            internal_ip="192.168.1.10",
            internal_port=1042,
        )
        self.assertIsNotNone(participant)
        assert participant is not None
        self.host_participant = participant
        self.service = CarbonRebroadcasterService(self.games)

        replies = self._bind_host()
        bootstrap = _active_messages(replies[1][0])
        hello = game_manager_body(bootstrap[0].payload)
        self.assertTrue(hello.startswith(bytes.fromhex(
            "0182800480808100000000000082000000000000000000000800010004"
        )))
        self.assertEqual(self.game.host.persona, "nfsdevserver")
        self.assertNotEqual(self.game.host.user_id, self.host.user_id)

    def test_ranked_dedicated_joiner_roster_keeps_existing_player_first(self) -> None:
        self.game = self.games.create(
            self.host,
            {"B-U-matchmaking_state": "1", "B-U-game_type": "0"},
            server_hosted=True,
        )
        first = self.games.enter(
            self.game.gid,
            self.host,
            internal_ip="192.168.1.10",
            internal_port=1042,
        )
        second = self.games.enter(
            self.game.gid,
            self.guest,
            internal_ip="192.168.1.11",
            internal_port=55277,
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.host_participant = first
        self.guest_participant = second
        self.service = CarbonRebroadcasterService(self.games)

        self._bind_host()
        replies = self._bind_guest()
        bootstrap = _active_messages(replies[1][0])
        decoded = [
            decode_player_data(item.game_manager.body, 2)[0]
            for item in bootstrap
            if item.game_manager is not None and item.game_manager.message_type == 0x03
        ]
        self.assertEqual([item.player_id for item in decoded], [1, 2])
        self.assertEqual([item.state for item in decoded], [6, 3])

        request = TunnelDatagram(
            28,
            (
                TunnelPacket(
                    1,
                    bytes.fromhex("0000010100000103") + bytes.fromhex("018404"),
                ),
            ),
        ).encode(EKEY)
        publication = self.service.handle_datagram(request, self.guest_addr)
        descriptor_bodies = [
            game_manager_body(active.payload)
            for raw, target in publication
            if target == self.guest_addr
            for active in _active_messages(raw)
            if game_manager_body(active.payload).startswith(bytes.fromhex("000000000022"))
        ]
        self.assertEqual(len(descriptor_bodies), 1)
        self.assertEqual(
            int.from_bytes(descriptor_bodies[0][9:13], "big"),
            self.game.descriptor_handle_base + 10,
        )
        attribute_bodies = [
            game_manager_body(active.payload)
            for raw, target in publication
            if target == self.guest_addr
            for active in _active_messages(raw)
            if game_manager_body(active.payload).startswith(bytes.fromhex("000000001d15"))
        ]
        self.assertEqual(len(attribute_bodies), 1)
        attributes = decode_session_attributes(attribute_bodies[0])
        self.assertEqual(attributes["game_type"], "0")
        self.assertEqual(attributes["matchmaking_state"], "1")

    def test_two_player_unranked_quickjoin_roster_keeps_joiner_local_first(self) -> None:
        player_ids = {
            self.host.user_id: 22305,
            self.guest.user_id: 5371,
        }
        self.games = CarbonGameDirectory(
            Endpoint("127.0.0.1", 19118),
            player_id_resolver=lambda identity: player_ids[identity.user_id],
        )
        self.game = self.games.create(
            self.host,
            {"B-U-matchmaking_state": "1", "B-U-game_type": "1"},
            server_hosted=True,
        )
        first = self.games.enter(
            self.game.gid,
            self.host,
            internal_ip="192.168.1.150",
            internal_port=39958,
        )
        entering = self.games.enter(
            self.game.gid,
            self.guest,
            internal_ip="192.168.1.150",
            internal_port=1042,
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(entering)
        assert first is not None and entering is not None
        self.host_participant = first
        self.guest_participant = entering
        self.service = CarbonRebroadcasterService(self.games)

        self._bind_host()
        replies = self._bind_guest()
        bootstrap = _active_messages(replies[1][0])
        roster = [
            decode_player_data(item.game_manager.body, 2)[0]
            for item in bootstrap
            if item.game_manager is not None and item.game_manager.message_type == 0x03
        ]
        self.assertEqual(
            [item.player_id for item in roster],
            [entering.player_id, first.player_id],
        )
        self.assertEqual([item.state for item in roster], [3, 6])

    def test_third_dedicated_roster_uses_room_slots_not_numeric_pid_order(self) -> None:
        third, _ = self.identities.login("Third")
        player_ids = {
            self.host.user_id: 31094,
            self.guest.user_id: 17954,
            third.user_id: 3305,
        }
        games = CarbonGameDirectory(
            Endpoint("127.0.0.1", 19118),
            player_id_resolver=lambda identity: player_ids[identity.user_id],
        )
        game = games.create(
            self.host,
            {"B-U-matchmaking_state": "1", "B-U-game_type": "1"},
            server_hosted=True,
        )
        participants = [
            games.enter(
                game.gid,
                identity,
                internal_ip=f"192.168.1.{150 - index * 25}",
                internal_port=1042 + index,
            )
            for index, identity in enumerate((self.host, self.guest, third))
        ]
        self.assertTrue(all(participant is not None for participant in participants))
        first, second, entering = participants
        assert first is not None and second is not None and entering is not None

        service = CarbonRebroadcasterService(games)
        addresses = (
            ("192.0.2.10", 1042),
            ("192.0.2.11", 1043),
            ("192.0.2.12", 1044),
        )
        for address, participant in zip(addresses, (first, second, entering)):
            resolution = games.resolve_ticket(games.ticket(game, participant))
            self.assertIsNotNone(resolution)
            assert resolution is not None
            service._wire[address] = EndpointWireState(
                bootstrap_sent=True,
                bootstrap_acknowledgement=0x100,
            )
            self.assertTrue(service._bind(address, resolution))

        entering_resolution = games.resolve_ticket(games.ticket(game, entering))
        self.assertIsNotNone(entering_resolution)
        assert entering_resolution is not None
        replies: list[tuple[bytes, tuple[str, int]]] = []
        service._append_bootstrap(replies, addresses[2], entering_resolution)

        roster = [
            decode_player_data(active.game_manager.body, 2)[0]
            for active in _active_messages(replies[0][0])
            if active.game_manager is not None
            and active.game_manager.message_type == 0x03
        ]
        self.assertEqual(
            [player.player_id for player in roster],
            [31094, 17954, 3305],
        )
        self.assertEqual([player.state for player in roster], [6, 6, 3])

    def test_dedicated_invite_join_uses_capture_correct_roster_and_self_join(self) -> None:
        self.game = self.games.create(
            self.host,
            {
                "players.0.props.{sessionType}": "resetServer",
                "players.0.props.{filter-game_type}": "2",
                "players.0.props.{filter-matchmaking_state}": "0",
            },
            server_hosted=True,
        )
        first = self.games.enter(
            self.game.gid,
            self.host,
            internal_ip="192.168.1.10",
            internal_port=1042,
        )
        self.assertIsNotNone(first)
        assert first is not None
        second = self.games.enter(
            self.game.gid,
            self.guest,
            internal_ip="192.168.1.11",
            internal_port=55277,
            invite_remote_player_id=first.player_id,
            invite_entry=True,
        )
        self.assertIsNotNone(second)
        assert second is not None
        self.host_participant = first
        self.guest_participant = second
        self.service = CarbonRebroadcasterService(self.games)

        self._bind_host()
        replies = self._bind_guest()
        bootstrap = _active_messages(replies[1][0])
        roster = [
            decode_player_data(item.game_manager.body, 2)[0]
            for item in bootstrap
            if item.game_manager is not None and item.game_manager.message_type == 0x03
        ]
        self.assertEqual([item.player_id for item in roster], [first.player_id, second.player_id])
        self.assertEqual([item.state for item in roster], [6, 3])

        request = TunnelDatagram(
            28,
            (
                TunnelPacket(
                    1,
                    bytes.fromhex("0000010100000103") + bytes.fromhex("018404"),
                ),
            ),
        ).encode(EKEY)
        publication = self.service.handle_datagram(request, self.guest_addr)

        joined_by_target: dict[tuple[str, int], list] = {}
        for raw, target in publication:
            for active in _active_messages(raw):
                if active.game_manager is None or active.game_manager.message_type != 0x05:
                    continue
                body = active.game_manager.body
                player, consumed = decode_player_data(body, 4)
                self.assertEqual(consumed, len(body))
                joined_by_target.setdefault(target, []).append(player)

        self.assertEqual(
            [item.player_id for item in joined_by_target[self.guest_addr]],
            [second.player_id],
        )
        self.assertEqual(joined_by_target[self.guest_addr][0].state, 6)
        self.assertEqual(
            [item.player_id for item in joined_by_target[self.host_addr]],
            [second.player_id],
        )
        self.assertEqual(joined_by_target[self.host_addr][0].state, 6)

    def test_invite_host_traffic_cannot_shift_guest_bootstrap_window(self) -> None:
        self.game = self.games.create(
            self.host,
            {
                "players.0.props.{sessionType}": "resetServer",
                "players.0.props.{filter-game_type}": "2",
                "players.0.props.{filter-matchmaking_state}": "0",
            },
            server_hosted=True,
        )
        first = self.games.enter(
            self.game.gid,
            self.host,
            internal_ip="192.168.1.10",
            internal_port=1042,
        )
        self.assertIsNotNone(first)
        assert first is not None
        second = self.games.enter(
            self.game.gid,
            self.guest,
            internal_ip="192.168.1.11",
            internal_port=55277,
            invite_remote_player_id=first.player_id,
            invite_entry=True,
        )
        self.assertIsNotNone(second)
        assert second is not None
        self.host_participant = first
        self.guest_participant = second
        self.service = CarbonRebroadcasterService(self.games)

        self._bind_host()
        resolution = self.games.resolve_ticket(self.games.ticket(self.game, second))
        self.assertIsNotNone(resolution)
        assert resolution is not None
        guest_wire = EndpointWireState(
            bootstrap_pending=True,
            bootstrap_acknowledgement=0x100,
        )
        self.service._wire[self.guest_addr] = guest_wire
        self.assertTrue(self.service._bind(self.guest_addr, resolution))

        # The live failure had this host LatencyInfo take sequence 0x101 in
        # the ticket-ACK -> HostHello gap.  Retail leaves that sequence for
        # HostHello, so all room traffic is held until the invite session is
        # application-confirmed.
        replies: list[tuple[bytes, tuple[str, int]]] = []
        host_binding = self.service._bindings[self.host_addr]
        self.service.gameplay_relay.relay_logical_to_peers(
            replies,
            self.host_addr,
            host_binding,
            bytes.fromhex("00000000120000000100000000"),
            footer=False,
            confirmation="session-latency-info",
        )
        self.assertFalse(any(target == self.guest_addr for _raw, target in replies))
        self.assertEqual(guest_wire.next_server_sequence, 0x101)
        self.assertFalse(any(
            item.label == "session-latency-info"
            for item in self.service.confirmations.pending(self.guest_addr)
        ))

        bootstrap_replies: list[tuple[bytes, tuple[str, int]]] = []
        self.service._append_bootstrap(
            bootstrap_replies,
            self.guest_addr,
            resolution,
        )
        bootstrap = _active_messages(bootstrap_replies[0][0])
        self.assertEqual(
            [item.sequence & 0x0FFFFFFF for item in bootstrap],
            [0x101, 0x102, 0x103],
        )

    def test_late_coop_joiner_receives_cached_host_challenge_attributes(self) -> None:
        self.game = self.games.create(
            self.host,
            {
                "players.0.props.{sessionType}": "resetServer",
                "players.0.props.{filter-game_type}": "2",
                "players.0.props.{filter-matchmaking_state}": "0",
                "players.0.props.{pref-help_type}": "2",
            },
            server_hosted=True,
        )
        first = self.games.enter(
            self.game.gid,
            self.host,
            internal_ip="192.168.1.10",
            internal_port=1042,
        )
        self.games.challenge_quick_join_after_ready = True
        self.games.set_challenge_ready(
            self.game.gid,
            True,
            reason="test-ready",
        )
        second = self.games.enter(
            self.game.gid,
            self.guest,
            internal_ip="192.168.1.11",
            internal_port=55277,
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.host_participant = first
        self.guest_participant = second
        self.service = CarbonRebroadcasterService(self.games)

        self._bind_host()
        self.service._wire[self.host_addr].session_confirmed = True
        challenge = session_attributes(
            {
                "B-U-game_type": "2",
                "B-U-matchmaking_state": "0",
                "B-U-help_type": "0",
                "B-U-game_mode": "0",
                "B-U-car_tier": "1",
                "B-U-max_online_player": "2",
                "B-U-race_type_sprint": "cs.2.1",
                "B-U-race_type_circuit": "ABSTAIN",
                "B-U-race_type_canyon_due": "ABSTAIN",
                "B-U-race_type_speedtrap": "ABSTAIN",
                "B-U-race_type_knockout": "ABSTAIN",
                "B-U-race_type_pursuit_tag": "ABSTAIN",
            }
        )
        publication = TunnelDatagram(
            28,
            (
                TunnelPacket(
                    1,
                    bytes.fromhex("0000010200000102") + challenge + b"\x04",
                ),
            ),
        ).encode(EKEY)
        host_replies = self.service.handle_datagram(publication, self.host_addr)

        host_properties = [
            game_manager_body(active.payload)
            for raw, target in host_replies
            if target == self.host_addr
            for active in _active_messages(raw)
            if active.game_manager is not None
            and active.game_manager.message_type == 0x0C
        ]
        self.assertEqual(host_properties, [bytes.fromhex("018c000101808100000002")])

        self.assertEqual(self.game.properties["B-U-game_type"], "2")
        self.assertEqual(self.game.properties["B-U-help_type"], "0")
        self.assertEqual(self.game.properties["B-U-game_mode"], "0")
        self.assertEqual(self.game.properties["B-U-car_tier"], "1")
        self.assertEqual(self.game.properties["B-U-race_type_sprint"], "cs.2.1")

        self._bind_guest()
        request = TunnelDatagram(
            28,
            (
                TunnelPacket(
                    1,
                    bytes.fromhex("0000010100000103") + bytes.fromhex("018404"),
                ),
            ),
        ).encode(EKEY)
        replies = self.service.handle_datagram(request, self.guest_addr)
        attribute_bodies = [
            game_manager_body(active.payload)
            for raw, target in replies
            if target == self.guest_addr
            for active in _active_messages(raw)
            if game_manager_body(active.payload).startswith(bytes.fromhex("000000001d15"))
        ]
        self.assertEqual(len(attribute_bodies), 1)
        decoded = decode_session_attributes(attribute_bodies[0])
        self.assertEqual(decoded["game_type"], "2")
        self.assertEqual(decoded["help_type"], "0")
        self.assertEqual(decoded["game_mode"], "0")
        self.assertEqual(decoded["car_tier"], "1")
        self.assertEqual(decoded["race_type_sprint"], "cs.2.1")


    def test_challenge_allocation_normalizes_transient_unranked_host_snapshot(self) -> None:
        self.game = self.games.create(
            self.host,
            {
                "players.0.props.{sessionType}": "resetServer",
                "players.0.props.{filter-game_type}": "2",
                "players.0.props.{filter-matchmaking_state}": "0",
                "players.0.props.{pref-help_type}": "2",
            },
            server_hosted=True,
        )
        participant = self.games.enter(
            self.game.gid,
            self.host,
            internal_ip="192.168.1.10",
            internal_port=1042,
        )
        self.assertIsNotNone(participant)
        assert participant is not None
        self.host_participant = participant
        self.service = CarbonRebroadcasterService(self.games)
        self._bind_host()

        # Carbon can briefly publish its previous local Unranked room while the
        # dedicated Challenge event is still resolving. A normal mu.* event is
        # not a valid Challenge identity and must not become authoritative.
        transient_unranked = session_attributes(
            {
                "B-U-game_type": "1",
                "B-U-matchmaking_state": "1",
                "B-U-help_type": "2",
                "B-U-game_mode": "5",
                "B-U-max_online_player": "8",
                "B-U-skill": "999",
                "B-U-player_dnf": "1",
                "B-U-team_play": "0",
                "B-U-car_tier": "3",
                "B-U-length": "2",
                "B-U-n2o": "0",
                "B-U-collision_detection": "0",
                "B-U-track": "mu.2.2",
                "B-U-race_type_sprint": "ABSTAIN",
                "B-U-race_type_circuit": "ABSTAIN",
                "B-U-race_type_canyon_due": "ABSTAIN",
                "B-U-race_type_speedtrap": "mu.2.2",
                "B-U-race_type_knockout": "ABSTAIN",
                "B-U-race_type_pursuit_tag": "ABSTAIN",
            }
        )
        request = TunnelDatagram(
            28,
            (
                TunnelPacket(
                    1,
                    bytes.fromhex("0000010200000102") + transient_unranked + b"\x04",
                ),
            ),
        ).encode(EKEY)
        self.service.handle_datagram(request, self.host_addr)

        normalized = decode_session_attributes(
            self.service._race[self.game.gid].attributes
        )
        self.assertEqual(normalized["game_type"], "2")
        self.assertEqual(normalized["matchmaking_state"], "0")
        self.assertEqual(normalized["help_type"], "0")
        self.assertEqual(normalized["game_mode"], "0")
        self.assertEqual(normalized["max_online_player"], "2")
        self.assertEqual(normalized["skill"], "")
        self.assertEqual(normalized["player_dnf"], "")
        self.assertEqual(normalized["team_play"], "1")
        self.assertEqual(normalized["car_tier"], "1")
        self.assertEqual(normalized["length"], "1")
        self.assertEqual(normalized["track"], "")
        self.assertEqual(normalized["n2o"], "1")
        self.assertEqual(normalized["collision_detection"], "1")
        self.assertEqual(normalized["race_type_sprint"], "ABSTAIN")
        self.assertEqual(normalized["race_type_speedtrap"], "ABSTAIN")
        self.assertEqual(
            self.service._race[self.game.gid].challenge_event_identity,
            {},
        )
        self.assertEqual(self.game.properties["B-U-game_type"], "2")
        self.assertEqual(self.game.properties["B-U-help_type"], "0")
        self.assertEqual(self.game.properties["B-U-game_mode"], "0")
        self.assertEqual(self.game.properties["B-U-race_type_speedtrap"], "ABSTAIN")

        valid_challenge = session_attributes(
            {
                "B-U-game_type": "2",
                "B-U-matchmaking_state": "0",
                "B-U-help_type": "0",
                "B-U-game_mode": "5",
                "B-U-max_online_player": "2",
                "B-U-car_tier": "3",
                "B-U-length": "2",
                "B-U-n2o": "0",
                "B-U-collision_detection": "0",
                "B-U-race_type_sprint": "ABSTAIN",
                "B-U-race_type_circuit": "ABSTAIN",
                "B-U-race_type_canyon_due": "ABSTAIN",
                "B-U-race_type_speedtrap": "cs.11.2",
                "B-U-race_type_knockout": "ABSTAIN",
                "B-U-race_type_pursuit_tag": "ABSTAIN",
            }
        )
        request = TunnelDatagram(
            28,
            (
                TunnelPacket(
                    1,
                    bytes.fromhex("0000010300000102")
                    + valid_challenge
                    + b"\x04",
                ),
            ),
        ).encode(EKEY)
        self.service.handle_datagram(request, self.host_addr)

        captured = decode_session_attributes(
            self.service._race[self.game.gid].attributes
        )
        self.assertEqual(captured["game_type"], "2")
        self.assertEqual(captured["game_mode"], "5")
        self.assertEqual(captured["car_tier"], "3")
        self.assertEqual(captured["length"], "2")
        self.assertEqual(captured["race_type_sprint"], "ABSTAIN")
        self.assertEqual(captured["race_type_speedtrap"], "cs.11.2")
        self.assertEqual(self.game.properties["B-U-game_mode"], "5")
        self.assertEqual(
            self.game.properties["B-U-race_type_speedtrap"],
            "cs.11.2",
        )

    def test_challenge_freezes_join_snapshot_then_accepts_committed_settings(self) -> None:
        self.game = self.games.create(
            self.host,
            {
                "players.0.props.{sessionType}": "resetServer",
                "players.0.props.{filter-game_type}": "2",
                "players.0.props.{filter-matchmaking_state}": "0",
                "players.0.props.{pref-help_type}": "2",
            },
            server_hosted=True,
        )
        participant = self.games.enter(
            self.game.gid,
            self.host,
            internal_ip="192.168.1.10",
            internal_port=1042,
        )
        self.assertIsNotNone(participant)
        assert participant is not None
        self.host_participant = participant
        self.service = CarbonRebroadcasterService(self.games)
        self._bind_host()

        first_event = session_attributes(
            {
                "B-U-game_type": "2",
                "B-U-matchmaking_state": "0",
                "B-U-help_type": "0",
                # Theater and GameManager retain the same concrete pair.
                "B-U-game_mode": "5",
                "B-U-car_tier": "2",
                "B-U-max_online_player": "2",
                "B-U-length": "1",
                "B-U-n2o": "1",
                "B-U-collision_detection": "1",
                "B-U-race_type_sprint": "ABSTAIN",
                "B-U-race_type_circuit": "ABSTAIN",
                "B-U-race_type_canyon_due": "ABSTAIN",
                "B-U-race_type_speedtrap": "cs.11.2",
                "B-U-race_type_knockout": "ABSTAIN",
                "B-U-race_type_pursuit_tag": "ABSTAIN",
            }
        )
        stale_later_event = session_attributes(
            {
                "B-U-game_type": "2",
                "B-U-matchmaking_state": "0",
                "B-U-help_type": "0",
                "B-U-game_mode": "0",
                "B-U-car_tier": "2",
                "B-U-max_online_player": "2",
                "B-U-length": "1",
                "B-U-n2o": "1",
                "B-U-collision_detection": "1",
                "B-U-race_type_sprint": "cs.2.2",
                "B-U-race_type_circuit": "ABSTAIN",
                "B-U-race_type_canyon_due": "ABSTAIN",
                "B-U-race_type_speedtrap": "ABSTAIN",
                "B-U-race_type_knockout": "ABSTAIN",
                "B-U-race_type_pursuit_tag": "ABSTAIN",
            }
        )
        empty_later_event = session_attributes(
            {
                "B-U-game_type": "2",
                "B-U-matchmaking_state": "0",
                "B-U-help_type": "0",
                "B-U-game_mode": "0",
                "B-U-car_tier": "1",
                "B-U-max_online_player": "2",
                "B-U-length": "1",
                "B-U-n2o": "1",
                "B-U-collision_detection": "1",
                "B-U-race_type_sprint": "ABSTAIN",
                "B-U-race_type_circuit": "ABSTAIN",
                "B-U-race_type_canyon_due": "ABSTAIN",
                "B-U-race_type_speedtrap": "ABSTAIN",
                "B-U-race_type_knockout": "ABSTAIN",
                "B-U-race_type_pursuit_tag": "ABSTAIN",
            }
        )

        # A newly bound helper is still inside its allocation window.  The
        # host can publish stale local Challenge data here; keep the first
        # trustworthy event until that helper receives its room commit.
        guest_participant = self.games.enter(
            self.game.gid,
            self.guest,
            internal_ip="192.168.1.11",
            internal_port=55277,
            invite_remote_player_id=self.host_participant.player_id,
            invite_entry=True,
        )
        self.assertIsNotNone(guest_participant)
        assert guest_participant is not None
        self.guest_participant = guest_participant
        self._bind_guest()

        for sequence, attributes in (
            (0x102, first_event),
            (0x103, stale_later_event),
            (0x104, empty_later_event),
        ):
            request = TunnelDatagram(
                28,
                (
                    TunnelPacket(
                        1,
                        sequence.to_bytes(4, "big")
                        + bytes.fromhex("00000102")
                        + attributes
                        + b"\x04",
                    ),
                ),
            ).encode(EKEY)
            self.service.handle_datagram(request, self.host_addr)

        retained = decode_session_attributes(
            self.service._race[self.game.gid].attributes
        )
        self.assertEqual(retained["game_mode"], "5")
        self.assertEqual(retained["car_tier"], "2")
        self.assertEqual(retained["race_type_speedtrap"], "cs.11.2")
        self.assertEqual(retained["race_type_circuit"], "ABSTAIN")
        self.assertEqual(retained["race_type_sprint"], "ABSTAIN")
        self.assertEqual(self.game.properties["B-U-game_mode"], "5")
        self.assertEqual(self.game.properties["B-U-car_tier"], "2")
        self.assertEqual(
            self.game.properties["B-U-race_type_speedtrap"],
            "cs.11.2",
        )
        self.assertEqual(self.game.properties["B-U-race_type_circuit"], "ABSTAIN")
        self.assertEqual(self.game.properties["B-U-race_type_sprint"], "ABSTAIN")
        invite = self.game.invite_fields()
        self.assertEqual(invite["game_mode"], "5")
        self.assertEqual(invite["track"], "cs.11.2")
        self.assertEqual(invite["race_type_speedtrap"], "cs.11.2")
        publication = TunnelDatagram(
            28,
            (
                TunnelPacket(
                    1,
                    bytes.fromhex("0000010100000103")
                    + bytes.fromhex("018404"),
                ),
            ),
        ).encode(EKEY)
        guest_replies = self.service.handle_datagram(
            publication,
            self.guest_addr,
        )
        guest_attributes = [
            game_manager_body(active.payload)
            for raw, target in guest_replies
            if target == self.guest_addr
            for active in _active_messages(raw)
            if game_manager_body(active.payload).startswith(
                bytes.fromhex("000000001d15")
            )
        ]
        self.assertEqual(len(guest_attributes), 1)
        guest_event = decode_session_attributes(guest_attributes[0])
        self.assertEqual(guest_event["game_mode"], "5")
        self.assertEqual(guest_event["car_tier"], "2")
        self.assertEqual(guest_event["race_type_speedtrap"], "cs.11.2")
        self.assertEqual(guest_event["race_type_circuit"], "ABSTAIN")
        self.assertEqual(guest_event["race_type_sprint"], "ABSTAIN")

        # Once the helper is committed, later host settings are intentional.
        # They must update directory capacity and reach the helper on its own
        # destination-local reliable stream.
        race = self.service._race[self.game.gid]
        race.room_commit_sent = True
        race.coop_committed_helpers.add(self.guest_addr)
        self.service._wire[self.guest_addr].session_confirmed = True
        updated_settings = session_attributes(
            {
                "B-U-game_type": "2",
                "B-U-matchmaking_state": "0",
                "B-U-help_type": "0",
                "B-U-game_mode": "5",
                "B-U-car_tier": "1",
                "B-U-max_online_player": "3",
                "B-U-length": "2",
                "B-U-n2o": "0",
                "B-U-collision_detection": "1",
                "B-U-race_type_sprint": "ABSTAIN",
                "B-U-race_type_circuit": "ABSTAIN",
                "B-U-race_type_canyon_due": "ABSTAIN",
                "B-U-race_type_speedtrap": "cs.11.2",
                "B-U-race_type_knockout": "ABSTAIN",
                "B-U-race_type_pursuit_tag": "ABSTAIN",
            }
        )
        update_request = TunnelDatagram(
            29,
            (
                TunnelPacket(
                    1,
                    bytes.fromhex("0000010500000103")
                    + updated_settings
                    + b"\x04",
                ),
            ),
        ).encode(EKEY)
        update_replies = self.service.handle_datagram(
            update_request,
            self.host_addr,
        )
        relayed_updates = [
            game_manager_body(active.payload)
            for raw, target in update_replies
            if target == self.guest_addr
            for active in _active_messages(raw)
            if game_manager_body(active.payload).startswith(
                bytes.fromhex("000000001d15")
            )
        ]
        self.assertTrue(relayed_updates)
        accepted = decode_session_attributes(relayed_updates[-1])
        self.assertEqual(accepted["car_tier"], "1")
        self.assertEqual(accepted["max_online_player"], "3")
        self.assertEqual(accepted["length"], "2")
        self.assertEqual(accepted["n2o"], "0")
        self.assertEqual(accepted["game_mode"], "5")
        self.assertEqual(accepted["race_type_sprint"], "ABSTAIN")
        self.assertEqual(accepted["race_type_speedtrap"], "cs.11.2")
        self.assertEqual(self.game.session.capacity, 3)
        self.assertEqual(self.game.invite_fields()["max_online_player"], "3")

    def test_challenge_preserves_concrete_circuit_wire_identity(self) -> None:
        self.game = self.games.create(
            self.host,
            {
                "players.0.props.{sessionType}": "resetServer",
                "players.0.props.{filter-game_type}": "2",
                "players.0.props.{filter-matchmaking_state}": "0",
                "players.0.props.{pref-help_type}": "2",
            },
            server_hosted=True,
        )
        participant = self.games.enter(
            self.game.gid,
            self.host,
            internal_ip="192.168.1.10",
            internal_port=1042,
        )
        self.assertIsNotNone(participant)
        assert participant is not None
        self.host_participant = participant
        self.service = CarbonRebroadcasterService(self.games)
        self._bind_host()

        bronze_circuit = session_attributes(
            {
                "B-U-game_type": "2",
                "B-U-matchmaking_state": "0",
                "B-U-help_type": "0",
                "B-U-game_mode": "1",
                "B-U-car_tier": "1",
                "B-U-max_online_player": "3",
                "B-U-length": "1",
                "B-U-n2o": "1",
                "B-U-collision_detection": "1",
                "B-U-race_type_sprint": "ABSTAIN",
                "B-U-race_type_circuit": "cs.8.1",
                "B-U-race_type_canyon_due": "ABSTAIN",
                "B-U-race_type_speedtrap": "ABSTAIN",
                "B-U-race_type_knockout": "ABSTAIN",
                "B-U-race_type_pursuit_tag": "ABSTAIN",
            }
        )
        request = TunnelDatagram(
            28,
            (
                TunnelPacket(
                    1,
                    bytes.fromhex("0000010200000102")
                    + bronze_circuit
                    + b"\x04",
                ),
            ),
        ).encode(EKEY)
        self.service.handle_datagram(request, self.host_addr)

        captured = decode_session_attributes(
            self.service._race[self.game.gid].attributes
        )
        self.assertEqual(captured["game_type"], "2")
        self.assertEqual(captured["game_mode"], "1")
        self.assertEqual(captured["car_tier"], "1")
        self.assertEqual(captured["max_online_player"], "3")
        self.assertEqual(self.game.session.capacity, 3)
        self.assertEqual(self.game.invite_fields()["max_online_player"], "3")
        self.assertEqual(captured["race_type_circuit"], "cs.8.1")
        self.assertEqual(captured["race_type_sprint"], "ABSTAIN")
        self.assertEqual(self.game.properties["B-U-game_mode"], "1")
        self.assertEqual(
            self.game.properties["B-U-race_type_circuit"],
            "cs.8.1",
        )

        guest = self.games.enter(
            self.game.gid,
            self.guest,
            invite_entry=True,
        )
        third_identity, _ = self.identities.login("Third")
        third = self.games.enter(
            self.game.gid,
            third_identity,
            invite_entry=True,
        )
        fourth_identity, _ = self.identities.login("Fourth")
        fourth = self.games.enter(
            self.game.gid,
            fourth_identity,
            invite_entry=True,
        )
        self.assertIsNotNone(guest)
        self.assertIsNotNone(third)
        self.assertIsNone(fourth)

    def test_post_race_challenge_settings_updates_reach_guest(self) -> None:
        self.game = self.games.create(
            self.host,
            {
                "players.0.props.{sessionType}": "resetServer",
                "players.0.props.{filter-game_type}": "2",
                "players.0.props.{filter-matchmaking_state}": "0",
            },
            server_hosted=True,
        )
        host_participant = self.games.enter(
            self.game.gid,
            self.host,
            internal_ip="192.168.1.10",
            internal_port=1042,
        )
        guest_participant = self.games.enter(
            self.game.gid,
            self.guest,
            internal_ip="192.168.1.11",
            internal_port=55277,
            invite_entry=True,
        )
        self.assertIsNotNone(host_participant)
        self.assertIsNotNone(guest_participant)
        assert host_participant is not None and guest_participant is not None
        self.host_participant = host_participant
        self.guest_participant = guest_participant
        self.service = CarbonRebroadcasterService(self.games)
        self._bind_host()
        self._bind_guest()
        self.service._wire[self.host_addr].session_confirmed = True
        self.service._wire[self.guest_addr].session_confirmed = True
        race = self.service._race[self.game.gid]
        race.post_race_reopened = True
        race.room_commit_sent = True
        race.coop_committed_helpers.add(self.guest_addr)

        def publish(sequence: int, attributes: bytes) -> dict[str, str]:
            request = TunnelDatagram(
                28,
                (
                    TunnelPacket(
                        1,
                        int(sequence).to_bytes(4, "big")
                        + bytes.fromhex("00000102")
                        + attributes
                        + b"\x04",
                    ),
                ),
            ).encode(EKEY)
            replies = self.service.handle_datagram(request, self.host_addr)
            relayed = [
                game_manager_body(active.payload)
                for raw, target in replies
                if target == self.guest_addr
                for active in _active_messages(raw)
                if game_manager_body(active.payload).startswith(
                    bytes.fromhex("000000001d15")
                )
            ]
            self.assertTrue(relayed)
            return decode_session_attributes(relayed[-1])

        circuit = session_attributes(
            {
                "B-U-game_type": "2",
                "B-U-matchmaking_state": "0",
                "B-U-help_type": "0",
                "B-U-game_mode": "1",
                "B-U-car_tier": "3",
                "B-U-max_online_player": "2",
                "B-U-length": "1",
                "B-U-n2o": "1",
                "B-U-collision_detection": "1",
                "B-U-race_type_circuit": "ex.5.1",
                "B-U-race_type_sprint": "ABSTAIN",
                "B-U-race_type_canyon_due": "ABSTAIN",
                "B-U-race_type_speedtrap": "ABSTAIN",
                "B-U-race_type_knockout": "ABSTAIN",
                "B-U-race_type_pursuit_tag": "ABSTAIN",
            }
        )
        first = publish(0x102, circuit)
        self.assertEqual(first["game_mode"], "1")
        self.assertEqual(first["race_type_circuit"], "ex.5.1")

        sprint = session_attributes(
            {
                "B-U-game_type": "2",
                "B-U-matchmaking_state": "0",
                "B-U-help_type": "0",
                "B-U-game_mode": "0",
                "B-U-car_tier": "3",
                "B-U-max_online_player": "2",
                "B-U-length": "2",
                "B-U-n2o": "0",
                "B-U-collision_detection": "1",
                "B-U-race_type_circuit": "ABSTAIN",
                "B-U-race_type_sprint": "ex.4.2",
                "B-U-race_type_canyon_due": "ABSTAIN",
                "B-U-race_type_speedtrap": "ABSTAIN",
                "B-U-race_type_knockout": "ABSTAIN",
                "B-U-race_type_pursuit_tag": "ABSTAIN",
            }
        )
        second = publish(0x103, sprint)
        self.assertEqual(second["game_mode"], "0")
        self.assertEqual(second["race_type_sprint"], "ex.4.2")
        self.assertEqual(second["race_type_circuit"], "ABSTAIN")
        self.assertEqual(second["length"], "2")
        self.assertEqual(second["n2o"], "0")

        transient = session_attributes(
            {
                "B-U-game_type": "2",
                "B-U-matchmaking_state": "0",
                "B-U-help_type": "0",
                "B-U-game_mode": "0",
                "B-U-car_tier": "3",
                "B-U-max_online_player": "2",
                "B-U-length": "3",
                "B-U-n2o": "1",
                "B-U-collision_detection": "1",
                "B-U-race_type_circuit": "ABSTAIN",
                "B-U-race_type_sprint": "ABSTAIN",
                "B-U-race_type_canyon_due": "ABSTAIN",
                "B-U-race_type_speedtrap": "ABSTAIN",
                "B-U-race_type_knockout": "ABSTAIN",
                "B-U-race_type_pursuit_tag": "ABSTAIN",
            }
        )
        third = publish(0x104, transient)
        self.assertEqual(third["race_type_sprint"], "ex.4.2")
        self.assertEqual(third["length"], "3")
        self.assertEqual(third["n2o"], "1")


    def test_fast_pc_host_barrier_waits_for_final_continuation_ack(self) -> None:
        """Do not open the host barrier while the helper is only at 0x109.

        The live PC trace completed the helper object with ACK 0x109, then
        acknowledged the final host continuation 0x10a on the next datagram.
        Termux had already acknowledged 0x10a when its object completed.  The
        host-side 0x01 must therefore be deferred only for the fast ordering
        and released as soon as the helper ACK catches up.
        """
        self.game.properties["B-U-game_type"] = "2"
        self._bind_host()
        host_0184 = TunnelDatagram(24, (
            TunnelPacket(1, bytes.fromhex("0000010100000102") + bytes.fromhex("018404")),
        )).encode(EKEY)
        self.service.handle_datagram(host_0184, self.host_addr)
        for index, offset in enumerate((0, 0x1E4, 0x3C8), start=2):
            request = TunnelDatagram(30 + index, (
                TunnelPacket(
                    1,
                    int(index).to_bytes(4, "big")
                    + bytes.fromhex("00000102")
                    + _session_block(1, "Host", offset)
                    + b"\x04",
                ),
            )).encode(EKEY)
            self.service.handle_datagram(request, self.host_addr)
        host_token = TunnelDatagram(40, (
            TunnelPacket(
                1,
                bytes.fromhex("0000000600000108")
                + bytes.fromhex("0000000002427f6e98")
                + b"\x04",
            ),
        )).encode(EKEY)
        self.service.handle_datagram(host_token, self.host_addr)

        self.guest_participant = self.games.enter(
            self.game.gid,
            self.guest,
            internal_ip=self.guest_participant.internal_ip,
            internal_port=self.guest_participant.internal_port,
            invite_remote_player_id=self.host_participant.player_id,
        )
        assert self.guest_participant is not None
        self._bind_guest()
        guest_0184 = TunnelDatagram(28, (
            TunnelPacket(1, bytes.fromhex("0000010100000103") + bytes.fromhex("018404")),
        )).encode(EKEY)
        self.service.handle_datagram(guest_0184, self.guest_addr)

        # Model the live fast-PC ordering: the host continuations are already
        # on the wire before the helper's complete local object is processed.
        early_replies: list[tuple[bytes, tuple[str, int]]] = []
        self.assertEqual(
            self.service.session_objects.append_remote_parts(
                early_replies,
                self.host_addr,
                self.guest_addr,
                offsets={0x1E4, 0x3C8},
                bundle=True,
            ),
            2,
        )
        guest_wire = self.service._wire[self.guest_addr]
        continuation_final = guest_wire.invite_host_continuation_final_sequence
        self.assertNotEqual(continuation_final, 0)
        ack_before_final = (continuation_final - 1) & 0x0FFFFFFF

        final_replies: list[tuple[bytes, tuple[str, int]]] = []
        for index, offset in enumerate((0, 0x1E4, 0x3C8), start=10):
            request = TunnelDatagram(60 + index, (
                TunnelPacket(
                    1,
                    int(index).to_bytes(4, "big")
                    + ack_before_final.to_bytes(4, "big")
                    + _session_block(2, "Guest", offset)
                    + b"\x04",
                ),
            )).encode(EKEY)
            final_replies = self.service.handle_datagram(request, self.guest_addr)

        self.assertFalse(any(
            target == self.host_addr
            and game_manager_body(item.payload) == bytes.fromhex("0000000001")
            for raw, target in final_replies
            for item in _active_messages(raw)
        ))
        self.assertTrue(guest_wire.invite_host_barrier_pending)
        self.assertFalse(guest_wire.session_probe_sent)

        catch_up = TunnelDatagram(90, (
            TunnelPacket(
                1,
                (20).to_bytes(4, "big")
                + continuation_final.to_bytes(4, "big"),
            ),
        )).encode(EKEY)
        catch_up_replies = self.service.handle_datagram(catch_up, self.guest_addr)
        self.assertTrue(any(
            target == self.host_addr
            and game_manager_body(item.payload) == bytes.fromhex("0000000001")
            for raw, target in catch_up_replies
            for item in _active_messages(raw)
        ))
        self.assertTrue(any(
            target == self.guest_addr
            and game_manager_body(item.payload) == bytes.fromhex("0000000001")
            and ((item.sequence >> 28) & 0xF) == 0
            for raw, target in catch_up_replies
            for item in _active_messages(raw)
        ))
        self.assertFalse(guest_wire.invite_host_barrier_pending)
        self.assertTrue(guest_wire.session_probe_sent)


    def test_fast_pc_same_call_continuations_still_defer_host_barrier(self) -> None:
        """Gate the barrier when 0x109/0x10a are emitted in the same handler call.

        The live PC trace completed the helper object while the helper still
        acknowledged 0x108. Completing that object caused the server to emit
        the host's 0x1e4/0x3c8 continuations as 0x109/0x10a in the same call.
        V793 skipped its ACK gate in exactly that case and opened the host
        barrier early. The barrier must remain pending until a later packet
        acknowledges the newly emitted final continuation.
        """
        self.game.properties["B-U-game_type"] = "2"
        self._bind_host()
        host_0184 = TunnelDatagram(24, (
            TunnelPacket(1, bytes.fromhex("0000010100000102") + bytes.fromhex("018404")),
        )).encode(EKEY)
        self.service.handle_datagram(host_0184, self.host_addr)
        for index, offset in enumerate((0, 0x1E4, 0x3C8), start=2):
            request = TunnelDatagram(30 + index, (
                TunnelPacket(
                    1,
                    int(index).to_bytes(4, "big")
                    + bytes.fromhex("00000102")
                    + _session_block(1, "Host", offset)
                    + b"\x04",
                ),
            )).encode(EKEY)
            self.service.handle_datagram(request, self.host_addr)
        host_token = TunnelDatagram(40, (
            TunnelPacket(
                1,
                bytes.fromhex("0000000600000108")
                + bytes.fromhex("0000000002427f6e98")
                + b"\x04",
            ),
        )).encode(EKEY)
        self.service.handle_datagram(host_token, self.host_addr)

        self.guest_participant = self.games.enter(
            self.game.gid,
            self.guest,
            internal_ip=self.guest_participant.internal_ip,
            internal_port=self.guest_participant.internal_port,
            invite_remote_player_id=self.host_participant.player_id,
        )
        assert self.guest_participant is not None
        self._bind_guest()
        guest_0184 = TunnelDatagram(28, (
            TunnelPacket(1, bytes.fromhex("0000010100000103") + bytes.fromhex("018404")),
        )).encode(EKEY)
        self.service.handle_datagram(guest_0184, self.guest_addr)

        guest_wire = self.service._wire[self.guest_addr]
        expected_final = (int(guest_wire.next_server_sequence) + 1) & 0x0FFFFFFF
        ack_before_final = (expected_final - 2) & 0x0FFFFFFF

        final_replies: list[tuple[bytes, tuple[str, int]]] = []
        for index, offset in enumerate((0, 0x1E4, 0x3C8), start=10):
            request = TunnelDatagram(60 + index, (
                TunnelPacket(
                    1,
                    int(index).to_bytes(4, "big")
                    + ack_before_final.to_bytes(4, "big")
                    + _session_block(2, "Guest", offset)
                    + b"\x04",
                ),
            )).encode(EKEY)
            final_replies = self.service.handle_datagram(request, self.guest_addr)

        continuation_final = guest_wire.invite_host_continuation_final_sequence
        self.assertEqual(continuation_final, expected_final)
        self.assertFalse(self.service._sequence_acked(
            guest_wire.last_client_acknowledgement,
            continuation_final,
        ))
        self.assertFalse(any(
            target == self.host_addr
            and game_manager_body(item.payload) == bytes.fromhex("0000000001")
            for raw, target in final_replies
            for item in _active_messages(raw)
        ))
        self.assertTrue(guest_wire.invite_host_barrier_pending)
        self.assertFalse(guest_wire.session_probe_sent)

        # V796 still opened the barrier when the final ACK arrived on a pure
        # retransmission of the same client sequence.  That is the exact PC
        # trace: ACK advances to 0x10a while client_seq remains unchanged.
        deferred_sequence = guest_wire.invite_host_barrier_deferred_client_sequence
        pure_ack = TunnelDatagram(90, (
            TunnelPacket(
                1,
                deferred_sequence.to_bytes(4, "big")
                + continuation_final.to_bytes(4, "big"),
            ),
        )).encode(EKEY)
        pure_ack_replies = self.service.handle_datagram(pure_ack, self.guest_addr)
        self.assertFalse(any(
            target == self.host_addr
            and game_manager_body(item.payload) == bytes.fromhex("0000000001")
            for raw, target in pure_ack_replies
            for item in _active_messages(raw)
        ))
        self.assertTrue(guest_wire.invite_host_barrier_pending)
        self.assertFalse(guest_wire.session_probe_sent)

        progressed = TunnelDatagram(91, (
            TunnelPacket(
                1,
                ((deferred_sequence + 1) & 0x0FFFFFFF).to_bytes(4, "big")
                + continuation_final.to_bytes(4, "big")
                + bytes.fromhex("000000001c00000000000304"),
            ),
        )).encode(EKEY)
        progressed_replies = self.service.handle_datagram(progressed, self.guest_addr)
        self.assertTrue(any(
            target == self.host_addr
            and game_manager_body(item.payload) == bytes.fromhex("0000000001")
            for raw, target in progressed_replies
            for item in _active_messages(raw)
        ))
        self.assertTrue(any(
            target == self.guest_addr
            and game_manager_body(item.payload) == bytes.fromhex("0000000001")
            and ((item.sequence >> 28) & 0xF) == 0
            for raw, target in progressed_replies
            for item in _active_messages(raw)
        ))
        self.assertFalse(guest_wire.invite_host_barrier_pending)
        self.assertTrue(guest_wire.session_probe_sent)

    def test_coop_helper_receives_retail_room_commit_after_session_barrier(self) -> None:
        self.game = self.games.create(
            self.host,
            {
                "players.0.props.{sessionType}": "resetServer",
                "players.0.props.{filter-game_type}": "2",
                "players.0.props.{filter-matchmaking_state}": "0",
                "players.0.props.{pref-help_type}": "2",
            },
            server_hosted=True,
        )
        first = self.games.enter(
            self.game.gid,
            self.host,
            internal_ip="192.168.1.10",
            internal_port=1042,
        )
        self.games.challenge_quick_join_after_ready = True
        self.games.set_challenge_ready(
            self.game.gid,
            True,
            reason="test-ready",
        )
        second = self.games.enter(
            self.game.gid,
            self.guest,
            internal_ip="192.168.1.11",
            internal_port=55277,
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.host_participant = first
        self.guest_participant = second
        self.service = CarbonRebroadcasterService(self.games)

        self._bind_host()
        challenge = session_attributes(
            {
                "B-U-game_type": "2",
                "B-U-matchmaking_state": "0",
                "B-U-help_type": "0",
                "B-U-game_mode": "0",
                "B-U-car_tier": "2",
                "B-U-max_online_player": "2",
                "B-U-length": "1",
                "B-U-n2o": "1",
                "B-U-collision_detection": "1",
                "B-U-race_type_sprint": "cs.2.2",
                "B-U-race_type_circuit": "ABSTAIN",
                "B-U-race_type_canyon_due": "ABSTAIN",
                "B-U-race_type_speedtrap": "ABSTAIN",
                "B-U-race_type_knockout": "ABSTAIN",
                "B-U-race_type_pursuit_tag": "ABSTAIN",
            }
        )
        host_attributes = TunnelDatagram(
            28,
            (
                TunnelPacket(
                    1,
                    bytes.fromhex("0000010200000102") + challenge + b"\x04",
                ),
            ),
        ).encode(EKEY)
        self.service.handle_datagram(host_attributes, self.host_addr)
        self._bind_guest()

        for addr, participant in (
            (self.host_addr, self.host_participant),
            (self.guest_addr, self.guest_participant),
        ):
            wire = self.service._wire[addr]
            wire.session_confirmed = True
            wire.session_blocks = {
                offset: _session_block(participant.player_id, participant.identity.persona, offset)
                for offset in (0, 0x1E4, 0x3C8)
            }

        race = self.service._race[self.game.gid]
        race.latest_room_timer = start_timer(
            current_seconds=10.0,
            duration_seconds=590.0,
            timer_id=0,
        )
        race.coop_host_state7_seen = True
        state7 = bytes.fromhex("000000001c000000000007")
        host_latency = bytes.fromhex("0000000012000000013f800000")
        helper_latency = bytes.fromhex("00000000120000000200000000")
        race.pending_coop_host_state7 = state7 + named_state("stale", 14)
        self.service._wire[self.guest_addr].allocation_lock_triggered = True
        replies: list[tuple[bytes, tuple[str, int]]] = []
        self.service.room_commit.maybe_finalize_room_session(
            replies,
            self.game,
            barrier_host=self.host_addr,
            barrier_token=bytes.fromhex("01020304"),
        )
        self.assertFalse(race.room_commit_sent)
        self.assertEqual(race.pending_coop_host_state7, state7)

        self.service._wire[self.host_addr].latest_latency_info = host_latency
        self.service._wire[self.guest_addr].latest_latency_info = helper_latency
        host_sequence_base = self.service._wire[self.host_addr].next_server_sequence
        guest_sequence_base = self.service._wire[self.guest_addr].next_server_sequence
        guest_wire = self.service._wire[self.guest_addr]
        guest_wire.last_client_acknowledgement = (
            guest_sequence_base - 2
        ) & 0x0FFFFFFF
        replies = []
        self.service.room_commit.maybe_finalize_room_session(replies, self.game)
        self.assertFalse(race.room_commit_sent)
        self.assertEqual(replies, [])
        self.assertEqual(
            guest_wire.room_commit_prerequisite_sequence,
            (guest_sequence_base - 1) & 0x0FFFFFFF,
        )

        # chalangeinvite frames 2777-2794: helper ACKs the standalone context
        # record before the flags=4 room-commit aggregate is released.
        guest_wire.last_client_acknowledgement = (
            guest_sequence_base - 1
        ) & 0x0FFFFFFF
        replies = []
        self.service.room_commit.advance_helper_generation_barrier(
            replies,
            self.game,
            current_address=self.guest_addr,
        )
        attribute_bodies = [
            game_manager_body(active.payload)
            for raw, target in replies
            if target == self.host_addr
            for active in _active_messages(raw)
            if game_manager_body(active.payload).startswith(bytes.fromhex("000000001d15"))
        ]
        self.assertEqual(len(attribute_bodies), 2)
        decoded = decode_session_attributes(attribute_bodies[-1])
        self.assertEqual(decoded["game_type"], "2")
        self.assertEqual(decoded["help_type"], "0")
        self.assertEqual(decoded["game_mode"], "0")
        self.assertEqual(decoded["car_tier"], "2")
        self.assertEqual(decoded["race_type_sprint"], "cs.2.2")
        self.assertTrue(race.room_commit_sent)
        self.assertEqual(race.phase, RacePhase.SESSION_SETUP)

        host_logical = [
            game_manager_body(active.payload)
            for raw, target in replies
            if target == self.host_addr
            for active in _active_messages(raw)
            if logical_type(game_manager_body(active.payload)) is not None
        ]
        host_types = [logical_type(body) for body in host_logical]
        commit_types = [
            OLMessageType.GAME_ATTRIBUTES,
            OLMessageType.START_TIMER,
            OLMessageType.GAME_ATTRIBUTES,
            OLMessageType.ACTIVE_GAME_MESSAGE,
        ]
        self.assertTrue(any(
            host_types[index:index + 4] == commit_types
            for index in range(max(0, len(host_types) - 3))
        ))

        host_commit_datagrams = [
            raw
            for raw, target in replies
            if target == self.host_addr and len(_active_messages(raw)) == 7
        ]
        self.assertEqual(len(host_commit_datagrams), 1)
        self.assertEqual(len(host_commit_datagrams[0]), 485)

        host_actives = [
            active
            for raw, target in replies
            if target == self.host_addr
            for active in _active_messages(raw)
        ]
        guest_actives = [
            active
            for raw, target in replies
            if target == self.guest_addr
            for active in _active_messages(raw)
        ]

        # chalangeinvite frame 2779: state7 is first sent normally, then the
        # host receives two cumulative reliable-history packets with flags 1/2.
        host_history = [
            active for active in host_actives
            if ((int(active.sequence) >> 28) & 0x0F) in (1, 2)
        ]
        self.assertEqual(
            [((int(active.sequence) >> 28) & 0x0F) for active in host_history],
            [1, 2],
        )
        self.assertEqual(
            [int(active.sequence) & 0x0FFFFFFF for active in host_history],
            [
                (host_sequence_base + 5) & 0x0FFFFFFF,
                (host_sequence_base + 6) & 0x0FFFFFFF,
            ],
        )
        self.assertEqual(
            _aggregate_logical_records(host_history[0]),
            (host_latency, state7),
        )
        self.assertEqual(
            _aggregate_logical_records(host_history[1]),
            (helper_latency, host_latency, state7),
        )

        # chalangeinvite frame 2794: the helper receives one flags=4 packet.
        # The five actual reliable records are newest-to-oldest below; 0x0e,
        # 0x0c, 0x96 and 0x12 are their one-byte lengths on the wire.
        self.assertEqual(len(guest_actives), 1)
        guest_commit_datagrams = [
            raw
            for raw, target in replies
            if target == self.guest_addr and len(_active_messages(raw)) == 1
        ]
        self.assertEqual(len(guest_commit_datagrams), 1)
        self.assertEqual(len(guest_commit_datagrams[0]), 224)
        guest_commit = guest_actives[0]
        self.assertEqual(len(guest_commit.payload), 220)
        self.assertEqual((int(guest_commit.sequence) >> 28) & 0x0F, 4)
        self.assertEqual(
            int(guest_commit.sequence) & 0x0FFFFFFF,
            (guest_sequence_base + 4) & 0x0FFFFFFF,
        )
        guest_records = _aggregate_logical_records(guest_commit)
        self.assertEqual(
            guest_records,
            (helper_latency, host_latency, state7, challenge, race.latest_room_timer),
        )
        guest_challenge = decode_session_attributes(guest_records[3])
        self.assertEqual(guest_challenge["game_type"], "2")
        self.assertEqual(guest_challenge["matchmaking_state"], "0")
        self.assertEqual(guest_challenge["help_type"], "0")
        self.assertEqual(guest_challenge["game_mode"], "0")
        self.assertEqual(guest_challenge["race_type_sprint"], "cs.2.2")
        self.assertEqual(
            self.service._wire[self.guest_addr].next_server_sequence,
            (guest_sequence_base + 5) & 0x0FFFFFFF,
        )
        self.assertEqual(
            self.service._wire[self.host_addr].next_server_sequence,
            (host_sequence_base + 7) & 0x0FFFFFFF,
        )

        guest_active_count = sum(
            1
            for raw, target in replies
            if target == self.guest_addr
            for _ in _active_messages(raw)
        )
        # The allocation bundle, not the later room commit, owns HostProps.
        self.assertEqual(guest_active_count, 1)
        guest_hostprops = [
            active.game_manager.body
            for raw, target in replies
            if target == self.guest_addr
            for active in _active_messages(raw)
            if active.game_manager is not None
            and active.game_manager.message_type == 0x0C
        ]
        self.assertEqual(guest_hostprops, [])
        self.assertIn(bytes.fromhex("000000001c000000000007"), host_logical)

        host_confirmations = [
            game_manager_body(active.payload)
            for raw, target in replies
            if target == self.host_addr
            for active in _active_messages(raw)
            if logical_type(game_manager_body(active.payload)) == OLMessageType.CLOCK_SYNC_END
        ]
        self.assertEqual(len(host_confirmations), 1)
        self.assertEqual(host_confirmations[0][5:9], bytes.fromhex("01020304"))

        second_pass: list[tuple[bytes, tuple[str, int]]] = []
        self.service.room_commit.maybe_finalize_room_session(
            second_pass,
            self.game,
        )
        duplicate_attributes = [
            game_manager_body(active.payload)
            for raw, target in second_pass
            if target == self.host_addr
            for active in _active_messages(raw)
            if game_manager_body(active.payload).startswith(bytes.fromhex("000000001d15"))
        ]
        self.assertEqual(duplicate_attributes, [])

        # A later invite joins the already-committed room. It needs its own
        # helper aggregate; the first helper must not consume the room-wide
        # commit flag on behalf of every future participant.
        self.assertEqual(
            self.games.set_authoritative_capacity(
                self.game.gid,
                3,
                reason="test-late-helper",
            ),
            3,
        )
        third_identity, _ = self.identities.login("ThirdHelper")
        third_participant = self.games.enter(
            self.game.gid,
            third_identity,
            internal_ip="192.168.1.12",
            internal_port=55278,
            invite_remote_player_id=self.host_participant.player_id,
            invite_entry=True,
        )
        self.assertIsNotNone(third_participant)
        assert third_participant is not None
        third_addr = ("192.0.2.12", 55278)
        resolution = self.games.resolve_ticket(
            self.games.ticket(self.game, third_participant)
        )
        self.assertIsNotNone(resolution)
        assert resolution is not None
        self.assertTrue(self.service._bind(third_addr, resolution))
        third_wire = self.service._wire[third_addr]
        third_wire.session_confirmed = True
        third_wire.session_object_id = 33
        third_wire.session_blocks = {
            offset: _session_block(
                third_participant.player_id,
                third_participant.identity.persona,
                offset,
            )
            for offset in (0, 0x1E4, 0x3C8)
        }
        third_wire.allocation_lock_triggered = True
        third_latency = bytes.fromhex("00000000120000000340400000")
        third_wire.latest_latency_info = third_latency

        late_replies: list[tuple[bytes, tuple[str, int]]] = []
        self.service.room_commit.maybe_finalize_room_session(
            late_replies,
            self.game,
            barrier_host=self.host_addr,
            barrier_token=bytes.fromhex("05060708"),
        )
        self.assertNotIn(third_addr, race.coop_committed_helpers)
        prerequisite = third_wire.room_commit_prerequisite_sequence
        self.assertNotEqual(prerequisite, 0)

        third_wire.last_client_acknowledgement = prerequisite
        late_replies = []
        self.service.room_commit.maybe_finalize_room_session(
            late_replies,
            self.game,
        )
        self.assertIn(self.guest_addr, race.coop_committed_helpers)
        self.assertIn(third_addr, race.coop_committed_helpers)
        self.assertTrue(race.room_commit_sent)

        third_commits = [
            active
            for raw, target in late_replies
            if target == third_addr
            for active in _active_messages(raw)
            if ((int(active.sequence) >> 28) & 0x0F) == 4
        ]
        self.assertEqual(len(third_commits), 1)
        self.assertEqual(
            _aggregate_logical_records(third_commits[0]),
            (
                third_latency,
                host_latency,
                state7,
                challenge,
                race.latest_room_timer,
            ),
        )

    def test_challenge_state7_capture_discards_commudp_history_tail(self) -> None:
        self.game.server_hosted = True
        self.game.allocator_user_id = self.host.user_id
        self.game.properties["B-U-game_type"] = "2"
        self._bind_host()

        state7 = bytes.fromhex("000000001c000000000007")
        history = named_state("Host", 14)
        active = CommUDPActive(
            0x100,
            0x101,
            bytes.fromhex("0000010000000101") + state7 + history + b"\x04",
            None,
        )
        replies: list[tuple[bytes, tuple[str, int]]] = []
        self.service._handle_bound_active(
            replies,
            self.host_addr,
            self.service._bindings[self.host_addr],
            active,
        )

        race = self.service._race[self.game.gid]
        self.assertTrue(race.coop_host_state7_seen)
        self.assertEqual(race.pending_coop_host_state7, state7)

    def test_unhandled_olmsg_is_logged_once_per_participant_phase(self) -> None:
        self._bind_host()
        logical = bytes.fromhex("0000000021aabbcc")
        active = CommUDPActive(
            0x100,
            0x101,
            bytes.fromhex("0000010000000101") + logical + b"\x04",
            None,
        )
        replies: list[tuple[bytes, tuple[str, int]]] = []
        with self.assertLogs(
            "carbon.rebroadcaster.service",
            level="WARNING",
        ) as captured:
            self.service._handle_bound_active(
                replies,
                self.host_addr,
                self.service._bindings[self.host_addr],
                active,
            )
            self.service._handle_bound_active(
                replies,
                self.host_addr,
                self.service._bindings[self.host_addr],
                active,
            )
        warnings = [
            line
            for line in captured.output
            if "Carbon GM unhandled inbound message" in line
        ]
        self.assertEqual(len(warnings), 1)
        self.assertIn("kind=POST_RACE_SYNC", warnings[0])
        self.assertIn("logical=0000000021aabbcc", warnings[0])

    def test_host_context_snapshot_is_reflected_as_reliable_liveness(self) -> None:
        self._bind_host()
        snapshot = bytes.fromhex("00000000120000000140c00000")
        request = TunnelDatagram(
            30,
            (
                TunnelPacket(
                    1,
                    bytes.fromhex("0000010200000102") + snapshot + b"\x04",
                ),
            ),
        ).encode(EKEY)
        replies = self.service.handle_datagram(request, self.host_addr)
        self.assertEqual(len(replies), 1)
        reflected = _active_messages(replies[0][0])
        self.assertEqual(len(reflected), 1)
        self.assertEqual(game_manager_body(reflected[0].payload), snapshot)
        self.assertEqual(reflected[0].acknowledgement, 0x102)

    def test_joiner_bootstrap_is_local_first_then_existing_host(self) -> None:
        self._bind_host()
        replies = self._bind_guest()
        self.assertEqual([decode_datagram(raw, EKEY).offset_words for raw, _ in replies], [12, 17])
        bootstrap = _active_messages(replies[1][0])
        self.assertEqual([item.game_manager.message_type for item in bootstrap], [0x02, 0x03, 0x03])
        decoded = [decode_player_data(item.game_manager.body, 2)[0] for item in bootstrap[1:]]
        self.assertEqual([item.player_id for item in decoded], [2, 1])
        self.assertEqual([item.state for item in decoded], [3, 6])

    def test_guest_leave_is_delivered_to_host_as_playerleft(self) -> None:
        self._bind_host()
        self._bind_guest()
        self.assertTrue(self.games.leave(self.game.gid, self.guest.user_id))
        self.service.drop_participant(self.game.gid, self.guest.user_id)

        heartbeat = TunnelDatagram(
            40,
            (TunnelPacket(1, bytes.fromhex("0000010800000108")),),
        ).encode(EKEY)
        replies = self.service.handle_datagram(heartbeat, self.host_addr)
        messages = [
            active.game_manager
            for raw, target in replies
            if target == self.host_addr
            for active in _active_messages(raw)
            if active.game_manager is not None
        ]
        leaves = [message for message in messages if message.message_type == 0x07]
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0].body, bytes.fromhex("0187000280"))

    def test_ecnl_is_deferred_while_race_is_active(self) -> None:
        self._bind_host()
        self._bind_guest()
        self.service._race[self.game.gid] = GameRaceState(phase=RacePhase.RACING)

        self.assertTrue(
            self.service.drop_participant(self.game.gid, self.guest.user_id)
        )
        self.assertIn(self.guest_addr, self.service._bindings)

    def test_allocator_ecnl_delivers_playerleft_before_transport_retire(self) -> None:
        self.game.server_hosted = True
        self.game.allocator_user_id = self.host.user_id
        self._bind_host()
        self._bind_guest()
        self.service.confirmations.register(
            self.guest_addr,
            (b"stale-confirmation",),
            base_sequence=0x124,
            final_sequence=0x124,
            label="active-game-state",
            now=1.0,
        )
        self.assertTrue(self.service.confirmations.pending(self.guest_addr))

        deferred = self.service.drop_participant(
            self.game.gid,
            self.host.user_id,
        )

        self.assertFalse(deferred)
        self.assertEqual(
            self.service.session_endpoints(self.game.gid),
            (self.guest_addr,),
        )
        self.assertIsNone(self.service.binding(self.host_addr))
        self.assertIsNotNone(self.service.binding(self.guest_addr))
        self.assertEqual(self.service.confirmations.pending(self.host_addr), ())
        # Stale room traffic is cleared before the terminal leave window.
        self.assertEqual(self.service.confirmations.pending(self.guest_addr), ())
        self.assertTrue(self.games.leave(self.game.gid, self.host.user_id))
        self.assertIsNone(self.games.get(self.game.gid))

        replies = self.service.poll_retries()
        leaves = [
            active.game_manager
            for raw, target in replies
            if target == self.guest_addr
            for active in _active_messages(raw)
            if active.game_manager is not None
            and active.game_manager.message_type == 0x07
        ]
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0].body, bytes.fromhex("0187000180"))
        windows = [
            window
            for window in self.service.confirmations.pending(self.guest_addr)
            if window.label == "player-left"
        ]
        self.assertEqual(len(windows), 1)

        self.service.confirmations.acknowledge(
            self.guest_addr,
            windows[0].final_sequence,
        )
        self.service.poll_retries()
        self.assertIsNone(self.service.binding(self.guest_addr))
        self.assertEqual(self.service.session_endpoints(self.game.gid), ())
        self.assertEqual(self.service.confirmations.pending(self.guest_addr), ())

    def test_guest_udp_drop_resets_countdown_without_ready_epoch(self) -> None:
        self._bind_host()
        self._bind_guest()
        race = self.service._race[self.game.gid]
        race.phase = RacePhase.COUNTDOWN
        race.room_access = RoomAccess.LOCKED
        race.countdown_deadline = 100.0
        race.latest_match_timer = b"stale-match-timer"
        race.countdown_wire_deadline = 99.0
        race.countdown_generation_id = 4
        race.countdown_initial_timer = b"initial"
        race.countdown_latest_timer = b"latest"
        self.service.games.set_quick_join_locked(
            self.game.gid,
            True,
            reason="test-countdown",
        )
        self.assertNotIn(self.game.gid, self.service._ready_epochs)

        with self.assertLogs(
            "carbon.rebroadcaster.service",
            level="INFO",
        ) as captured:
            self.service._drop_endpoint(self.guest_addr)

        self.assertEqual(race.phase, RacePhase.SESSION_SETUP)
        self.assertEqual(race.room_access, RoomAccess.OPEN)
        self.assertFalse(self.game.quick_join_locked)
        self.assertEqual(race.countdown_deadline, 0.0)
        self.assertEqual(race.latest_match_timer, b"")
        self.assertEqual(race.countdown_wire_deadline, 0.0)
        self.assertEqual(race.countdown_generation_id, 0)
        self.assertEqual(race.countdown_initial_timer, b"")
        self.assertEqual(race.countdown_latest_timer, b"")
        self.assertTrue(
            any(
                "Carbon GM orphan countdown reset" in line
                for line in captured.output
            )
        )

    def test_ecnl_is_deferred_while_ready_transition_owns_transport(self) -> None:
        self._bind_host()
        self._bind_guest()
        self.service._race[self.game.gid] = GameRaceState(phase=RacePhase.SESSION_SETUP)
        self.service._ready_epochs[self.game.gid] = ReadyEpoch(
            generation=1,
            stage=ReadyStage.STATE_PAIR_SENT_WAIT_HOST_NATIVE_BUNDLE,
            host_pid=self.host_participant.player_id,
            guest_pid=self.guest_participant.player_id,
            source_first_sequence=0x100,
            source_final_sequence=0x104,
            source_payload_hash=0,
            attributes=b"attributes",
            wire_deadline=20.0,
        )

        self.assertTrue(
            self.service.drop_participant(self.game.gid, self.host.user_id)
        )
        self.assertIn(self.host_addr, self.service._bindings)
        self.assertIn(self.host.user_id, self.game.participants)

    def test_reconnect_clears_stale_publication_and_remote_object_caches(self) -> None:
        ticket = self.games.ticket(self.game, self.guest_participant)
        resolution = self.games.resolve_ticket(ticket)
        self.assertIsNotNone(resolution)
        assert resolution is not None

        old_addr = self.guest_addr
        new_addr = (self.guest_addr[0], self.guest_addr[1] + 1)
        peer_addr = self.host_addr
        source_key = (self.game.gid, self.guest.user_id)
        publication_key = (self.game.gid, self.guest_participant.player_id)

        self.service._wire[old_addr] = self.service._wire.get(old_addr) or EndpointWireState()
        self.service._wire[peer_addr] = EndpointWireState(
            pending_session_releases={old_addr},
            published_remote_objects={source_key: (b"cached",)},
            published_session_offsets={source_key: {0, 0x1E4, 0x3C8}},
        )
        self.service._endpoints[old_addr] = object()
        self.assertTrue(self.service._bind(old_addr, resolution))
        self.service._published_joins.add(publication_key)

        self.assertTrue(self.service._bind(new_addr, resolution))
        self.assertNotIn(old_addr, self.service._bindings)
        self.assertNotIn(old_addr, self.service._wire)
        self.assertNotIn(old_addr, self.service._endpoints)
        self.assertNotIn(publication_key, self.service._published_joins)
        self.assertNotIn(old_addr, self.service._wire[peer_addr].pending_session_releases)
        self.assertNotIn(source_key, self.service._wire[peer_addr].published_remote_objects)
        self.assertNotIn(source_key, self.service._wire[peer_addr].published_session_offsets)
        self.assertEqual(self.service._participant_endpoints[source_key], new_addr)

    def test_same_address_reconnect_bootstraps_without_republishing_self_join(self) -> None:
        ticket = self.games.ticket(self.game, self.host_participant)
        resolution = self.games.resolve_ticket(ticket)
        self.assertIsNotNone(resolution)
        assert resolution is not None

        source_key = (self.game.gid, self.host.user_id)
        publication_key = (self.game.gid, self.host_participant.player_id)
        self.service._wire[self.host_addr] = EndpointWireState(bootstrap_sent=True)
        self.assertTrue(self.service._bind(self.host_addr, resolution))
        self.service._published_joins.add(publication_key)
        self.service._reconnect_pending.add(source_key)
        self.service._drop_endpoint(self.host_addr)
        self.service._wire[self.host_addr] = EndpointWireState(bootstrap_sent=True)

        self.assertTrue(self.service._bind(self.host_addr, resolution))
        self.assertTrue(self.service._wire[self.host_addr].suppress_self_join_publication)
        replies = []
        self.service._append_join_publication(replies, self.host_addr, resolution)

        self.assertFalse(self.service._wire[self.host_addr].suppress_self_join_publication)
        self.assertNotIn(publication_key, self.service._published_joins)
        self.assertFalse(any(
            active.game_manager is not None and active.game_manager.message_type == 0x05
            for raw, _ in replies
            for active in _active_messages(raw)
        ))

    def test_joiner_0184_publishes_full_0185_to_guest_and_host(self) -> None:
        self._bind_host()
        self._bind_guest()
        request = TunnelDatagram(
            28,
            (
                TunnelPacket(1, bytes.fromhex("0000010100000103") + bytes.fromhex("018404")),
            ),
        ).encode(EKEY)
        replies = self.service.handle_datagram(request, self.guest_addr)
        self.assertEqual({target for _, target in replies}, {self.host_addr, self.guest_addr})
        type5_replies = []
        session_bootstrap = []
        hostprops = []
        for raw, target in replies:
            active = _active_messages(raw)
            gm_types = [item.game_manager.message_type for item in active if item.game_manager is not None]
            if gm_types == [0x05]:
                type5_replies.append((active[0], target))
            elif gm_types == [0x0C]:
                hostprops.append((active[0], target))
            else:
                session_bootstrap.append((active, target))
        self.assertEqual(len(type5_replies), 2)
        self.assertEqual(len(session_bootstrap), 1)
        # Capture frames 445-464 keep HostProps behind the local 0x1e/0x03
        # session barrier; it must not ride alongside the initial 0x0185.
        self.assertEqual(hostprops, [])
        self.assertEqual(session_bootstrap[0][1], self.guest_addr)
        self.assertEqual(
            [game_manager_body(item.payload)[:6] for item in session_bootstrap[0][0]],
            [
                bytes.fromhex("000000000022"),
                bytes.fromhex("000000000104"),
                bytes.fromhex("000000001d15"),
            ],
        )
        self.assertEqual(
            [item.sequence >> 28 for item in session_bootstrap[0][0]],
            [0, 1, 0],
        )
        for active, _target in type5_replies:
            message = active.game_manager
            self.assertEqual(message.message_type, 0x05)
            self.assertEqual(int.from_bytes(message.body[2:4], "big"), 2)
            player, consumed = decode_player_data(message.body, 4)
            self.assertEqual(player.player_id, 2)
            self.assertEqual(player.name, "Guest")
            self.assertEqual(player.state, 6)
            self.assertEqual(consumed, len(message.body))

        guest_join_window = next(
            window
            for window in self.service.confirmations.pending(self.guest_addr)
            if window.label == "session-self-join"
        )
        duplicate = self.service.handle_datagram(request, self.guest_addr)
        self.assertIn(
            (guest_join_window.records[0], self.guest_addr),
            duplicate,
        )
        duplicate_ack = next(
            item
            for raw, target in duplicate
            if target == self.guest_addr
            for item in _active_messages(raw)
            if game_manager_body(item.payload) == b""
        )
        wire = self.service._wire[self.guest_addr]
        self.assertEqual(duplicate_ack.sequence, wire.next_server_sequence)

        repeated = self.service.handle_datagram(request, self.guest_addr)
        self.assertIn(
            (guest_join_window.records[0], self.guest_addr),
            repeated,
        )
        repeated_ack = next(
            item
            for raw, target in repeated
            if target == self.guest_addr
            for item in _active_messages(raw)
            if game_manager_body(item.payload) == b""
        )
        self.assertEqual(repeated_ack.sequence, duplicate_ack.sequence)
        self.assertEqual(wire.next_server_sequence, duplicate_ack.sequence)

    def test_session_stage_waits_without_republish_after_transport_ack(self) -> None:
        self._bind_host()
        self._bind_guest()
        request = TunnelDatagram(
            28,
            (
                TunnelPacket(
                    1,
                    bytes.fromhex("0000010100000103") + bytes.fromhex("018404"),
                ),
            ),
        ).encode(EKEY)
        self.service.handle_datagram(request, self.guest_addr)
        wire = self.service._wire[self.guest_addr]
        window = wire.session_bootstrap_window
        self.assertIsNotNone(window)
        assert window is not None
        old_records = window.records
        old_base = window.base_sequence
        old_final = window.final_sequence
        next_sequence = wire.next_server_sequence
        next_offset = wire.next_offset_words
        retries_sent = window.retry.retries_sent

        self.service.confirmations.acknowledge(
            self.guest_addr,
            old_final,
        )
        wire.last_client_acknowledgement = old_final
        self.service.confirmations.clear_endpoint(self.host_addr)
        self.service.confirmations.clear_endpoint(self.guest_addr)

        after_ack = self.service.poll_retries(
            now=window.retry.retry_not_before,
        )
        self.assertEqual(after_ack, [])
        self.assertEqual(window.records, old_records)
        self.assertEqual(window.base_sequence, old_base)
        self.assertEqual(window.final_sequence, old_final)
        self.assertTrue(window.transport_acknowledged)
        self.assertEqual(window.retry.retries_sent, retries_sent)
        self.assertEqual(wire.next_server_sequence, next_sequence)
        self.assertEqual(wire.next_offset_words, next_offset)
        self.assertIs(wire.session_bootstrap_window, window)

        session_object = TunnelDatagram(
            40,
            (
                TunnelPacket(
                    1,
                    bytes.fromhex("0000000200000106")
                    + _session_block(2, "Guest", 0)
                    + b"\x04",
                ),
            ),
        ).encode(EKEY)
        self.service.handle_datagram(session_object, self.guest_addr)
        self.service.poll_retries(now=window.retry.retry_not_before)

        self.assertIsNone(wire.session_bootstrap_window)

    def test_session_bootstrap_retry_waits_for_application_progress(self) -> None:
        self._bind_host()
        self._bind_guest()
        self.service._wire[self.host_addr].session_bootstrap_window = None
        request = TunnelDatagram(
            28,
            (
                TunnelPacket(
                    1,
                    bytes.fromhex("0000010100000103") + bytes.fromhex("018404"),
                ),
            ),
        ).encode(EKEY)
        self.service.handle_datagram(request, self.guest_addr)
        wire = self.service._wire[self.guest_addr]
        window = wire.session_bootstrap_window
        self.assertIsNotNone(window)
        assert window is not None
        # Isolate the bootstrap-specific application confirmation window.  The
        # generic manager is covered separately and may have several join
        # publications due at the same instant.
        self.service.confirmations.clear_endpoint(self.host_addr)
        self.service.confirmations.clear_endpoint(self.guest_addr)
        payloads = window.records
        self.assertTrue(payloads)
        next_sequence = wire.next_server_sequence
        next_offset = wire.next_offset_words

        retried = self.service.poll_retries(
            now=window.retry.retry_not_before,
        )

        self.assertEqual(
            retried,
            [(payload, self.guest_addr) for payload in payloads],
        )
        self.assertEqual(wire.next_server_sequence, next_sequence)
        self.assertEqual(wire.next_offset_words, next_offset)
        self.assertEqual(window.retry.retries_sent, 1)

        wire.last_client_acknowledgement = window.final_sequence
        old_base = window.base_sequence
        old_final = window.final_sequence
        retries_sent = window.retry.retries_sent
        retried_after_transport_ack = self.service.poll_retries(
            now=window.retry.retry_not_before,
        )
        self.assertEqual(retried_after_transport_ack, [])
        self.assertEqual(window.records, payloads)
        self.assertEqual(window.base_sequence, old_base)
        self.assertEqual(window.final_sequence, old_final)
        self.assertTrue(window.transport_acknowledged)
        self.assertEqual(window.retry.retries_sent, retries_sent)
        self.assertEqual(wire.next_server_sequence, next_sequence)
        self.assertEqual(wire.next_offset_words, next_offset)
        self.assertIs(wire.session_bootstrap_window, window)

        wire.session_blocks[0] = b"application-progress"
        self.assertEqual(
            self.service.poll_retries(
                now=window.retry.retry_not_before,
            ),
            [],
        )
        self.assertIsNone(wire.session_bootstrap_window)

    def test_dedicated_challenge_guest_gets_only_host_offset_zero_during_join(self) -> None:
        self.game.server_hosted = True
        self.game.properties["B-U-game_type"] = "2"
        self.game.created_tick_ms = 0x27FC21B5
        self._bind_host()
        host_wire = self.service._wire[self.host_addr]
        host_wire.session_blocks = {
            offset: _session_block(14, "Host", offset)
            for offset in (0, 0x1E4, 0x3C8)
        }

        self._bind_guest()
        guest_0184 = TunnelDatagram(28, (
            TunnelPacket(1, bytes.fromhex("0000010100000103") + bytes.fromhex("018404")),
        )).encode(EKEY)
        self.service._clock_origin = 100.0
        with patch(
            "carbon.rebroadcaster.service.time.monotonic",
            return_value=112.39546,
        ):
            with patch.object(
                self.service.session_objects,
                "append_remote_parts",
                wraps=self.service.session_objects.append_remote_parts,
            ) as publish:
                replies = self.service.handle_datagram(guest_0184, self.guest_addr)
        guest_publications = [
            call
            for call in publish.call_args_list
            if call.args[2] == self.guest_addr
        ]
        self.assertTrue(guest_publications)
        self.assertEqual(guest_publications[-1].kwargs["offsets"], {0})

        # Retail puts the descriptor, redundant descriptor bundle,
        # GameAttributes and the existing host's offset-zero object in one
        # CommUDP datagram (chalangeinvite frame 2480;
        # create&invitejoin frame 764; quickjoin frame 86).
        bootstrap_datagrams = [
            raw
            for raw, target in replies
            if target == self.guest_addr
            and any(
                game_manager_body(item.payload).startswith(bytes.fromhex("000000001e"))
                for item in _active_messages(raw)
            )
        ]
        self.assertEqual(len(bootstrap_datagrams), 1)
        bootstrap_active = _active_messages(bootstrap_datagrams[0])
        bootstrap_bodies = [game_manager_body(item.payload) for item in bootstrap_active]
        self.assertEqual(
            [body[:6] for body in bootstrap_bodies],
            [
                bytes.fromhex("000000000022"),
                bytes.fromhex("000000000104"),
                bytes.fromhex("000000001d15"),
                bytes.fromhex("000000001e00"),
            ],
        )
        self.assertEqual([item.sequence >> 28 for item in bootstrap_active], [0, 1, 0, 0])
        self.assertEqual([item.acknowledgement for item in bootstrap_active], [0x101] * 4)
        descriptor_clock = struct.unpack(">f", bootstrap_bodies[0][13:17])[0]
        self.assertAlmostEqual(descriptor_clock, 12.39546, places=5)
        self.assertEqual(
            int.from_bytes(bootstrap_bodies[0][21:25], "big"),
            0x27FC21B5,
        )
        self.assertEqual(
            int.from_bytes(bootstrap_bodies[0][9:13], "big"),
            self.game.descriptor_handle_base + 10,
        )
        self.assertIn(bootstrap_bodies[0], bootstrap_bodies[1])

    def test_third_participant_bootstrap_splits_native_session_objects_under_1000_bytes(self) -> None:
        self.game.server_hosted = True
        self._bind_host()
        self._bind_guest()

        for addr, participant in (
            (self.host_addr, self.host_participant),
            (self.guest_addr, self.guest_participant),
        ):
            wire = self.service._wire[addr]
            wire.session_bootstrap_sent = True
            wire.session_blocks = {
                offset: _native_session_block(
                    participant.player_id,
                    participant.identity.persona,
                    offset,
                )
                for offset in (0, 0x1E4, 0x3C8)
            }

        third, _ = self.identities.login("Third")
        third_participant = self.games.enter(
            self.game.gid,
            third,
            internal_ip="192.168.1.12",
            internal_port=38286,
            invite_remote_player_id=self.host_participant.player_id,
        )
        self.assertIsNotNone(third_participant)
        assert third_participant is not None
        third_ticket = self.games.ticket(self.game, third_participant)
        third_resolution = self.games.resolve_ticket(third_ticket)
        self.assertIsNotNone(third_resolution)
        assert third_resolution is not None
        third_addr = ("192.0.2.12", 38286)
        self.service._wire[third_addr] = EndpointWireState(
            bootstrap_sent=True,
            last_client_sequence=0x101,
        )
        self.assertTrue(self.service._bind(third_addr, third_resolution))

        replies: list[tuple[bytes, tuple[str, int]]] = []
        self.service._append_session_bootstrap(
            replies,
            third_addr,
            third_resolution,
        )

        self.assertEqual(len(replies), 2)
        self.assertTrue(all(len(raw) <= 1000 for raw, _target in replies))
        active_by_datagram = [_active_messages(raw) for raw, _target in replies]
        self.assertEqual([len(active) for active in active_by_datagram], [4, 1])
        flattened = [
            game_manager_body(active.payload)
            for datagram in active_by_datagram
            for active in datagram
        ]
        self.assertEqual(
            [body[:6] for body in flattened],
            [
                bytes.fromhex("000000000022"),
                bytes.fromhex("000000000104"),
                bytes.fromhex("000000001d15"),
                bytes.fromhex("000000001e00"),
                bytes.fromhex("000000001e00"),
            ],
        )
        sequences = [
            int(active.sequence) & 0x0FFFFFFF
            for datagram in active_by_datagram
            for active in datagram
        ]
        self.assertEqual(
            sequences,
            list(range(sequences[0], sequences[0] + len(sequences))),
        )
        third_wire = self.service._wire[third_addr]
        for participant in (self.host_participant, self.guest_participant):
            source_key = (self.game.gid, participant.identity.user_id)
            self.assertEqual(
                third_wire.published_session_offsets[source_key],
                {0},
            )

    def test_confirmed_host_repetition_holds_continuations_until_quickjoin_guest_object(self) -> None:
        self.game.server_hosted = True
        self.game.properties["B-U-game_type"] = "0"
        self.game.properties["B-U-matchmaking_state"] = "1"
        self._bind_host()
        host_wire = self.service._wire[self.host_addr]
        host_wire.session_bootstrap_sent = True
        host_wire.session_confirmed = True
        host_wire.session_blocks = {
            offset: _session_block(1, "Host", offset)
            for offset in (0, 0x1E4, 0x3C8)
        }

        self._bind_guest()
        guest_0184 = TunnelDatagram(28, (
            TunnelPacket(1, bytes.fromhex("0000010100000103") + bytes.fromhex("018404")),
        )).encode(EKEY)
        self.service.handle_datagram(guest_0184, self.guest_addr)
        guest_wire = self.service._wire[self.guest_addr]
        host_key = self.service._source_key(self.service._bindings[self.host_addr])
        self.assertEqual(guest_wire.published_session_offsets[host_key], {0})

        repeated_host: list[tuple[bytes, tuple[str, int]]] = []
        self.service.invite_session.handle_complete_session_object(
            repeated_host,
            self.host_addr,
            self.service._bindings[self.host_addr],
        )
        repeated_offsets = {
            int.from_bytes(body[13:17], "big")
            for raw, target in repeated_host
            if target == self.guest_addr
            for item in _active_messages(raw)
            for body in (game_manager_body(item.payload),)
            if body.startswith(bytes.fromhex("000000001e"))
        }
        self.assertEqual(repeated_offsets, set())
        self.assertEqual(guest_wire.published_session_offsets[host_key], {0})

        guest_wire.session_blocks = {
            offset: _session_block(2, "Guest", offset)
            for offset in (0, 0x1E4, 0x3C8)
        }
        guest_complete: list[tuple[bytes, tuple[str, int]]] = []
        self.service.invite_session.handle_complete_session_object(
            guest_complete,
            self.guest_addr,
            self.service._bindings[self.guest_addr],
        )
        continuation_offsets = {
            int.from_bytes(body[13:17], "big")
            for raw, target in guest_complete
            if target == self.guest_addr
            for item in _active_messages(raw)
            for body in (game_manager_body(item.payload),)
            if body.startswith(bytes.fromhex("000000001e"))
        }
        self.assertEqual(continuation_offsets, {0x1E4, 0x3C8})

    def test_ready_lock_waits_for_quickjoin_guest_session_confirmation(self) -> None:
        self.game.server_hosted = True
        self._bind_host()
        self._bind_guest()
        for endpoint in (self.host_addr, self.guest_addr):
            self.service._wire[endpoint].ready_requested = True
        host_wire = self.service._wire[self.host_addr]
        host_wire.session_confirmed = True
        host_wire.session_blocks = {
            offset: _session_block(1, "Host", offset)
            for offset in (0, 0x1E4, 0x3C8)
        }

        replies: list[tuple[bytes, tuple[str, int]]] = []
        self.service._maybe_broadcast_ready_lock(
            replies,
            self.game,
            self.host_addr,
        )

        self.assertEqual(replies, [])
        self.assertEqual(
            self.service._race[self.game.gid].room_access.name,
            "OPEN",
        )

    def test_complete_guest_object_is_published_back_to_host_v681(self) -> None:
        self.game.properties["B-U-game_type"] = "2"
        self._bind_host()
        host_0184 = TunnelDatagram(24, (
            TunnelPacket(1, bytes.fromhex("0000010100000102") + bytes.fromhex("018404")),
        )).encode(EKEY)
        self.service.handle_datagram(host_0184, self.host_addr)

        for index, offset in enumerate((0, 0x1E4, 0x3C8), start=2):
            request = TunnelDatagram(30 + index, (
                TunnelPacket(
                    1,
                    int(index).to_bytes(4, "big")
                    + bytes.fromhex("00000102")
                    + _session_block(1, "Host", offset)
                    + b"\x04",
                ),
            )).encode(EKEY)
            self.service.handle_datagram(request, self.host_addr)

        # Host completes its endpoint-local object -> 0x02 -> reflected object
        # plus 0x03 before the guest joins, matching capture frames 448-454.
        host_token = TunnelDatagram(40, (
            TunnelPacket(1, bytes.fromhex("0000000600000108") + bytes.fromhex("0000000002427f6e98") + b"\x04"),
        )).encode(EKEY)
        host_confirm = self.service.handle_datagram(host_token, self.host_addr)
        self.assertTrue(any(
            game_manager_body(item.payload).startswith(bytes.fromhex("000000001e"))
            for raw, target in host_confirm if target == self.host_addr
            for item in _active_messages(raw)
        ))
        self.assertTrue(any(
            game_manager_body(item.payload).startswith(bytes.fromhex("0000000003"))
            for raw, target in host_confirm if target == self.host_addr
            for item in _active_messages(raw)
        ))
        host_confirmation_datagrams = [
            [game_manager_body(item.payload) for item in _active_messages(raw)]
            for raw, target in host_confirm
            if target == self.host_addr
            and any(
                game_manager_body(item.payload).startswith(
                    (bytes.fromhex("000000001e"), bytes.fromhex("0000000003"))
                )
                for item in _active_messages(raw)
            )
        ]
        self.assertEqual(len(host_confirmation_datagrams), 2)
        self.assertEqual(
            [int.from_bytes(body[13:17], "big") for body in host_confirmation_datagrams[0]],
            [0],
        )
        self.assertEqual(
            [
                int.from_bytes(body[13:17], "big")
                for body in host_confirmation_datagrams[1][:-1]
            ],
            [0x1E4, 0x3C8],
        )
        self.assertTrue(host_confirmation_datagrams[1][-1].startswith(bytes.fromhex("0000000003")))
        host_ack = TunnelDatagram(41, (
            TunnelPacket(1, bytes.fromhex("0000000700000109") + b"\x04"),
        )).encode(EKEY)
        self.service.handle_datagram(host_ack, self.host_addr)

        self.guest_participant = self.games.enter(
            self.game.gid,
            self.guest,
            internal_ip=self.guest_participant.internal_ip,
            internal_port=self.guest_participant.internal_port,
            invite_remote_player_id=self.host_participant.player_id,
        )
        assert self.guest_participant is not None
        self._bind_guest()
        guest_0184 = TunnelDatagram(28, (
            TunnelPacket(1, bytes.fromhex("0000010100000103") + bytes.fromhex("018404")),
        )).encode(EKEY)
        join_replies = self.service.handle_datagram(guest_0184, self.guest_addr)
        host_object_replies = [
            raw for raw, target in join_replies
            if target == self.guest_addr
            and any(game_manager_body(item.payload).startswith(bytes.fromhex("000000001e")) for item in _active_messages(raw))
        ]
        # The exact barrier publishes only offset zero initially.
        self.assertEqual(len(host_object_replies), 1)
        initial_host = next(
            game_manager_body(item.payload)
            for item in _active_messages(host_object_replies[0])
            if game_manager_body(item.payload).startswith(bytes.fromhex("000000001e"))
        )
        self.assertEqual(first_block_identity([initial_host]), (2, 1, "Host"))
        self.assertEqual(int.from_bytes(initial_host[13:17], "big"), 0)

        probe_replies = []
        continuation_start = self.service._wire[self.guest_addr].next_server_sequence
        for index, offset in enumerate((0, 0x1E4, 0x3C8), start=10):
            request = TunnelDatagram(50 + index, (
                TunnelPacket(
                    1,
                    int(index).to_bytes(4, "big")
                    + bytes.fromhex("00000104")
                    + _session_block(2, "Guest", offset)
                    + b"\x04",
                ),
            )).encode(EKEY)
            probe_replies = self.service.handle_datagram(request, self.guest_addr)
            if offset != 0x3C8:
                # chalangeinvite frames 2484/2485 receive no intervening
                # empty ACK. The next server sequence must remain available
                # for the first real host continuation.
                self.assertEqual(probe_replies, [])
                self.assertEqual(
                    self.service._wire[self.guest_addr].next_server_sequence,
                    continuation_start,
                )
        self.assertFalse(any(
            target == self.host_addr
            and game_manager_body(item.payload) == bytes.fromhex("0000000001")
            for raw, target in probe_replies
            for item in _active_messages(raw)
        ))
        self.assertTrue(
            self.service._wire[self.guest_addr].invite_host_barrier_pending
        )
        continuations = [
            game_manager_body(item.payload)
            for raw, target in probe_replies if target == self.guest_addr
            for item in _active_messages(raw)
            if game_manager_body(item.payload).startswith(bytes.fromhex("000000001e"))
        ]
        self.assertEqual(
            {int.from_bytes(item[13:17], "big") for item in continuations},
            {0x1E4, 0x3C8},
        )
        continuation_datagrams = [
            raw
            for raw, target in probe_replies
            if target == self.guest_addr
            and any(
                game_manager_body(item.payload).startswith(bytes.fromhex("000000001e"))
                for item in _active_messages(raw)
            )
        ]
        self.assertEqual(len(continuation_datagrams), 1)
        self.assertEqual(
            [
                int.from_bytes(game_manager_body(item.payload)[13:17], "big")
                for item in _active_messages(continuation_datagrams[0])
            ],
            [0x1E4, 0x3C8],
        )
        self.assertEqual(
            [
                item.acknowledgement
                for item in _active_messages(continuation_datagrams[0])
            ],
            [0x101, 0x101],
        )
        self.assertEqual(
            [
                item.sequence & 0x0FFFFFFF
                for item in _active_messages(continuation_datagrams[0])
            ],
            [continuation_start, continuation_start + 1],
        )
        guest_wire = self.service._wire[self.guest_addr]
        next_server_after_continuations = guest_wire.next_server_sequence

        # A pure ACK with the same sequence as the final object fragment is
        # transport progress only and must not open the application barrier.
        barrier_ack = TunnelDatagram(62, (
            TunnelPacket(
                1,
                (12).to_bytes(4, "big")
                + (next_server_after_continuations - 1).to_bytes(4, "big"),
            ),
        )).encode(EKEY)
        barrier_replies = self.service.handle_datagram(
            barrier_ack,
            self.guest_addr,
        )
        self.assertFalse(any(
            target == self.host_addr
            and game_manager_body(item.payload) == bytes.fromhex("0000000001")
            for raw, target in barrier_replies
            for item in _active_messages(raw)
        ))
        self.assertTrue(guest_wire.invite_host_barrier_pending)
        self.assertFalse(guest_wire.session_probe_sent)

        preconfirm_latency = bytes.fromhex("00000000120000000200000000")
        latency_only = TunnelDatagram(63, (
            TunnelPacket(
                1,
                (13).to_bytes(4, "big")
                + (next_server_after_continuations - 1).to_bytes(4, "big")
                + preconfirm_latency
                + b"\x04",
            ),
        )).encode(EKEY)
        latency_replies = self.service.handle_datagram(latency_only, self.guest_addr)
        self.assertTrue(any(
            target == self.host_addr
            and game_manager_body(item.payload) == bytes.fromhex("0000000001")
            for raw, target in latency_replies
            for item in _active_messages(raw)
        ))
        self.assertTrue(any(
            target == self.guest_addr
            and game_manager_body(item.payload) == bytes.fromhex("0000000001")
            and ((item.sequence >> 28) & 0xF) == 0
            for raw, target in latency_replies
            for item in _active_messages(raw)
        ))
        self.assertFalse(guest_wire.invite_host_barrier_pending)
        self.assertTrue(guest_wire.session_probe_sent)
        self.assertEqual(
            guest_wire.next_server_sequence,
            (next_server_after_continuations + 1) & 0x0FFFFFFF,
        )
        self.assertEqual(guest_wire.latest_latency_info, preconfirm_latency)

        # A slower live helper can still be inside the 0x10a -> native 0x02
        # gap when the already-connected host publishes its attributes,
        # room-wait timer and ActiveGame state.  Those peer relays must not
        # become helper sequence 0x10b.  Their authoritative snapshots are
        # cached and published by the post-confirmation Challenge commit.
        self.game.server_hosted = True
        host_binding = self.service._bindings[self.host_addr]
        early_host_replies: list[tuple[bytes, tuple[str, int]]] = []
        self.service.gameplay_relay.relay_logical_to_peers(
            early_host_replies,
            self.host_addr,
            host_binding,
            session_attributes(self.game.properties),
            footer=True,
        )
        self.service._broadcast_room_timer(
            early_host_replies,
            self.game,
            start_timer(
                current_seconds=20.0,
                duration_seconds=600.5,
                timer_id=0,
            ),
            source=self.host_addr,
        )
        self.service.gameplay_relay.reflect_logical_to_room(
            early_host_replies,
            self.host_addr,
            host_binding,
            bytes.fromhex("000000001c00000000000e"),
        )
        self.service.gameplay_relay.reflect_logical_to_room(
            early_host_replies,
            self.guest_addr,
            self.service._bindings[self.guest_addr],
            bytes.fromhex("000000001c000567756573740000000d"),
        )
        self.assertFalse(any(
            target == self.guest_addr
            for _raw, target in early_host_replies
        ))
        self.assertEqual(
            guest_wire.next_server_sequence,
            (next_server_after_continuations + 1) & 0x0FFFFFFF,
        )
        self.assertEqual(
            guest_wire.preconfirm_deferred_types,
            {
                int(OLMessageType.START_TIMER),
                int(OLMessageType.GAME_ATTRIBUTES),
                int(OLMessageType.ACTIVE_GAME_MESSAGE),
            },
        )
        self.game.server_hosted = False

        transport_only = TunnelDatagram(64, (
            TunnelPacket(
                1,
                (14).to_bytes(4, "big")
                + (next_server_after_continuations - 1).to_bytes(4, "big"),
            ),
        )).encode(EKEY)
        self.assertEqual(
            self.service.handle_datagram(transport_only, self.guest_addr),
            [],
        )
        self.assertEqual(
            guest_wire.next_server_sequence,
            (next_server_after_continuations + 1) & 0x0FFFFFFF,
        )
        self.assertFalse(any(
            target == self.guest_addr
            and game_manager_body(item.payload) == bytes.fromhex("0000000001")
            for raw, target in probe_replies
            for item in _active_messages(raw)
        ))

        # The guest's 0x02 publishes its complete object to the host before
        # the host answers the later barrier, matching invite frames 2488-2496.
        guest_token = TunnelDatagram(80, (
            TunnelPacket(1, bytes.fromhex("0000001000000115") + bytes.fromhex("0000000002427f0000") + b"\x04"),
        )).encode(EKEY)
        final_replies = self.service.handle_datagram(guest_token, self.guest_addr)

        guest_confirmation_datagrams = [
            [game_manager_body(item.payload) for item in _active_messages(raw)]
            for raw, target in final_replies
            if target == self.guest_addr
            and any(
                game_manager_body(item.payload).startswith(
                    (bytes.fromhex("000000001e"), bytes.fromhex("0000000003"))
                )
                for item in _active_messages(raw)
            )
        ]
        self.assertEqual(len(guest_confirmation_datagrams), 2)
        self.assertEqual(
            [int.from_bytes(body[13:17], "big") for body in guest_confirmation_datagrams[0]],
            [0],
        )
        self.assertEqual(
            [
                int.from_bytes(body[13:17], "big")
                for body in guest_confirmation_datagrams[1][:-1]
            ],
            [0x1E4, 0x3C8],
        )
        self.assertTrue(guest_confirmation_datagrams[1][-1].startswith(bytes.fromhex("0000000003")))

        host_guest_object_datagrams = [
            _active_messages(raw)
            for raw, target in final_replies
            if target == self.host_addr
            and any(
                game_manager_body(item.payload).startswith(bytes.fromhex("000000001e"))
                for item in _active_messages(raw)
            )
        ]
        self.assertEqual(len(host_guest_object_datagrams), 2)
        self.assertEqual(
            [
                int.from_bytes(game_manager_body(item.payload)[13:17], "big")
                for item in host_guest_object_datagrams[0]
            ],
            [0],
        )
        self.assertEqual(
            [
                int.from_bytes(game_manager_body(item.payload)[13:17], "big")
                for item in host_guest_object_datagrams[1]
            ],
            [0x1E4, 0x3C8],
        )
        self.assertEqual(
            [item.acknowledgement for item in host_guest_object_datagrams[1]],
            [self.service._wire[self.host_addr].last_client_sequence] * 2,
        )
        guest_ack = TunnelDatagram(81, (
            TunnelPacket(1, bytes.fromhex("0000001100000118") + b"\x04"),
        )).encode(EKEY)
        self.service.handle_datagram(guest_ack, self.guest_addr)

        guest_remote_blocks = [
            game_manager_body(item.payload)
            for raw, target in final_replies if target == self.host_addr
            for item in _active_messages(raw)
            if game_manager_body(item.payload).startswith(bytes.fromhex("000000001e"))
        ]
        self.assertEqual(len(guest_remote_blocks), 3)
        # Receiver-local ids must not alias the host's reflected local object.
        # With local id 2 reserved, the reciprocal guest object is id 3.
        self.assertEqual(first_block_identity(guest_remote_blocks), (3, 2, "Guest"))
        guest_remote_first = next(item for item in guest_remote_blocks if int.from_bytes(item[13:17], "big") == 0)
        self.assertEqual(int.from_bytes(guest_remote_first[39:43], "big"), 1)
        self.assertEqual({int.from_bytes(item[13:17], "big") for item in guest_remote_blocks}, {0, 0x1E4, 0x3C8})

        # Host 0x02 confirms the barrier without republishing continuations.
        host_barrier_token = TunnelDatagram(70, (
            TunnelPacket(1, bytes.fromhex("0000000800000110") + bytes.fromhex("000000000242800000") + b"\x04"),
        )).encode(EKEY)
        continuation_replies = self.service.handle_datagram(host_barrier_token, self.host_addr)
        duplicate_continuations = [
            game_manager_body(item.payload)
            for raw, target in continuation_replies if target == self.guest_addr
            for item in _active_messages(raw)
            if game_manager_body(item.payload).startswith(bytes.fromhex("000000001e"))
        ]
        self.assertEqual(duplicate_continuations, [])

        # V743 keeps the host token as a barrier only.  The room context waits
        # for the later host state-7 and helper allocation window; emitting it
        # here recreates the premature invite commit seen before frame 2779.
        context_targets = {
            target for raw, target in [*continuation_replies, *final_replies]
            if any(game_manager_body(item.payload).startswith(bytes.fromhex("000000001c")) for item in _active_messages(raw))
        }
        self.assertEqual(context_targets, set())
        self.assertFalse(any(
            game_manager_body(item.payload).startswith(bytes.fromhex("000000001b"))
            for raw, _target in [*continuation_replies, *final_replies]
            for item in _active_messages(raw)
        ))

    def test_first_dedicated_participant_keeps_standalone_session_probe(self) -> None:
        self.game.server_hosted = True
        self.game.properties["B-U-game_type"] = "2"
        self._bind_host()
        self.assertEqual(self.host_participant.invite_remote_player_id, 0)

        replies = []
        for index, offset in enumerate((0, 0x1E4, 0x3C8), start=2):
            request = TunnelDatagram(30 + index, (
                TunnelPacket(
                    1,
                    int(index).to_bytes(4, "big")
                    + bytes.fromhex("00000102")
                    + _session_block(1, "Host", offset)
                    + b"\x04",
                ),
            )).encode(EKEY)
            replies = self.service.handle_datagram(request, self.host_addr)

        self.assertTrue(any(
            target == self.host_addr
            and game_manager_body(item.payload) == bytes.fromhex("0000000001")
            for raw, target in replies
            for item in _active_messages(raw)
        ))
        self.assertTrue(self.service._wire[self.host_addr].clock_probe_sent)


if __name__ == "__main__":
    unittest.main()
