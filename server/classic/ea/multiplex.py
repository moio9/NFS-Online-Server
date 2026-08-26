"""Protocol sniffing for the classic EA control endpoints."""

from __future__ import annotations

import socket
from threading import Event
from typing import Callable


ConnectionHandler = Callable[[socket.socket, tuple[str, int], Event], None]


class _BufferedSocket:
    """Minimal socket facade that replays bytes consumed by the sniffer."""

    def __init__(self, conn: socket.socket, prefix: bytes) -> None:
        self._conn = conn
        self._prefix = bytearray(prefix)

    def recv(self, size: int, flags: int = 0) -> bytes:
        if self._prefix and flags == 0:
            count = min(max(0, int(size)), len(self._prefix))
            data = bytes(self._prefix[:count])
            del self._prefix[:count]
            return data
        return self._conn.recv(size, flags)

    def settimeout(self, value: float | None) -> None:
        self._conn.settimeout(value)

    def sendall(self, data: bytes, flags: int = 0) -> None:
        self._conn.sendall(data, flags)

    def shutdown(self, how: int) -> None:
        self._conn.shutdown(how)


class ClassicEndpointMultiplexer:
    """Accept HTTP/PREL and EA Messenger on either classic control port."""

    def __init__(
        self,
        messenger_handler: ConnectionHandler,
        web_handler: ConnectionHandler,
        *,
        sniff_timeout: float = 0.5,
    ) -> None:
        self.messenger_handler = messenger_handler
        self.web_handler = web_handler
        self.sniff_timeout = max(0.05, float(sniff_timeout))

    def handle_connection(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
        stop_event: Event,
    ) -> None:
        conn.settimeout(self.sniff_timeout)
        prefix = bytearray()
        while len(prefix) < 4 and not stop_event.is_set():
            try:
                chunk = conn.recv(4 - len(prefix))
            except socket.timeout:
                continue
            if not chunk:
                return
            prefix.extend(chunk)

        if len(prefix) < 4:
            return
        buffered = _BufferedSocket(conn, bytes(prefix))
        upper = bytes(prefix).upper()
        if upper in {b"GET ", b"HEAD", b"PREL"}:
            self.web_handler(buffered, addr, stop_event)  # type: ignore[arg-type]
            return
        self.messenger_handler(buffered, addr, stop_event)  # type: ignore[arg-type]
