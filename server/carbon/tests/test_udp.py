"""Concurrency and ordering tests for the shared UDP listener."""

from __future__ import annotations

from threading import Event, Lock
import time
import unittest

from carbon.core.config import Endpoint
from carbon.core.udp import UDPListener


class _RecordingSocket:
    def __init__(self, blocked_target: tuple[str, int] | None = None) -> None:
        self.blocked_target = blocked_target
        self.release_blocked = Event()
        self.fast_sent = Event()
        self.blocked_sent = Event()
        self._lock = Lock()
        self.calls: list[tuple[bytes, tuple[str, int], float]] = []

    def sendto(self, payload: bytes, target: tuple[str, int]) -> int:
        if target == self.blocked_target:
            self.release_blocked.wait(timeout=1.0)
        with self._lock:
            self.calls.append((bytes(payload), target, time.monotonic()))
        if target == self.blocked_target:
            self.blocked_sent.set()
        else:
            self.fast_sent.set()
        return len(payload)

    def close(self) -> None:
        self.release_blocked.set()


class UDPListenerSendIsolationTests(unittest.TestCase):
    def test_poll_handler_replies_use_the_normal_send_path(self) -> None:
        target = ("192.0.2.5", 1042)
        sock = _RecordingSocket()
        listener = UDPListener(
            Endpoint("127.0.0.1", 0),
            lambda _payload, _addr: (),
            name="test-udp",
            poll_handler=lambda: ((b"retry", target),),
        )
        listener._socket = sock  # type: ignore[assignment]
        try:
            listener._dispatch_poll_replies()
            self.assertEqual(
                [(payload, destination) for payload, destination, _when in sock.calls],
                [(b"retry", target)],
            )
        finally:
            listener.stop()

    def test_blocked_destination_does_not_block_another_destination(self) -> None:
        slow = ("192.0.2.10", 1042)
        fast = ("192.0.2.20", 1042)
        sock = _RecordingSocket(blocked_target=slow)
        listener = UDPListener(
            Endpoint("127.0.0.1", 0),
            lambda _payload, _addr: (),
            name="test-udp",
            isolate_reply_targets=True,
        )
        listener._socket = sock  # type: ignore[assignment]
        try:
            listener._dispatch_replies(((b"slow", slow), (b"fast", fast)))
            self.assertTrue(sock.fast_sent.wait(timeout=0.25))
            self.assertFalse(sock.blocked_sent.is_set())
            sock.release_blocked.set()
            self.assertTrue(sock.blocked_sent.wait(timeout=0.25))
            self.assertEqual(
                [(payload, target) for payload, target, _when in sock.calls],
                [(b"fast", fast), (b"slow", slow)],
            )
        finally:
            listener.stop()

    def test_each_destination_preserves_order_and_configured_spacing(self) -> None:
        target = ("192.0.2.30", 1042)
        sock = _RecordingSocket()
        listener = UDPListener(
            Endpoint("127.0.0.1", 0),
            lambda _payload, _addr: (),
            name="test-udp",
            reply_spacing_seconds=lambda _target: 0.03,
            isolate_reply_targets=True,
        )
        listener._socket = sock  # type: ignore[assignment]
        try:
            listener._dispatch_replies(
                ((b"first", target), (b"second", target), (b"third", target))
            )
            deadline = time.monotonic() + 0.5
            while len(sock.calls) < 3 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(
                [payload for payload, _target, _when in sock.calls],
                [b"first", b"second", b"third"],
            )
            times = [when for _payload, _target, when in sock.calls]
            self.assertGreaterEqual(times[1] - times[0], 0.02)
            self.assertGreaterEqual(times[2] - times[1], 0.02)
        finally:
            listener.stop()


if __name__ == "__main__":
    unittest.main()
