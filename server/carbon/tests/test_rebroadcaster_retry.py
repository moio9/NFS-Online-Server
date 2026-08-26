"""Unit coverage for server-owned retry timing and lifecycle cleanup."""

import unittest

from carbon.accounts.identity import Identity
from carbon.core.config import Endpoint
from carbon.gamemanager.race_state import RacePhase
from carbon.rebroadcaster.confirmations import ConfirmationManager
from carbon.rebroadcaster.retry import ReliableWindow, RetryPolicy
from carbon.rebroadcaster.service import CarbonRebroadcasterService
from carbon.rebroadcaster.state import EndpointWireState
from carbon.tests import test_carbon_gamemanager_session as session_tests
from carbon.tests.test_carbon_race_flow import _finalized_flow
from carbon.theater.directory import CarbonGameDirectory
from carbon.transport.commudp import game_manager_body


def _contains_startsync(replies) -> bool:
    return any(
        game_manager_body(active.payload).startswith(bytes.fromhex("000000000a"))
        for raw, _target in replies
        for active in session_tests._active_messages(raw)
    )


class RetryPolicyTests(unittest.TestCase):
    def test_schedule_backs_off_and_stops_at_attempt_limit(self) -> None:
        policy = RetryPolicy(0.5, 2.0, 3, 20.0)
        schedule = policy.begin(10.0)

        self.assertFalse(schedule.due(10.49))
        self.assertTrue(schedule.due(10.5))
        self.assertEqual(schedule.record_retry(10.5), 1.0)
        self.assertEqual(schedule.record_retry(11.5), 2.0)
        self.assertEqual(schedule.record_retry(13.5), 2.0)
        self.assertTrue(schedule.exhausted(13.5))
        self.assertEqual(schedule.exhaustion_reason(13.5), "attempt-limit")

    def test_deadline_is_not_extended_by_deferral(self) -> None:
        schedule = RetryPolicy(1.0, 2.0, 5, 4.0).begin(20.0)
        schedule.defer_from(23.5)

        self.assertEqual(schedule.retry_not_before, 24.0)
        self.assertEqual(schedule.exhaustion_reason(24.0), "deadline")

    def test_reliable_window_owns_immutable_record_bytes(self) -> None:
        source = bytearray(b"record")
        window = ReliableWindow(
            records=(source,),
            base_sequence=0x101,
            final_sequence=0x101,
            retry=RetryPolicy(0.5, 1.0, 2, 3.0).begin(0.0),
        )
        source[:] = b"broken"

        self.assertEqual(window.records, (b"record",))


class ConfirmationManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.address = ("192.0.2.44", 1042)
        self.wire = EndpointWireState(last_client_acknowledgement=0x100)
        self.manager = ConfirmationManager(
            {self.address: self.wire},
            retry_policy=RetryPolicy(0.5, 1.0, 2, 5.0),
        )

    def test_poll_replays_exact_records_until_cumulative_ack(self) -> None:
        records = (b"encrypted-one", b"encrypted-two")
        window = self.manager.register(
            self.address,
            records,
            base_sequence=0x101,
            final_sequence=0x102,
            label="join-stage",
            now=10.0,
        )
        self.assertIsNotNone(window)
        self.assertEqual(self.manager.poll(now=10.49), [])
        self.assertEqual(
            self.manager.poll(now=10.5),
            [(records[0], self.address), (records[1], self.address)],
        )

        duplicate = self.manager.observe_inbound(
            self.address,
            sequence=0x20,
            acknowledgement=0x102,
            payload=b"client-ack",
            track_sequence=True,
        )
        self.assertFalse(duplicate)
        self.assertEqual(self.manager.pending(self.address), ())
        self.assertEqual(self.manager.poll(now=11.5), [])

    def test_phase_local_ack_regression_does_not_reopen_confirmed_range(self) -> None:
        self.manager.register(
            self.address,
            (b"publication",),
            base_sequence=0x101,
            final_sequence=0x105,
            label="ready-stage",
            now=20.0,
        )
        self.manager.observe_inbound(
            self.address,
            sequence=0x30,
            acknowledgement=0x104,
            payload=b"phase-one",
            track_sequence=True,
        )
        self.manager.observe_inbound(
            self.address,
            sequence=0x01,
            acknowledgement=0x101,
            payload=b"phase-two",
            track_sequence=True,
        )

        self.assertEqual(self.wire.last_client_acknowledgement, 0x101)
        self.assertEqual(len(self.manager.pending(self.address)), 1)
        self.manager.observe_inbound(
            self.address,
            sequence=0x02,
            acknowledgement=0x105,
            payload=b"phase-two-ack",
            track_sequence=True,
        )
        self.assertEqual(self.manager.pending(self.address), ())

    def test_duplicate_client_datagram_replays_dormant_window(self) -> None:
        manager = ConfirmationManager(
            {self.address: self.wire},
            retry_policy=RetryPolicy(0.5, 1.0, 1, 5.0),
        )
        window = manager.register(
            self.address,
            (b"critical-window",),
            base_sequence=0x101,
            final_sequence=0x101,
            label="create-stage",
            now=30.0,
        )
        self.assertIsNotNone(window)
        assert window is not None
        self.assertEqual(
            manager.poll(now=30.5),
            [(b"critical-window", self.address)],
        )
        self.assertEqual(manager.poll(now=31.0), [])

        self.assertFalse(manager.observe_inbound(
            self.address,
            sequence=0x40,
            acknowledgement=0x0FF,
            payload=b"same-request",
            track_sequence=True,
        ))
        self.assertTrue(manager.observe_inbound(
            self.address,
            sequence=0x40,
            acknowledgement=0x100,
            payload=b"same-request",
            track_sequence=True,
        ))
        self.assertEqual(
            manager.replay_pending(
                self.address,
                now=31.0,
                reason="duplicate-client-datagram",
            ),
            [(b"critical-window", self.address)],
        )
        self.assertEqual(window.retry.retries_sent, 0)

    def test_transport_ack_stops_application_stage_wire_retries(self) -> None:
        window = self.manager.register(
            self.address,
            (b"host-hello-and-roster",),
            base_sequence=0x101,
            final_sequence=0x102,
            label="session-host-bootstrap",
            application_confirmation=True,
            now=40.0,
        )
        self.assertIsNotNone(window)
        assert window is not None

        self.manager.acknowledge(self.address, 0x102)

        self.assertEqual(self.manager.pending(self.address), (window,))
        self.assertTrue(window.transport_acknowledged)
        self.assertEqual(self.manager.poll(now=40.5), [])
        self.assertEqual(window.retry.retries_sent, 0)
        self.assertEqual(
            self.manager.replay_pending(
                self.address,
                now=41.0,
                reason="duplicate-client-datagram",
            ),
            [],
        )
        self.assertEqual(
            self.manager.confirm_application(
                self.address,
                label="session-host-bootstrap",
            ),
            1,
        )
        self.assertEqual(self.manager.pending(self.address), ())


class RebroadcasterCleanupTests(unittest.TestCase):
    def _service(self, *, join_timeout: float = 45.0, race_timeout: float = 60.0):
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        return games, CarbonRebroadcasterService(
            games,
            join_timeout_seconds=join_timeout,
            race_idle_timeout_seconds=race_timeout,
        )

    def test_dedicated_allocation_without_egam_expires(self) -> None:
        games, service = self._service(join_timeout=5.0)
        identity = Identity("account", "Player", 1001, 2001)
        game = games.create(
            identity,
            {"B-U-game_type": "1"},
            server_hosted=True,
        )

        service.poll_retries(now=game.created_at + 5.0)

        self.assertIsNone(games.get(game.gid))
        self.assertIsNone(games.sessions.get_game(game.session.game_id))

    def test_egam_membership_without_udp_bind_expires(self) -> None:
        games, service = self._service(join_timeout=5.0)
        identity = Identity("account", "Player", 1001, 2001)
        game = games.create(identity)
        participant = game.participants[identity.user_id]

        service.poll_retries(now=participant.entered_at + 5.0)

        self.assertIsNone(games.get(game.gid))

    def test_stalled_active_race_retires_room_and_endpoints(self) -> None:
        flow = _finalized_flow()
        service = flow.service
        service.race_idle_timeout_seconds = 5.0
        race = service._race[flow.game.gid]
        race.phase = RacePhase.RACING
        current = 1000.0
        for addr, wire in service._wire.items():
            wire.last_activity_at = (
                current if addr == flow.host_addr else current - 5.0
            )
            wire.pending_ai_registration_windows.clear()
            wire.session_bootstrap_window = None

        service.poll_retries(now=current)

        self.assertIsNone(service.games.get(flow.game.gid))
        self.assertFalse(service.session_endpoints(flow.game.gid))

    def test_ai_registration_retries_from_udp_poll_without_client_traffic(self) -> None:
        flow = _finalized_flow()
        service = flow.service
        registration = bytes.fromhex(
            "0000000005000000790004d1a01768000756494e43454e54"
        )
        initial: list[tuple[bytes, tuple[str, int]]] = []
        service.gameplay_relay.relay_player_controlled_ai(
            initial,
            flow.host_addr,
            service._bindings[flow.host_addr],
            (registration,),
        )
        window = service._wire[
            flow.host_addr
        ].pending_ai_registration_windows[0]
        current = window.retry.retry_not_before

        replies = service.poll_retries(now=current)
        retried_records = [
            game_manager_body(active.payload)
            for raw, target in replies
            if target == flow.host_addr
            for active in session_tests._active_messages(raw)
        ]

        self.assertTrue(any(body.startswith(bytes.fromhex("0000000005")) for body in retried_records))
        self.assertEqual(window.retry.retries_sent, 1)

    def test_loading_fallback_requires_every_live_peer_signal(self) -> None:
        flow = _finalized_flow()
        from carbon.tests.test_carbon_race_flow import (
            _advance_to_countdown_expired,
            _send,
        )

        _advance_to_countdown_expired(flow)
        _send(
            flow.service,
            flow.host_addr,
            outer=100,
            sequence=0x20,
            acknowledgement=0x130,
            logical=bytes.fromhex("000000000800000001"),
        )
        race = flow.service._race[flow.game.gid]
        replies = flow.service.poll_retries(
            now=race.loading_started_at
            + flow.service.loading_ready_fallback_seconds
        )

        self.assertFalse(_contains_startsync(replies))
        self.assertEqual(race.phase, RacePhase.LOADING)

    def test_loading_fallback_releases_after_guest_loading_signal(self) -> None:
        flow = _finalized_flow()
        from carbon.tests.test_carbon_race_flow import (
            _advance_to_countdown_expired,
            _send,
        )

        _advance_to_countdown_expired(flow)
        _send(
            flow.service,
            flow.host_addr,
            outer=100,
            sequence=0x20,
            acknowledgement=0x130,
            logical=bytes.fromhex("000000000800000001"),
        )
        _send(
            flow.service,
            flow.guest_addr,
            outer=101,
            sequence=0x21,
            acknowledgement=0x131,
            logical=bytes.fromhex("000000000800000004"),
        )
        race = flow.service._race[flow.game.gid]
        before = flow.service.poll_retries(
            now=race.loading_started_at
            + flow.service.loading_ready_fallback_seconds
            - 0.001
        )
        after = flow.service.poll_retries(
            now=race.loading_started_at
            + flow.service.loading_ready_fallback_seconds
        )

        self.assertFalse(_contains_startsync(before))
        self.assertTrue(_contains_startsync(after))
        self.assertEqual(race.phase, RacePhase.RACING)

    def test_bootstrap_deadline_removes_only_failed_guest(self) -> None:
        flow = session_tests.CarbonGameManagerFlowTests(methodName="runTest")
        flow.setUp()
        flow._bind_host()
        flow._bind_guest()
        guest_wire = flow.service._wire[flow.guest_addr]
        window = ReliableWindow(
            records=(b"bootstrap",),
            base_sequence=0x101,
            final_sequence=0x101,
            retry=RetryPolicy(0.5, 1.0, 2, 3.0).begin(100.0),
        )
        guest_wire.session_bootstrap_window = window
        window.retry.deadline = window.retry.opened_at

        flow.service.poll_retries(now=window.retry.opened_at)

        self.assertNotIn(flow.guest_addr, flow.service._bindings)
        self.assertIn(flow.host.user_id, flow.game.participants)
        self.assertNotIn(flow.guest.user_id, flow.game.participants)
        self.assertIs(flow.service.games.get(flow.game.gid), flow.game)

    def test_acked_bootstrap_waits_silently_then_removes_failed_guest(self) -> None:
        flow = session_tests.CarbonGameManagerFlowTests(methodName="runTest")
        flow.setUp()
        flow._bind_host()
        flow._bind_guest()
        guest_wire = flow.service._wire[flow.guest_addr]
        window = ReliableWindow(
            records=(b"bootstrap",),
            base_sequence=0x101,
            final_sequence=0x101,
            retry=RetryPolicy(0.5, 1.0, 1, 3.0).begin(100.0),
        )
        guest_wire.session_bootstrap_window = window
        guest_wire.last_client_acknowledgement = window.final_sequence
        flow.service.confirmations.clear_endpoint(flow.host_addr)
        flow.service.confirmations.clear_endpoint(flow.guest_addr)

        self.assertEqual(flow.service.poll_retries(now=100.5), [])
        self.assertTrue(window.transport_acknowledged)
        self.assertEqual(window.retry.retries_sent, 0)
        self.assertIn(flow.guest_addr, flow.service._bindings)

        self.assertEqual(flow.service.poll_retries(now=102.99), [])
        self.assertIn(flow.guest_addr, flow.service._bindings)

        flow.service.poll_retries(now=103.0)

        self.assertNotIn(flow.guest_addr, flow.service._bindings)
        self.assertIn(flow.host.user_id, flow.game.participants)
        self.assertNotIn(flow.guest.user_id, flow.game.participants)
        self.assertIs(flow.service.games.get(flow.game.gid), flow.game)


if __name__ == "__main__":
    unittest.main()
