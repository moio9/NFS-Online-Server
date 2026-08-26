"""Reusable UDP listener whose protocol handler owns all reply decisions."""

from __future__ import annotations

import logging
from queue import Queue
import socket
import time
from threading import Event, Lock, Thread
from typing import Callable, Iterable

from classic.core.config import Endpoint


log = logging.getLogger(__name__)
DatagramReply = tuple[bytes, tuple[str, int]]
DatagramHandler = Callable[[bytes, tuple[str, int]], Iterable[DatagramReply]]
ReplySpacing = float | Callable[[tuple[str, int]], float]
SendQueueItem = tuple[bytes, float] | None


class UDPListener:
    def __init__(
        self,
        endpoint: Endpoint,
        handler: DatagramHandler,
        *,
        name: str,
        reply_spacing_seconds: ReplySpacing = 0.0,
        isolate_reply_targets: bool = False,
    ) -> None:
        self.endpoint = endpoint
        self.handler = handler
        self.name = name
        self.reply_spacing_seconds = reply_spacing_seconds
        self.isolate_reply_targets = bool(isolate_reply_targets)
        self.stop_event = Event()
        self._socket: socket.socket | None = None
        self._thread: Thread | None = None
        self._send_lock = Lock()
        self._send_queues: dict[tuple[str, int], Queue[SendQueueItem]] = {}
        self._send_threads: dict[tuple[str, int], Thread] = {}
        self._send_backlog_log_not_before: dict[tuple[str, int], float] = {}

    @property
    def bound_endpoint(self) -> Endpoint:
        if self._socket is None:
            return self.endpoint
        host, port = self._socket.getsockname()[:2]
        return Endpoint(str(host), int(port))

    def start(self) -> Endpoint:
        if self._socket is not None:
            raise RuntimeError(f"{self.name} listener already started")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.endpoint.host, self.endpoint.port))
            sock.settimeout(0.5)
        except Exception:
            sock.close()
            raise
        self.stop_event.clear()
        self._socket = sock
        self._thread = Thread(target=self._loop, name=f"{self.name}-udp", daemon=True)
        self._thread.start()
        return self.bound_endpoint

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                assert self._socket is not None
                payload, raw_addr = self._socket.recvfrom(65_535)
            except socket.timeout:
                continue
            except OSError:
                break
            addr = (str(raw_addr[0]), int(raw_addr[1]))
            try:
                replies = self.handler(payload, addr)
                self._dispatch_replies(replies)
            except Exception:
                log.exception("%s datagram failed for %s:%d", self.name, addr[0], addr[1])

    def _dispatch_replies(self, replies: Iterable[DatagramReply]) -> None:
        for index, (response, target) in enumerate(replies):
            if not response:
                continue
            delay = self._reply_spacing_for(target) if index else 0.0
            if self.isolate_reply_targets:
                self._enqueue_send(response, target, delay)
                continue
            if delay:
                time.sleep(delay)
            self._send_response(response, target)

    def _reply_spacing_for(self, target: tuple[str, int]) -> float:
        configured = self.reply_spacing_seconds
        if callable(configured):
            try:
                return max(0.0, float(configured(target)))
            except Exception:
                log.exception(
                    "%s reply-spacing resolver failed for %s:%d",
                    self.name,
                    target[0],
                    target[1],
                )
                return 0.0
        return max(0.0, float(configured))

    def _enqueue_send(
        self,
        response: bytes,
        target: tuple[str, int],
        delay: float,
    ) -> None:
        with self._send_lock:
            if self.stop_event.is_set():
                return
            queue = self._send_queues.get(target)
            if queue is None:
                queue = Queue()
                thread = Thread(
                    target=self._send_loop,
                    args=(target, queue),
                    name=f"{self.name}-send-{target[0]}-{target[1]}",
                    daemon=True,
                )
                self._send_queues[target] = queue
                self._send_threads[target] = thread
                thread.start()
            queue.put((bytes(response), max(0.0, float(delay))))
            pending = queue.qsize()
            now = time.monotonic()
            if (
                pending >= 8
                and now >= self._send_backlog_log_not_before.get(target, 0.0)
            ):
                self._send_backlog_log_not_before[target] = now + 2.0
                log.warning(
                    "%s isolated send queue backlogged for %s:%d pending=%d",
                    self.name,
                    target[0],
                    target[1],
                    pending,
                )

    def _send_loop(
        self,
        target: tuple[str, int],
        queue: Queue[SendQueueItem],
    ) -> None:
        while True:
            item = queue.get()
            if item is None or self.stop_event.is_set():
                return
            response, delay = item
            if delay and self.stop_event.wait(delay):
                return
            try:
                self._send_response(response, target)
            except OSError:
                if self.stop_event.is_set():
                    return
                log.exception(
                    "%s isolated send failed for %s:%d",
                    self.name,
                    target[0],
                    target[1],
                )

    def _send_response(self, response: bytes, target: tuple[str, int]) -> None:
        sock = self._socket
        if sock is None or self.stop_event.is_set():
            return
        sock.sendto(response, target)

    def send_datagram(self, response: bytes, target: tuple[str, int]) -> None:
        """Send through this listener's bound port.

        Multi-port relays use this to select the source port associated with
        the recipient while keeping socket ownership inside ``UDPListener``.
        """

        if response:
            self._send_response(bytes(response), target)

    def stop(self) -> None:
        self.stop_event.set()
        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()
        with self._send_lock:
            queues = tuple(self._send_queues.values())
            threads = tuple(self._send_threads.values())
        for queue in queues:
            queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        for thread in threads:
            thread.join(timeout=2.0)
        with self._send_lock:
            self._send_queues.clear()
            self._send_threads.clear()
            self._send_backlog_log_not_before.clear()
