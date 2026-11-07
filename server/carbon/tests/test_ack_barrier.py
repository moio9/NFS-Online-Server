import unittest

from carbon.rebroadcaster.ack_barrier import AckBarrier


class AckBarrierTests(unittest.TestCase):
    def test_retries_flow_and_later_records_release_in_order(self):
        gate = AckBarrier[str]()
        a = ("127.0.0.1", 41000)
        self.assertTrue(gate.open(a, 0x103))
        self.assertFalse(gate.open(a, 0x104))
        self.assertFalse(gate.defer(a, (0x103,), b"join", "join retry"))
        self.assertFalse(gate.defer(a, (0x102,), b"older", "old retry"))
        self.assertTrue(gate.defer(a, (0x104,), b"init", "init"))
        self.assertTrue(gate.defer(a, (0x105,), b"peer", "peer"))
        self.assertTrue(gate.defer(a, (0x104,), b"init", "duplicate"))
        self.assertIsNone(gate.acknowledge(a, 0x102))
        self.assertEqual(gate.acknowledge(a, 0x103), ("init", "peer"))
        self.assertFalse(gate.pending(a))
        self.assertFalse(gate.defer(a, (0x106,), b"next", "next"))

    def test_wrap_destinations_and_cancel_are_independent(self):
        gate = AckBarrier[str]()
        a, b = ("127.0.0.1", 1), ("127.0.0.1", 2)
        gate.open(a, 0x0FFFFFFF)
        gate.open(b, 9)
        self.assertTrue(gate.defer(a, (0,), b"a", "a"))
        self.assertTrue(gate.defer(b, (10,), b"b", "b"))
        self.assertIsNone(gate.acknowledge(a, 0x0FFFFFFE))
        self.assertEqual(gate.acknowledge(a, 0), ("a",))
        gate.cancel(b)
        self.assertIsNone(gate.acknowledge(b, 9))
        self.assertTrue(gate.open(b, 12))
        self.assertEqual(gate.acknowledge(b, 12), ())

    def test_mixed_aggregate_waits_but_predecessor_can_be_retried(self):
        gate = AckBarrier[str]()
        address = ("127.0.0.1", 1)
        gate.open(address, 0x103)
        self.assertTrue(gate.defer(address, (0x103, 0x104), b"aggregate", "aggregate"))
        self.assertFalse(gate.defer(address, (0x103,), b"self-join", "self-join"))
        self.assertEqual(gate.acknowledge(address, 0x103), ("aggregate",))

    def test_new_control_and_virtual_sequence_wait_but_old_wire_retry_passes(self):
        gate = AckBarrier[str]()
        address = ("127.0.0.1", 1)
        gate.open(address, 0x103)
        self.assertTrue(gate.defer(address, (), b"control", "control", newly_built=True))
        self.assertTrue(gate.defer(address, (0x80,), b"virtual", "virtual", newly_built=True))
        self.assertFalse(gate.defer(address, (0x103,), b"retry", "retry"))
        self.assertEqual(gate.acknowledge(address, 0x103), ("control", "virtual"))

    def test_overflow_never_releases_a_partial_sequence(self):
        gate = AckBarrier[str](max_records=1, max_bytes=4)
        a = ("127.0.0.1", 1)
        gate.open(a, 20)
        self.assertTrue(gate.defer(a, (21,), b"aa", "first"))
        self.assertFalse(gate.failed(a))
        self.assertTrue(gate.defer(a, (22,), b"bb", "overflow"))
        self.assertTrue(gate.failed(a))
        self.assertIsNone(gate.acknowledge(a, 20))
        self.assertTrue(gate.pending(a))
        self.assertTrue(gate.defer(a, (23,), b"cc", "discarded"))
        gate.cancel(a)
        self.assertFalse(gate.failed(a))
        self.assertTrue(gate.open(a, 30))
        self.assertTrue(gate.defer(a, (31,), b"12345", "too large"))
        self.assertIsNone(gate.acknowledge(a, 30))


if __name__ == "__main__":
    unittest.main()
