"""Clear Xbox framing is explicitly local and still requires an Xbox ticket."""

import unittest

from carbon.accounts.identity import ClientPlatform, IdentityStore
from carbon.core.config import Endpoint
from carbon.rebroadcaster.confirmations import ConfirmationManager
from carbon.rebroadcaster.outbound import EndpointPublisher, ReplyBuffer
from carbon.rebroadcaster.retry import RetryPolicy
from carbon.rebroadcaster.service import CarbonRebroadcasterService
from carbon.rebroadcaster.state import EndpointWireState
from carbon.theater.directory import CarbonGameDirectory
from carbon.transport.prototunnel import (
    ProtoTunnelError, TunnelDatagram, TunnelPacket, decode_clear_datagram,
    decode_datagram, encode_clear_datagram,
)


KEY = b"9181081919"
CONNECT = TunnelDatagram(0, (
    TunnelPacket(7, bytes.fromhex("000000071000")),
    TunnelPacket(1, bytes.fromhex("00000001dcf7b035")),
))


def ticket_datagram(ticket):
    ticket = ticket.encode("ascii")
    body = bytes.fromhex("01808004800000") + bytes((len(ticket),)) + ticket
    return TunnelDatagram(5, (
        TunnelPacket(1, bytes.fromhex("00000002dcf7b035")),
        TunnelPacket(1, bytes.fromhex("00000100000000ff") + body + b"\x04"),
    ))


class XboxClearTransportTests(unittest.TestCase):
    def setUp(self):
        self.identities = IdentityStore()
        self.games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))

    def member(self, name, platform):
        identity, _ = self.identities.login(name, client_platform=platform)
        game = self.games.create(identity)
        return game, game.participants[identity.user_id]

    def test_codec_round_trip_and_native_limits(self):
        self.assertEqual(decode_clear_datagram(encode_clear_datagram(CONNECT)), CONNECT)
        wrapped = TunnelDatagram(0x10000, CONNECT.packets)
        self.assertEqual(decode_clear_datagram(encode_clear_datagram(wrapped)), CONNECT)
        # The previous local adapter decrypted first and then wrote the clear
        # packet table. Check the new encoder preserves that channel ordering.
        unordered = TunnelDatagram(0, (
            TunnelPacket(0, b"clear"), TunnelPacket(2, b"secure"),
            TunnelPacket(7, b"123456"), TunnelPacket(1, b"active"),
        ))
        encrypted_order = decode_datagram(unordered.encode(KEY), KEY)
        self.assertEqual(decode_clear_datagram(encode_clear_datagram(unordered)), encrypted_order)
        for raw in (b"", b"\x00" * 1001, b"\x00\x00\x00\x71\x00"):
            with self.subTest(size=len(raw)), self.assertRaises(ProtoTunnelError):
                decode_clear_datagram(raw)
        for packets in ((TunnelPacket(7, b"bad"),), (TunnelPacket(9, b"x"),),
                        tuple(TunnelPacket(1, b"x") for _ in range(9))):
            with self.subTest(packets=len(packets)), self.assertRaises(ProtoTunnelError):
                encode_clear_datagram(TunnelDatagram(0, packets))

    def test_clear_handshake_is_opt_in_and_loopback_only(self):
        raw = encode_clear_datagram(CONNECT)
        default = CarbonRebroadcasterService(self.games)
        self.assertEqual(default.handle_datagram(raw, ("127.0.0.1", 41000)), [])
        service = CarbonRebroadcasterService(self.games, allow_clear_loopback=True)
        self.assertEqual(service.handle_datagram(raw, ("192.0.2.10", 41000)), [])
        address = ("127.0.0.1", 41000)
        replies = service.handle_datagram(raw, address)
        self.assertEqual(len(replies), 1)
        self.assertEqual(len(decode_clear_datagram(replies[0][0]).packets), 3)
        self.assertTrue(service._wire[address].clear_tunnel)
        before = service.stats().datagrams_rejected
        self.assertEqual(service.handle_datagram(b"\x00", address), [])
        self.assertEqual(service.stats().datagrams_rejected, before + 1)
        self.assertTrue(service._wire[address].clear_tunnel)

    def test_ticket_binding_reconnect_and_pc_isolation(self):
        xbox_game, xbox = self.member("Xbox", ClientPlatform.XBOX360)
        pc_game, pc = self.member("PC", ClientPlatform.UNKNOWN)
        service = CarbonRebroadcasterService(self.games, allow_clear_loopback=True)
        xbox_addr, pc_addr, wrong_addr = (("127.0.0.1", port) for port in (41000, 41001, 41002))
        xbox_ticket = self.games.ticket(xbox_game, xbox)
        pc_ticket = self.games.ticket(pc_game, pc)
        service.handle_datagram(encode_clear_datagram(CONNECT), xbox_addr)
        service.handle_datagram(encode_clear_datagram(ticket_datagram(xbox_ticket)), xbox_addr)
        self.assertIsNotNone(service.binding(xbox_addr))
        reconnect = service.handle_datagram(encode_clear_datagram(CONNECT), xbox_addr)
        self.assertEqual(len(reconnect), 1)
        decode_clear_datagram(reconnect[0][0])
        self.assertIsNone(service.binding(xbox_addr))
        service.handle_datagram(encode_clear_datagram(ticket_datagram(xbox_ticket)), xbox_addr)
        self.assertIsNotNone(service.binding(xbox_addr))
        service.handle_datagram(CONNECT.encode(KEY), pc_addr)
        replies = service.handle_datagram(ticket_datagram(pc_ticket).encode(KEY), pc_addr)
        self.assertIsNotNone(service.binding(pc_addr))
        self.assertFalse(service._wire[pc_addr].clear_tunnel)
        for reply, _ in replies:
            decode_datagram(reply, KEY)
        service.handle_datagram(encode_clear_datagram(CONNECT), wrong_addr)
        service.handle_datagram(encode_clear_datagram(ticket_datagram(pc_ticket)), wrong_addr)
        self.assertIsNone(service.binding(wrong_addr))

    def test_clear_predecessor_retry_and_deferred_bootstrap(self):
        address = ("127.0.0.1", 41000)
        wires = {address: EndpointWireState(clear_tunnel=True)}
        confirmations = ConfirmationManager(wires, retry_policy=RetryPolicy(0.5, 1.0, 2, 5.0))
        publisher = EndpointPublisher(KEY, wires, {}, confirmations=confirmations)
        initial = ReplyBuffer()
        sequence = publisher.append_active_body(initial, address, b"self-join", confirmation="join")
        self.assertTrue(publisher.join_ack_barrier.open(address, sequence))
        publisher.append_active_body(initial, address, b"bootstrap", confirmation="bootstrap")
        first, = initial.emitted()
        self.assertEqual(len(decode_clear_datagram(first[0]).packets), 1)
        window, = confirmations.pending(address)
        retried = confirmations.poll(now=window.retry.retry_not_before)
        self.assertEqual(retried, [first])
        released = ReplyBuffer()
        self.assertFalse(publisher.release_join_ack(released, address, sequence - 1))
        self.assertTrue(publisher.release_join_ack(released, address, sequence))
        delayed, = released.emitted()
        self.assertEqual(len(decode_clear_datagram(delayed[0]).packets), 1)


if __name__ == "__main__":
    unittest.main()
