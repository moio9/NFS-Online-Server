"""Direct invariants for the local endpoint outbound publisher."""

import unittest

from carbon.rebroadcaster.outbound import EndpointPublisher
from carbon.rebroadcaster.state import EndpointWireState
from carbon.transport.commudp import CommUDPActive, parse_channel_one
from carbon.transport.prototunnel import (
    TunnelPacket,
    decode_datagram,
)


DEFAULT_KEY = b"default-outbound-key"
ENDPOINT_KEY = b"endpoint-outbound-key"
DESTINATION = ("198.51.100.20", 20000)


class EndpointPublisherTests(unittest.TestCase):
    def _publisher(
        self,
        wire: EndpointWireState,
    ) -> EndpointPublisher:
        return EndpointPublisher(
            DEFAULT_KEY,
            {DESTINATION: wire},
            {},
        )

    def test_active_body_uses_endpoint_key_and_wraps_reliable_sequence(self) -> None:
        wire = EndpointWireState(
            tunnel_key=ENDPOINT_KEY,
            next_offset_words=9,
            next_server_sequence=0x0FFFFFFF,
            last_client_sequence=0x123,
        )
        publisher = self._publisher(wire)
        replies = []

        sequence = publisher.append_active_body(
            replies,
            DESTINATION,
            b"logical-body\x04",
        )

        self.assertEqual(sequence, 0x0FFFFFFF)
        self.assertEqual(wire.next_server_sequence, 0)
        decoded = decode_datagram(replies[0][0], ENDPOINT_KEY)
        active = parse_channel_one(decoded.packets[0])
        self.assertIsInstance(active, CommUDPActive)
        self.assertEqual(active.sequence, 0x0FFFFFFF)
        self.assertEqual(active.acknowledgement, 0x123)
        self.assertEqual(active.payload[8:], b"logical-body\x04")
        self.assertGreater(wire.next_offset_words, 9)

    def test_transport_ack_does_not_consume_reliable_sequence(self) -> None:
        wire = EndpointWireState(
            tunnel_key=ENDPOINT_KEY,
            next_server_sequence=0x155,
            last_client_sequence=0x144,
        )
        publisher = self._publisher(wire)
        replies = []

        publisher.append_transport_ack(replies, DESTINATION)
        publisher.append_transport_ack(replies, DESTINATION)

        self.assertEqual(wire.next_server_sequence, 0x155)
        self.assertEqual(len(replies), 2)
        for raw, _destination in replies:
            decoded = decode_datagram(raw, ENDPOINT_KEY)
            active = parse_channel_one(decoded.packets[0])
            self.assertIsInstance(active, CommUDPActive)
            self.assertEqual(active.sequence, 0x155)
            self.assertEqual(active.acknowledgement, 0x144)

    def test_active_bodies_share_one_datagram_and_contiguous_sequences(self) -> None:
        wire = EndpointWireState(
            tunnel_key=ENDPOINT_KEY,
            next_server_sequence=0x10,
            last_client_sequence=0xABC,
        )
        publisher = self._publisher(wire)
        replies = []

        publisher.append_active_bodies(
            replies,
            DESTINATION,
            (b"first\x04", b"second\x04"),
        )

        self.assertEqual(len(replies), 1)
        decoded = decode_datagram(replies[0][0], ENDPOINT_KEY)
        active = tuple(parse_channel_one(packet) for packet in decoded.packets)
        self.assertTrue(all(isinstance(item, CommUDPActive) for item in active))
        self.assertEqual([item.sequence for item in active], [0x10, 0x11])
        self.assertEqual([item.acknowledgement for item in active], [0xABC, 0xABC])
        self.assertEqual([item.payload[8:] for item in active], [b"first\x04", b"second\x04"])
        self.assertEqual(wire.next_server_sequence, 0x12)

        old_offset = wire.next_offset_words
        publisher.append_active_bodies(replies, DESTINATION, ())
        self.assertEqual(len(replies), 1)
        self.assertEqual(wire.next_server_sequence, 0x12)
        self.assertEqual(wire.next_offset_words, old_offset)

    def test_record_batch_and_packet_batching_preserve_native_windows(self) -> None:
        wire = EndpointWireState(
            tunnel_key=ENDPOINT_KEY,
            next_server_sequence=0x20,
            last_client_sequence=0x33,
        )
        publisher = self._publisher(wire)
        replies = []

        latest = publisher.append_active_record_batch(
            replies,
            DESTINATION,
            (b"oldest", b"newest"),
        )

        self.assertEqual(latest, 0x21)
        self.assertEqual(wire.next_server_sequence, 0x22)
        decoded = decode_datagram(replies[0][0], ENDPOINT_KEY)
        active = parse_channel_one(decoded.packets[0])
        self.assertIsInstance(active, CommUDPActive)
        self.assertEqual(active.sequence, 0x10000021)
        self.assertEqual(
            active.payload[8:],
            publisher.commudp_aggregate_payload((b"newest", b"oldest")),
        )

        packets = tuple(TunnelPacket(1, bytes((index,))) for index in range(9))
        reliable_before = wire.next_server_sequence
        batch_replies = []
        self.assertEqual(
            publisher.append_packet_batches(
                batch_replies,
                packets,
                DESTINATION,
            ),
            2,
        )
        self.assertEqual(wire.next_server_sequence, reliable_before)
        self.assertEqual(
            [
                len(decode_datagram(raw, ENDPOINT_KEY).packets)
                for raw, _destination in batch_replies
            ],
            [8, 1],
        )


if __name__ == "__main__":
    unittest.main()
