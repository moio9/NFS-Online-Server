"""Session fragments remain correct when UDP changes their arrival order."""

import unittest

from carbon.core.config import Endpoint
from carbon.gamemanager.session_object import (
    SessionObjectError, is_session_object_complete, parse_session_object_block,
    rewrite_for_receiver,
)
from carbon.rebroadcaster.service import CarbonRebroadcasterService
from carbon.rebroadcaster.state import EndpointWireState
from carbon.tests.test_carbon_gamemanager_session import _native_session_block
from carbon.theater.directory import CarbonGameDirectory


def block(object_id, offset, *, total_size=None):
    value = bytearray(_native_session_block(42, "Synthetic", offset))
    value[5:9] = object_id.to_bytes(4, "big")
    if total_size is not None:
        value[9:13] = total_size.to_bytes(4, "big")
    return bytes(value)


class SessionReorderTests(unittest.TestCase):
    def setUp(self):
        self.service = CarbonRebroadcasterService(CarbonGameDirectory(Endpoint("127.0.0.1", 19118)))
        self.addr = ("127.0.0.1", 41000)
        self.wire = EndpointWireState()
        self.service._wire[self.addr] = self.wire

    def ingest(self, value):
        self.service._ingest_session_blocks([], self.addr, self.wire, value)

    def test_new_generation_continuations_before_zero_are_not_lost_or_mixed(self):
        offsets = (0, 0x1E4, 0x3C8)
        for offset in offsets:
            self.ingest(block(1, offset))
        self.assertTrue(is_session_object_complete(self.wire.session_blocks.values()))
        for offset in offsets[1:]:
            self.ingest(block(2, offset))
        self.assertEqual(self.wire.session_object_id, 1)
        self.assertEqual({parse_session_object_block(item).object_id
                          for item in self.wire.session_blocks.values()}, {1})
        self.ingest(block(2, 0))
        self.assertEqual(self.wire.session_object_id, 2)
        self.assertTrue(is_session_object_complete(self.wire.session_blocks.values()))
        self.assertEqual({parse_session_object_block(item).object_id
                          for item in self.wire.session_blocks.values()}, {2})
        self.assertEqual(len(rewrite_for_receiver(self.wire.session_blocks.values(),
                                                 remote_object_id=3, remote_slot=0)), 3)
        # Late retransmissions from the retired generation cannot revert it.
        for offset in offsets:
            self.ingest(block(1, offset))
        self.assertEqual(self.wire.session_object_id, 2)
        self.assertEqual({parse_session_object_block(item).object_id
                          for item in self.wire.session_blocks.values()}, {2})

    def test_first_generation_can_arrive_out_of_order_and_sizes_must_match(self):
        self.ingest(block(7, 0x1E4))
        self.ingest(block(7, 0x3C8, total_size=0x455))
        self.assertEqual(self.wire.session_blocks, {})
        self.ingest(block(7, 0))
        self.assertEqual(set(self.wire.session_blocks), {0, 0x1E4})
        self.ingest(block(7, 0x3C8))
        self.assertTrue(is_session_object_complete(self.wire.session_blocks.values()))

    def test_unknown_generation_buffer_is_bounded(self):
        self.ingest(block(1, 0))
        for object_id in range(2, 9):
            self.ingest(block(object_id, 0x1E4))
        self.assertLessEqual(len(self.wire.pending_session_fragments), 4)
        self.assertNotIn(2, self.wire.pending_session_fragments)
        self.ingest(block(8, 0))
        self.assertEqual(set(self.wire.session_blocks), {0, 0x1E4})

    def test_continuation_with_wrong_size_after_zero_is_not_assembled(self):
        self.ingest(block(7, 0))
        self.ingest(block(7, 0x1E4, total_size=0x455))
        self.ingest(block(7, 0x3C8))
        self.assertFalse(is_session_object_complete(self.wire.session_blocks.values()))
        self.ingest(block(7, 0x1E4))
        self.assertTrue(is_session_object_complete(self.wire.session_blocks.values()))

    def test_inconsistent_retransmitted_zero_does_not_replace_current_object(self):
        for offset in (0, 0x1E4, 0x3C8):
            self.ingest(block(7, offset))
        original = dict(self.wire.session_blocks)
        self.ingest(block(7, 0, total_size=0x455))
        self.assertEqual(self.wire.session_blocks, original)

    def test_truncated_first_fragment_cannot_complete_an_object(self):
        first = bytearray(block(7, 0)[:24])
        first[21:23] = (1).to_bytes(2, "big")
        self.assertIsNone(parse_session_object_block(first))
        self.ingest(first)
        self.ingest(block(7, 0x1E4))
        self.ingest(block(7, 0x3C8))
        self.assertFalse(is_session_object_complete(self.wire.session_blocks.values()))
        self.ingest(block(7, 0))
        self.assertTrue(is_session_object_complete(self.wire.session_blocks.values()))

    def test_completeness_rejects_mixed_object_ids_or_sizes(self):
        first = block(7, 0)
        last = block(7, 0x3C8)
        for middle in (block(8, 0x1E4), block(7, 0x1E4, total_size=0x455)):
            fragments = (first, middle, last)
            self.assertFalse(is_session_object_complete(fragments))
            with self.assertRaises(SessionObjectError):
                rewrite_for_receiver(fragments, remote_object_id=9, remote_slot=0)


if __name__ == "__main__":
    unittest.main()
