"""Reusable threaded TCP listener without project-specific imports."""

from __future__ import annotations

import logging
import socket
from threading import Event, Lock, Thread
from typing import Callable, Protocol, TypeVar


log = logging.getLogger(__name__)


class EndpointLike(Protocol):
    host: str
    port: int


EndpointT = TypeVar("EndpointT", bound=EndpointLike)
ConnectionHandler = Callable[[socket.socket, tuple[str, int], Event], None]
DEFAULT_LISTEN_BACKLOG = 64


class TCPListener:
    def __init__(
        self,
        endpoint: EndpointT,
        handler: ConnectionHandler,
        *,
        name: str,
        backlog: int = DEFAULT_LISTEN_BACKLOG,
    ) -> None:
        if int(backlog) <= 0:
            raise ValueError("TCP listen backlog must be positive")
        self.endpoint = endpoint
        self.handler = handler
        self.name = name
        self.backlog = int(backlog)
        self.stop_event = Event()
        self._socket: socket.socket | None = None
        self._accept_thread: Thread | None = None
        self._clients: set[Thread] = set()
        self._connections: set[socket.socket] = set()
        self._clients_lock = Lock()

    @property
    def bound_endpoint(self) -> EndpointT:
        sock = self._socket
        if sock is None:
            return self.endpoint
        host, port = sock.getsockname()[:2]
        return type(self.endpoint)(str(host), int(port))

    def start(self) -> EndpointT:
        if self._socket is not None:
            raise RuntimeError(f"{self.name} listener already started")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.endpoint.host, self.endpoint.port))
            sock.listen(self.backlog)
            sock.settimeout(0.5)
        except Exception:
            sock.close()
            raise
        self.stop_event.clear()
        self._socket = sock
        self._accept_thread = Thread(
            target=self._accept_loop,
            name=f"{self.name}-accept",
            daemon=True,
        )
        self._accept_thread.start()
        return self.bound_endpoint

    def _accept_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                assert self._socket is not None
                conn, addr = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = Thread(
                target=self._run_client,
                args=(conn, (str(addr[0]), int(addr[1]))),
                name=f"{self.name}-client",
                daemon=True,
            )
            with self._clients_lock:
                self._clients.add(thread)
                self._connections.add(conn)
            thread.start()

    def _run_client(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        current = None
        try:
            from threading import current_thread

            current = current_thread()
            self.handler(conn, addr, self.stop_event)
        except Exception:
            log.exception("%s connection failed for %s:%d", self.name, addr[0], addr[1])
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._clients_lock:
                self._connections.discard(conn)
                if current is not None:
                    self._clients.discard(current)

    def stop(self) -> None:
        self.stop_event.set()
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None
        with self._clients_lock:
            connections = list(self._connections)
            clients = list(self._clients)
        # Wake handlers blocked in recv immediately instead of relying on each
        # protocol's polling timeout during shutdown.
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        for thread in clients:
            thread.join(timeout=2.0)
