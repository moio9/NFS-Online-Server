from __future__ import annotations

import socket
from threading import Event, Thread
import unittest

from common.legal import TERMS_OF_SERVICE_TEXT

from classic.core.config import Endpoint
from classic.ea.web import ClassicWebGateway, MW_NEWS_PATH, U2_NEWS_PATH


class ClassicWebGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = ClassicWebGateway(Endpoint("messenger.example", 13505))

    def _exchange(self, request: bytes) -> bytes:
        server, client = socket.socketpair()
        stop_event = Event()

        def serve() -> None:
            with server:
                self.gateway.handle_connection(
                    server,
                    ("127.0.0.1", 12345),
                    stop_event,
                )

        worker = Thread(target=serve, daemon=True)
        worker.start()
        response = bytearray()
        with client:
            client.settimeout(2.0)
            client.sendall(request)
            client.shutdown(socket.SHUT_WR)
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
        worker.join(2.0)
        self.assertFalse(worker.is_alive())
        return bytes(response)

    def test_each_game_has_its_own_news_document(self) -> None:
        u2 = self._exchange(
            f"GET {U2_NEWS_PATH} HTTP/1.1\r\nHost: example\r\n\r\n".encode("ascii")
        )
        mw = self._exchange(
            f"GET {MW_NEWS_PATH} HTTP/1.1\r\nHost: example\r\n\r\n".encode("ascii")
        )

        self.assertIn(b"HTTP/1.1 200 OK", u2)
        self.assertIn(b"Need for Speed Underground 2", u2)
        self.assertNotIn(b"Most Wanted", u2)
        self.assertIn(b"HTTP/1.1 200 OK", mw)
        self.assertIn(b"Need for Speed Most Wanted", mw)
        self.assertNotIn(b"Underground 2", mw)

    def test_tos_uses_the_shared_canonical_text(self) -> None:
        response = self._exchange(b"GET /tos HTTP/1.1\r\nHost: example\r\n\r\n")
        headers, body = response.split(b"\r\n\r\n", 1)
        expected_text = TERMS_OF_SERVICE_TEXT.replace("\n", "\r\n").encode("utf-8")

        self.assertIn(b"HTTP/1.1 200 OK", headers)
        self.assertIn(expected_text, body)
        self.assertIn(f"Content-Length: {len(body)}".encode("ascii"), headers)

    def test_head_and_unknown_routes_have_explicit_semantics(self) -> None:
        document = self.gateway._document_for("/tos")
        assert document is not None
        head = self._exchange(b"HEAD /tos HTTP/1.1\r\nHost: example\r\n\r\n")
        headers, body = head.split(b"\r\n\r\n", 1)
        missing = self._exchange(b"GET /missing HTTP/1.1\r\nHost: example\r\n\r\n")

        self.assertIn(f"Content-Length: {len(document)}".encode("ascii"), headers)
        self.assertEqual(body, b"")
        self.assertIn(b"HTTP/1.1 404 Not Found", missing)

    def test_legacy_news_route_remains_available(self) -> None:
        response = self._exchange(b"GET /news HTTP/1.1\r\nHost: example\r\n\r\n")

        self.assertIn(b"HTTP/1.1 200 OK", response)
        self.assertIn(b"Underground 2 and Most Wanted", response)


if __name__ == "__main__":
    unittest.main()
