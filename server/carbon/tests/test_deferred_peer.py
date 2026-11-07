"""Peer publications wait for an Xbox joiner's session without sequence gaps."""

from dataclasses import replace
import struct
import unittest

from carbon.accounts.identity import ClientPlatform
from carbon.gamemanager.race_session import latency_info, session_attributes
from carbon.gamemanager.race_state import RacePhase
from carbon.tests.test_carbon_race_flow import _finalized_flow, _logical_replies


class DeferredPeerTests(unittest.TestCase):
    def setUp(self):
        self.flow = _finalized_flow()
        self.service = self.flow.service
        self.host = self.flow.host_addr
        self.guest = self.flow.guest_addr
        old = self.service._bindings[self.guest]
        identity = replace(old.participant.identity, client_platform=ClientPlatform.XBOX360)
        participant = replace(old.participant, identity=identity)
        self.service._bindings[self.guest] = replace(old, participant=participant)
        self.wire = self.service._wire[self.guest]
        self.wire.session_confirmed = False
        self.wire.session_blocks.clear()
        self.wire.bootstrap_sent = False
        self.service.confirmations.clear_endpoint(self.guest)

    def test_peer_updates_and_timer_wait_then_release_in_order(self):
        binding = self.service._bindings[self.host]
        host_wire = self.service._wire[self.host]
        before_seq, before_offset = self.wire.next_server_sequence, self.wire.next_offset_words
        latency = latency_info(binding.participant.player_id)
        attrs = session_attributes(binding.game.properties)
        replies = []
        self.service.gameplay_relay.relay_logical_to_peers(
            replies, self.host, binding, latency, footer=False, confirmation="latency",
        )
        self.service.gameplay_relay.relay_logical_to_peers(
            replies, self.host, binding, attrs, footer=True, confirmation="attributes",
        )
        race = self.service._race[binding.game.gid]
        race.phase = RacePhase.SESSION_SETUP
        timer = bytes.fromhex("000000001b") + struct.pack(">Iff", 2, 10.0, 30.5)
        self.service._broadcast_room_timer(replies, binding.game, timer, source=self.host)
        self.assertFalse(any(addr == self.guest for _raw, addr in replies))
        self.assertEqual(self.wire.next_server_sequence, before_seq)
        self.assertEqual(self.wire.next_offset_words, before_offset)
        self.assertEqual(len(self.wire.deferred_peer_publications), 3)
        self.assertGreater(host_wire.next_server_sequence, 0)

        # Simulate first accepted fragment; service's normal finishing path
        # releases held messages, assigns sequences and opens retry windows.
        from carbon.tests.test_carbon_gamemanager_session import _session_block
        self.wire.session_blocks[0] = _session_block(2, "Guest", 0)
        released = []
        self.service._finish_bound_datagram(
            released, self.guest, self.service._bindings[self.guest], self.wire,
            [], None, 0,
        )
        logicals = _logical_replies(released, self.guest)
        self.assertEqual([body[4] for body in logicals], [0x12, 0x1D, 0x1B])
        self.assertEqual(self.wire.next_server_sequence, before_seq + 3)
        self.assertEqual(self.wire.deferred_peer_publications, [])
        self.assertEqual(self.wire.deferred_peer_bytes, 0)
        labels = [window.label for window in self.service.confirmations.pending(self.guest)]
        self.assertEqual(labels, ["latency", "attributes", "room-timer"])
        self.assertIsNotNone(self.wire.match_timer_retry)

    def test_departed_source_is_not_replayed_and_endpoint_cleanup_drops_queue(self):
        binding = self.service._bindings[self.host]
        self.assertTrue(self.service.outbound.defer_uninitialized_peer(
            self.host, self.guest, b"old", confirmation="old",
        ))
        self.service._bindings.pop(self.host)
        self.wire.session_blocks[0] = b"synthetic"
        self.assertEqual(self.service.outbound.release_deferred_peer([], self.guest), 0)
        self.assertEqual(self.wire.deferred_peer_bytes, 0)
        self.assertEqual(self.wire.deferred_peer_publications, [])

    def test_retransmitted_source_ticket_keeps_queued_publication(self):
        binding = self.service._bindings[self.host]
        self.assertTrue(self.service.outbound.defer_uninitialized_peer(
            self.host, self.guest, b"still-current", confirmation="peer",
        ))
        # Ticket resolution returns a fresh wrapper even for the same member.
        self.assertTrue(self.service._bind(self.host, replace(binding)))
        self.wire.session_blocks[0] = b"synthetic"
        replies = []
        self.assertEqual(self.service.outbound.release_deferred_peer(replies, self.guest), 1)
        self.assertEqual(len(replies), 1)
        self.assertEqual(self.wire.deferred_peer_bytes, 0)

    def test_replaced_source_wire_does_not_replay_old_publication(self):
        from carbon.rebroadcaster.state import EndpointWireState
        self.assertTrue(self.service.outbound.defer_uninitialized_peer(
            self.host, self.guest, b"old-connection",
        ))
        self.service._wire[self.host] = EndpointWireState()
        self.wire.session_blocks[0] = b"synthetic"
        self.assertEqual(self.service.outbound.release_deferred_peer([], self.guest), 0)
        self.assertEqual(self.wire.deferred_peer_bytes, 0)

    def test_refreshed_source_membership_keeps_queued_publication(self):
        binding = self.service._bindings[self.host]
        self.assertTrue(self.service.outbound.defer_uninitialized_peer(
            self.host, self.guest, b"current-participant",
        ))
        refreshed = self.service.games.enter(
            binding.game.gid, binding.participant.identity, internal_ip="192.0.2.99", internal_port=9090,
        )
        self.assertIsNot(refreshed, binding.participant)
        self.assertTrue(self.service._bind(self.host, replace(binding, participant=refreshed)))
        self.wire.session_blocks[0] = b"synthetic"
        self.assertEqual(self.service.outbound.release_deferred_peer([], self.guest), 1)

    def test_new_source_membership_does_not_replay_old_publication(self):
        binding = self.service._bindings[self.host]
        self.assertTrue(self.service.outbound.defer_uninitialized_peer(
            self.host, self.guest, b"old-participant",
        ))
        replacement = replace(binding.participant, entered_at=binding.participant.entered_at + 1)
        self.service._bindings[self.host] = replace(binding, participant=replacement)
        binding.game.participants[binding.participant.identity.user_id] = replacement
        self.wire.session_blocks[0] = b"synthetic"
        self.assertEqual(self.service.outbound.release_deferred_peer([], self.guest), 0)
        self.assertEqual(self.wire.deferred_peer_bytes, 0)

    def test_removed_source_membership_does_not_replay_old_publication(self):
        binding = self.service._bindings[self.host]
        self.assertTrue(self.service.outbound.defer_uninitialized_peer(
            self.host, self.guest, b"departed-participant",
        ))
        binding.game.participants.pop(binding.participant.identity.user_id)
        self.wire.session_blocks[0] = b"synthetic"
        self.assertEqual(self.service.outbound.release_deferred_peer([], self.guest), 0)

    def test_join_ack_barrier_prevents_early_release(self):
        publisher = self.service.outbound
        self.assertTrue(publisher.defer_uninitialized_peer(self.host, self.guest, b"message"))
        self.wire.session_blocks[0] = b"synthetic"
        self.assertTrue(publisher.join_ack_barrier.open(self.guest, 0x123))
        self.assertEqual(publisher.release_deferred_peer([], self.guest), 0)
        from carbon.rebroadcaster.outbound import ReplyBuffer
        self.assertTrue(publisher.release_join_ack(ReplyBuffer(), self.guest, 0x123))
        replies = []
        self.assertEqual(publisher.release_deferred_peer(replies, self.guest), 1)
        self.assertEqual(len(replies), 1)

    def test_released_publication_retries_identical_wire_until_acknowledged(self):
        publisher = self.service.outbound
        self.assertTrue(publisher.defer_uninitialized_peer(
            self.host, self.guest, b"reliable-message", confirmation="peer-update",
        ))
        self.assertEqual(self.service.confirmations.pending(self.guest), ())
        self.wire.session_blocks[0] = b"synthetic"
        replies = []
        self.assertEqual(publisher.release_deferred_peer(replies, self.guest), 1)
        window, = self.service.confirmations.pending(self.guest)
        self.assertEqual(window.label, "peer-update")
        retries = self.service.confirmations.poll(now=window.retry.retry_not_before)
        self.assertEqual([item for item in retries if item[1] == self.guest], replies)
        self.service.confirmations.acknowledge(self.guest, window.final_sequence)
        self.assertEqual(self.service.confirmations.pending(self.guest), ())
        retries = self.service.confirmations.poll(now=window.retry.retry_not_before)
        self.assertFalse(any(item[1] == self.guest for item in retries))

    def test_disconnected_destination_discards_its_queued_publications(self):
        self.assertTrue(self.service.outbound.defer_uninitialized_peer(
            self.host, self.guest, b"old-room-message",
        ))
        self.service._drop_endpoint(self.guest)
        self.assertNotIn(self.guest, self.service._wire)
        self.assertEqual(self.service.outbound.release_deferred_peer([], self.guest), 0)

    def test_deferred_countdown_records_the_sequence_only_when_sent(self):
        binding = self.service._bindings[self.host]
        race = self.service._race[binding.game.gid]
        race.phase = RacePhase.SESSION_SETUP
        self.wire.match_timer_sequence = 0
        timer = bytes.fromhex("000000001b") + struct.pack(">Iff", 5, 10.0, 20.5)
        self.service._broadcast_room_timer([], binding.game, timer, source=self.host)
        self.assertEqual(self.wire.match_timer_sequence, 0)
        self.assertEqual(len(self.wire.deferred_peer_publications), 1)
        expected = self.wire.next_server_sequence
        self.wire.session_blocks[0] = b"synthetic"
        self.assertEqual(self.service.outbound.release_deferred_peer([], self.guest), 1)
        self.assertEqual(self.wire.match_timer_sequence, expected)
        self.assertEqual(self.wire.match_timer_generation_id, race.countdown_generation_id)
        self.assertIsNotNone(self.wire.match_timer_retry)

    def test_superseded_countdown_does_not_replace_current_retry_state(self):
        binding = self.service._bindings[self.host]
        race = self.service._race[binding.game.gid]
        race.phase = RacePhase.SESSION_SETUP
        timer = bytes.fromhex("000000001b") + struct.pack(">Iff", 5, 10.0, 20.5)
        self.service._broadcast_room_timer([], binding.game, timer, source=self.host)
        self.wire.match_timer_sequence = 777
        self.wire.match_timer_retry = None
        race.latest_match_timer = b"newer-timer"
        self.wire.session_blocks[0] = b"synthetic"
        self.assertEqual(self.service.outbound.release_deferred_peer([], self.guest), 1)
        self.assertEqual(self.wire.match_timer_sequence, 777)
        self.assertIsNone(self.wire.match_timer_retry)

    def test_queue_overflow_expires_joiner_instead_of_sending_a_partial_stream(self):
        publisher = self.service.outbound
        before = self.wire.next_server_sequence
        for _ in range(65):
            self.assertTrue(publisher.defer_uninitialized_peer(self.host, self.guest, b"x"))
        self.assertTrue(self.wire.deferred_peer_overflow)
        self.assertEqual(len(self.wire.deferred_peer_publications), 64)
        self.wire.session_blocks[0] = b"synthetic"
        self.assertEqual(publisher.release_deferred_peer([], self.guest), 0)
        self.assertEqual(self.wire.next_server_sequence, before)
        self.service.endpoint_lifecycle.poll_retries()
        self.assertNotIn(self.guest, self.service._wire)
        self.assertNotIn(self.guest, self.service._bindings)

    def test_queue_byte_limit_expires_without_allocating_sequence(self):
        before = self.wire.next_server_sequence
        publisher = self.service.outbound
        self.assertTrue(publisher.defer_uninitialized_peer(self.host, self.guest, b"x" * 65536))
        self.assertTrue(publisher.defer_uninitialized_peer(self.host, self.guest, b"x"))
        self.assertTrue(self.wire.deferred_peer_overflow)
        self.wire.session_blocks[0] = b"synthetic"
        self.assertEqual(publisher.release_deferred_peer([], self.guest), 0)
        self.assertEqual(self.wire.next_server_sequence, before)
        self.service.endpoint_lifecycle.poll_retries()
        self.assertNotIn(self.guest, self.service._wire)

    def test_non_xbox_recipient_is_not_held(self):
        binding = self.service._bindings[self.guest]
        participant = replace(binding.participant, identity=replace(
            binding.participant.identity, client_platform=ClientPlatform.PC,
        ))
        self.service._bindings[self.guest] = replace(binding, participant=participant)
        self.assertFalse(self.service.outbound.defer_uninitialized_peer(
            self.host, self.guest, b"ordinary",
        ))
        self.assertEqual(self.wire.deferred_peer_publications, [])

    def test_server_hosted_quick_match_uses_allocator_as_host(self):
        game = self.flow.game
        game.server_hosted = True
        game.properties["B-U-game_type"] = "1"
        game.allocator_user_id = self.service._bindings[self.host].participant.identity.user_id
        self.assertTrue(self.service.outbound.defer_uninitialized_peer(
            self.host, self.guest, b"quick-match-message",
        ))
        self.assertEqual(len(self.wire.deferred_peer_publications), 1)
        game.properties["B-U-game_type"] = "2"
        self.assertFalse(self.service.outbound.defer_uninitialized_peer(
            self.host, self.guest, b"challenge-is-separate",
        ))

    def test_invited_helper_keeps_existing_barrier_policy(self):
        binding = self.service._bindings[self.guest]
        self.service._bindings[self.guest] = replace(
            binding, participant=replace(binding.participant, invite_remote_player_id=123),
        )
        self.assertFalse(self.service.outbound.defer_uninitialized_peer(
            self.host, self.guest, b"invited-helper",
        ))


if __name__ == "__main__":
    unittest.main()
