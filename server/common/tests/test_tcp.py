from __future__ import annotations

from dataclasses import dataclass
import socket
from threading import Event
import time
import unittest

from common.tcp import TCPListener


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int


class TCPListenerTests(unittest.TestCase):
    def test_non_positive_backlog_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "backlog"):
            TCPListener(
                Endpoint("127.0.0.1", 0),
                lambda _conn, _addr, _stop: None,
                name="invalid-backlog",
                backlog=0,
            )

    def test_stop_wakes_handler_blocked_in_recv(self) -> None:
        entered = Event()
        exited = Event()

        def handler(conn: socket.socket, _addr: tuple[str, int], _stop: Event) -> None:
            entered.set()
            try:
                conn.recv(1)
            except OSError:
                pass
            finally:
                exited.set()

        listener = TCPListener(
            Endpoint("127.0.0.1", 0),
            handler,
            name="shutdown-test",
        )
        bound = listener.start()
        client = socket.create_connection((bound.host, bound.port), timeout=1.0)
        try:
            self.assertTrue(entered.wait(2.0), "handler did not accept the client")
            started = time.monotonic()
            listener.stop()
            self.assertTrue(exited.wait(1.0), "handler stayed blocked after stop")
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            try:
                client.close()
            except OSError:
                pass
            listener.stop()


if __name__ == "__main__":
    unittest.main()
