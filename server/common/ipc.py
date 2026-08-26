"""Authenticated loopback JSON-lines IPC used between the server processes.

The transport deliberately remains loopback-only.  V2 authenticates the exact
message with HMAC-SHA256 and rejects stale or replayed envelopes; the secret is
never placed on the wire.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import socket
from threading import Event, RLock, Thread
import time
from typing import Callable, Mapping, Protocol, TypeVar
from uuid import uuid4

from .tcp import TCPListener


log = logging.getLogger(__name__)
PROTOCOL = "nfs-online-ipc-v2"
MAX_LINE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_CLOCK_SKEW_SECONDS = 60.0


class EndpointLike(Protocol):
    host: str
    port: int


EndpointT = TypeVar("EndpointT", bound=EndpointLike)
MessageHandler = Callable[[Mapping[str, object]], None]
SnapshotFactory = Callable[[], Mapping[str, object]]


def is_loopback_host(host: str) -> bool:
    text = str(host or "").strip().casefold()
    return text in {"127.0.0.1", "localhost", "::1"}


def _canonical(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _signature(secret: str, envelope_without_signature: Mapping[str, object]) -> str:
    return hmac.new(
        str(secret).encode("utf-8"),
        _canonical(envelope_without_signature),
        hashlib.sha256,
    ).hexdigest()


def authenticated_envelope(
    *,
    secret: str,
    instance_id: str,
    sequence: int,
    sent_at: float,
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Build one signed V2 envelope.

    Exposed mainly for deterministic transport tests and diagnostic tooling.
    """
    unsigned: dict[str, object] = {
        "protocol": PROTOCOL,
        "instance_id": str(instance_id),
        "sequence": int(sequence),
        "sent_at": float(sent_at),
        "payload": dict(payload),
    }
    return {**unsigned, "signature": _signature(secret, unsigned)}


class AuthenticatedJSONIPCServer:
    """Receive signed JSON messages on a loopback-only TCP listener."""

    def __init__(
        self,
        endpoint: EndpointT,
        *,
        secret: str,
        on_message: MessageHandler,
        name: str = "nfs-ipc",
        max_line_bytes: int = MAX_LINE_BYTES,
        max_clock_skew_seconds: float = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not is_loopback_host(endpoint.host):
            raise ValueError("IPC listener must bind to a loopback host")
        if not str(secret or ""):
            raise ValueError("IPC secret must not be empty")
        if int(max_line_bytes) < 1024:
            raise ValueError("IPC max line size is too small")
        if float(max_clock_skew_seconds) <= 0:
            raise ValueError("IPC max clock skew must be positive")
        self.endpoint = endpoint
        self.secret = str(secret)
        self.on_message = on_message
        self.name = name
        self.max_line_bytes = int(max_line_bytes)
        self.max_clock_skew_seconds = float(max_clock_skew_seconds)
        self._clock = clock or time.time
        self._sequence_lock = RLock()
        self._last_sequence: dict[str, int] = {}
        self._listener = TCPListener(endpoint, self._handle_connection, name=name)

    @property
    def bound_endpoint(self) -> EndpointT:
        return self._listener.bound_endpoint

    def start(self) -> EndpointT:
        return self._listener.start()

    def stop(self) -> None:
        self._listener.stop()

    def _verify(self, message: Mapping[str, object]) -> Mapping[str, object] | None:
        if str(message.get("protocol", "")) != PROTOCOL:
            return None
        instance_id = str(message.get("instance_id", "")).strip()
        signature = str(message.get("signature", "")).strip().casefold()
        payload = message.get("payload")
        try:
            sequence = int(message.get("sequence", 0))
            sent_at = float(message.get("sent_at", 0.0))
        except (TypeError, ValueError):
            return None
        if (
            not instance_id
            or sequence <= 0
            or not math.isfinite(sent_at)
            or not isinstance(payload, Mapping)
            or len(signature) != 64
        ):
            return None
        if abs(float(self._clock()) - sent_at) > self.max_clock_skew_seconds:
            log.warning("%s rejected stale IPC envelope instance=%s", self.name, instance_id)
            return None
        unsigned: dict[str, object] = {
            "protocol": PROTOCOL,
            "instance_id": instance_id,
            "sequence": sequence,
            "sent_at": sent_at,
            "payload": dict(payload),
        }
        expected = _signature(self.secret, unsigned)
        if not hmac.compare_digest(signature, expected):
            log.warning("%s rejected invalid IPC signature", self.name)
            return None
        with self._sequence_lock:
            previous = self._last_sequence.get(instance_id, 0)
            if sequence <= previous:
                log.warning(
                    "%s rejected replayed IPC envelope instance=%s sequence=%d previous=%d",
                    self.name,
                    instance_id,
                    sequence,
                    previous,
                )
                return None
            self._last_sequence[instance_id] = sequence
        return payload

    def _consume_line(self, raw: bytes, peer: tuple[str, int]) -> bool:
        if not raw.strip():
            return True
        if len(raw) > self.max_line_bytes:
            log.warning("%s rejected oversized IPC line", self.name)
            return False
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            log.warning("%s rejected invalid JSON from %s:%d", self.name, peer[0], peer[1])
            return False
        if not isinstance(message, Mapping):
            return False
        payload = self._verify(message)
        if payload is None:
            return False
        self.on_message(payload)
        return True

    def _handle_connection(
        self,
        conn: socket.socket,
        peer: tuple[str, int],
        stop_event: Event,
    ) -> None:
        if peer[0] not in {"127.0.0.1", "::1"}:
            log.warning("%s rejected non-loopback peer %s:%d", self.name, peer[0], peer[1])
            return
        conn.settimeout(0.5)
        buffer = bytearray()
        while not stop_event.is_set():
            try:
                chunk = conn.recv(64 * 1024)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buffer.extend(chunk)
            # Enforce the limit per JSON line, not across a recv() that may
            # legitimately contain several complete messages.
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                raw = bytes(buffer[:newline])
                del buffer[: newline + 1]
                if not self._consume_line(raw, peer):
                    return
            if len(buffer) > self.max_line_bytes:
                log.warning("%s rejected oversized unterminated IPC line", self.name)
                return


class AuthenticatedJSONIPCPublisher:
    """Publish changed snapshots and periodic heartbeats with bounded reconnect."""

    def __init__(
        self,
        endpoint: EndpointLike,
        *,
        secret: str,
        snapshot_factory: SnapshotFactory,
        name: str = "nfs-ipc-publisher",
        poll_interval: float = 0.1,
        heartbeat_interval: float = 1.0,
        connect_timeout: float = 1.0,
        reconnect_min_seconds: float = 0.25,
        reconnect_max_seconds: float = 5.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not is_loopback_host(endpoint.host):
            raise ValueError("IPC publisher must connect to a loopback host")
        if not str(secret or ""):
            raise ValueError("IPC secret must not be empty")
        if float(poll_interval) <= 0:
            raise ValueError("IPC poll interval must be positive")
        if float(heartbeat_interval) <= 0:
            raise ValueError("IPC heartbeat interval must be positive")
        if float(reconnect_min_seconds) <= 0:
            raise ValueError("IPC reconnect minimum must be positive")
        if float(reconnect_max_seconds) < float(reconnect_min_seconds):
            raise ValueError("IPC reconnect maximum must not be below the minimum")
        self.endpoint = endpoint
        self.secret = str(secret)
        self.snapshot_factory = snapshot_factory
        self.name = name
        self.poll_interval = float(poll_interval)
        self.heartbeat_interval = float(heartbeat_interval)
        self.connect_timeout = max(0.1, float(connect_timeout))
        self.reconnect_min_seconds = float(reconnect_min_seconds)
        self.reconnect_max_seconds = float(reconnect_max_seconds)
        self._clock = clock or time.time
        self.instance_id = uuid4().hex
        self._stop = Event()
        self._thread: Thread | None = None
        self._lock = RLock()
        self._last_snapshot_wire = b""
        self._sequence = 0

    @staticmethod
    def _canonical(payload: Mapping[str, object]) -> bytes:
        return _canonical(payload)

    def _wire(self, payload: Mapping[str, object]) -> bytes:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        envelope = authenticated_envelope(
            secret=self.secret,
            instance_id=self.instance_id,
            sequence=sequence,
            sent_at=float(self._clock()),
            payload=payload,
        )
        return _canonical(envelope) + b"\n"

    def _connect(self) -> socket.socket:
        sock = socket.create_connection(
            (self.endpoint.host, self.endpoint.port),
            timeout=self.connect_timeout,
        )
        sock.settimeout(self.connect_timeout)
        return sock

    def _run(self) -> None:
        sock: socket.socket | None = None
        next_heartbeat = 0.0
        retry_at = 0.0
        retry_delay = self.reconnect_min_seconds
        while not self._stop.wait(self.poll_interval):
            now = time.monotonic()
            if sock is None and now < retry_at:
                continue
            try:
                if sock is None:
                    sock = self._connect()
                    next_heartbeat = 0.0
                    retry_delay = self.reconnect_min_seconds
                    with self._lock:
                        self._last_snapshot_wire = b""
                    log.info(
                        "%s connected to %s:%d instance=%s",
                        self.name,
                        self.endpoint.host,
                        self.endpoint.port,
                        self.instance_id,
                    )
                state = dict(self.snapshot_factory())
                state_wire = self._canonical(state)
                with self._lock:
                    changed = state_wire != self._last_snapshot_wire
                if changed or now >= next_heartbeat:
                    snapshot = dict(state)
                    snapshot.update(
                        {
                            "kind": "snapshot",
                            "instance_id": self.instance_id,
                            "updated_at": float(self._clock()),
                        }
                    )
                    sock.sendall(self._wire(snapshot))
                    with self._lock:
                        self._last_snapshot_wire = state_wire
                    next_heartbeat = now + self.heartbeat_interval
            except (OSError, ValueError, TypeError):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                sock = None
                retry_at = time.monotonic() + retry_delay
                retry_delay = min(self.reconnect_max_seconds, retry_delay * 2.0)
        if sock is not None:
            try:
                shutdown = dict(self.snapshot_factory())
                shutdown.update(
                    {
                        "kind": "shutdown",
                        "instance_id": self.instance_id,
                        "updated_at": float(self._clock()),
                    }
                )
                sock.sendall(self._wire(shutdown))
            except (OSError, ValueError, TypeError):
                pass
            finally:
                sock.close()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        thread.join(timeout=max(3.0, self.connect_timeout + 1.0))
        self._thread = None
