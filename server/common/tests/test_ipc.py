from __future__ import annotations

from dataclasses import dataclass
import json
import socket
from threading import Event
import time
import unittest

from common.ipc import (
    AuthenticatedJSONIPCPublisher,
    AuthenticatedJSONIPCServer,
    authenticated_envelope,
)


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int


class IPCTransportTests(unittest.TestCase):
    @staticmethod
    def _wait(event: Event, timeout: float = 3.0) -> None:
        if not event.wait(timeout):
            raise AssertionError("timed out waiting for IPC message")

    @staticmethod
    def _wire(envelope: dict[str, object]) -> bytes:
        return json.dumps(
            envelope,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"

    def test_publisher_delivers_authenticated_snapshot_over_loopback(self) -> None:
        messages: list[dict[str, object]] = []
        received = Event()
        shutdown_received = Event()

        def on_message(payload) -> None:
            messages.append(dict(payload))
            if payload.get("kind") == "snapshot":
                received.set()
            elif payload.get("kind") == "shutdown":
                shutdown_received.set()

        server = AuthenticatedJSONIPCServer(
            Endpoint("127.0.0.1", 0),
            secret="shared-secret",
            on_message=on_message,
            name="ipc-test-server",
        )
        bound = server.start()
        publisher = AuthenticatedJSONIPCPublisher(
            bound,
            secret="shared-secret",
            snapshot_factory=lambda: {"game": "carbon", "sessions": {"key.": {"persona": "Driver"}}},
            name="ipc-test-publisher",
            poll_interval=0.01,
            heartbeat_interval=1.0,
        )
        try:
            publisher.start()
            self._wait(received)
            snapshot = next(item for item in messages if item.get("kind") == "snapshot")
            self.assertEqual(snapshot["game"], "carbon")
            self.assertEqual(snapshot["sessions"]["key."]["persona"], "Driver")
            self.assertTrue(snapshot["instance_id"])
            # Polling faster than the heartbeat must not publish a new frame
            # merely because transport metadata has a new timestamp.
            time.sleep(0.12)
            self.assertEqual(
                sum(1 for item in messages if item.get("kind") == "snapshot"),
                1,
            )
        finally:
            publisher.stop()
            self._wait(shutdown_received)
            shutdown = next(item for item in messages if item.get("kind") == "shutdown")
            self.assertEqual(shutdown["game"], "carbon")
            server.stop()

    def test_invalid_hmac_is_rejected(self) -> None:
        received = Event()
        server = AuthenticatedJSONIPCServer(
            Endpoint("127.0.0.1", 0),
            secret="correct-secret",
            on_message=lambda _payload: received.set(),
            name="ipc-secret-test",
        )
        bound = server.start()
        try:
            envelope = authenticated_envelope(
                secret="wrong-secret",
                instance_id="wrong-secret-publisher",
                sequence=1,
                sent_at=time.time(),
                payload={"kind": "snapshot"},
            )
            with socket.create_connection((bound.host, bound.port), timeout=1.0) as sock:
                sock.sendall(self._wire(envelope))
            time.sleep(0.1)
            self.assertFalse(received.is_set())
        finally:
            server.stop()

    def test_replayed_envelope_is_rejected(self) -> None:
        messages: list[dict[str, object]] = []
        received = Event()

        def on_message(payload) -> None:
            messages.append(dict(payload))
            received.set()

        server = AuthenticatedJSONIPCServer(
            Endpoint("127.0.0.1", 0),
            secret="shared-secret",
            on_message=on_message,
            name="ipc-replay-test",
        )
        bound = server.start()
        envelope = authenticated_envelope(
            secret="shared-secret",
            instance_id="publisher-a",
            sequence=1,
            sent_at=time.time(),
            payload={"kind": "snapshot", "value": 1},
        )
        try:
            with socket.create_connection((bound.host, bound.port), timeout=1.0) as sock:
                sock.sendall(self._wire(envelope) + self._wire(envelope))
            self._wait(received)
            time.sleep(0.1)
            self.assertEqual(messages, [{"kind": "snapshot", "value": 1}])
        finally:
            server.stop()

    def test_multiple_valid_lines_share_one_recv_without_false_size_rejection(self) -> None:
        messages: list[dict[str, object]] = []
        received = Event()

        def on_message(payload) -> None:
            messages.append(dict(payload))
            if len(messages) == 2:
                received.set()

        server = AuthenticatedJSONIPCServer(
            Endpoint("127.0.0.1", 0),
            secret="shared-secret",
            on_message=on_message,
            name="ipc-lines-test",
            max_line_bytes=1024,
        )
        bound = server.start()
        now = time.time()
        first = authenticated_envelope(
            secret="shared-secret",
            instance_id="publisher-b",
            sequence=1,
            sent_at=now,
            payload={"kind": "snapshot", "padding": "a" * 500},
        )
        second = authenticated_envelope(
            secret="shared-secret",
            instance_id="publisher-b",
            sequence=2,
            sent_at=now,
            payload={"kind": "heartbeat", "padding": "b" * 500},
        )
        wire = self._wire(first) + self._wire(second)
        self.assertGreater(len(wire), 1024)
        self.assertLess(len(self._wire(first)), 1024)
        self.assertLess(len(self._wire(second)), 1024)
        try:
            with socket.create_connection((bound.host, bound.port), timeout=1.0) as sock:
                sock.sendall(wire)
            self._wait(received)
            self.assertEqual([item["kind"] for item in messages], ["snapshot", "heartbeat"])
        finally:
            server.stop()

    def test_stale_envelope_is_rejected(self) -> None:
        received = Event()
        server = AuthenticatedJSONIPCServer(
            Endpoint("127.0.0.1", 0),
            secret="shared-secret",
            on_message=lambda _payload: received.set(),
            name="ipc-stale-test",
            max_clock_skew_seconds=1.0,
            clock=lambda: 100.0,
        )
        bound = server.start()
        envelope = authenticated_envelope(
            secret="shared-secret",
            instance_id="publisher-c",
            sequence=1,
            sent_at=90.0,
            payload={"kind": "snapshot"},
        )
        try:
            with socket.create_connection((bound.host, bound.port), timeout=1.0) as sock:
                sock.sendall(self._wire(envelope))
            time.sleep(0.1)
            self.assertFalse(received.is_set())
        finally:
            server.stop()

    def test_non_loopback_endpoint_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            AuthenticatedJSONIPCServer(
                Endpoint("0.0.0.0", 13506),
                secret="secret",
                on_message=lambda _payload: None,
            )
        with self.assertRaisesRegex(ValueError, "loopback"):
            AuthenticatedJSONIPCPublisher(
                Endpoint("192.0.2.1", 13506),
                secret="secret",
                snapshot_factory=lambda: {},
            )


if __name__ == "__main__":
    unittest.main()
