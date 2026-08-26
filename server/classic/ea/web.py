"""Shared classic EA news/TOS and PREL endpoint."""

from __future__ import annotations

import socket
from threading import Event

from common.legal import TERMS_OF_SERVICE_TEXT

from classic.core.config import Endpoint


# Keep the advertised paths shorter than the legacy ``/news`` path.  U2 pads
# this lobby frame to a stock size, so longer routes can needlessly expand the
# signed wire packet when a public hostname is also long.
U2_NEWS_PATH = "/u2"
MW_NEWS_PATH = "/mw"

_NEWS_HEADER = b'%{ CMD=news TITLE="News" BTN1="Close" BTN1-GOTO="$quit"%}\r\n'
_TOS_HEADER = (
    b'%{ CMD=news TITLE="Terms of Service" BTN1="Agree" BTN1-GOTO="$quit" '
    b'BTN2="Disagree" BTN2-GOTO="$exit=-1" %}\r\n'
)
_NOT_FOUND_BODY = b"Not Found\r\n"


def _text_document(header: bytes, text: str) -> bytes:
    body = text.replace("\n", "\r\n").encode("utf-8")
    return header + body + b"\r\n"


_TOS_BODY = _text_document(_TOS_HEADER, TERMS_OF_SERVICE_TEXT)
_NEWS_TEXT = {
    U2_NEWS_PATH: "Need for Speed Underground 2 online services are available.",
    MW_NEWS_PATH: "Need for Speed Most Wanted online services are available.",
    "/news": "Underground 2 and Most Wanted online services are available.",
}
_NEWS_BODY = {
    path: _text_document(_NEWS_HEADER, text)
    for path, text in _NEWS_TEXT.items()
}


class ClassicWebGateway:
    def __init__(self, messenger_public: Endpoint, *, max_request_size: int = 16_384) -> None:
        self.messenger_public = messenger_public
        self.max_request_size = int(max_request_size)

    def set_messenger_public(self, endpoint: Endpoint) -> None:
        self.messenger_public = endpoint

    @staticmethod
    def _http_response(
        body: bytes,
        *,
        include_body: bool,
        status: str = "200 OK",
    ) -> bytes:
        payload = body if include_body else b""
        headers = (
            f"HTTP/1.1 {status}\r\n".encode("ascii")
            + b"Content-Type: text/plain; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
        )
        return headers + payload

    @staticmethod
    def _document_for(target: str) -> bytes | None:
        path = target.split("?", 1)[0].casefold()
        if path == "/tos":
            return _TOS_BODY
        return _NEWS_BODY.get(path)

    def handle_connection(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
        stop_event: Event,
    ) -> None:
        del addr
        conn.settimeout(5.0)
        data = bytearray()
        while not stop_event.is_set() and len(data) < self.max_request_size:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            data.extend(chunk)
            if b"\r\n\r\n" in data or b"\x00" in data:
                break
        raw = bytes(data)
        upper = raw[:16].upper()
        if upper.startswith((b"GET ", b"HEAD ")):
            line = raw.decode("latin-1", errors="replace").split("\r\n", 1)[0]
            parts = line.split(" ")
            method = parts[0].upper() if parts else "GET"
            target = parts[1] if len(parts) > 1 else "/"
            document = self._document_for(target)
            if document is None:
                conn.sendall(
                    self._http_response(
                        _NOT_FOUND_BODY,
                        include_body=method != "HEAD",
                        status="404 Not Found",
                    )
                )
                return
            conn.sendall(
                self._http_response(document, include_body=method != "HEAD")
            )
            return
        if upper.startswith(b"PREL"):
            endpoint = self.messenger_public
            response = "\t".join(
                (
                    "PRELRESP",
                    "VER=1",
                    f"LOBBYHOST={endpoint.host}",
                    f"LOBBYTCP={endpoint.port}",
                    "STATUS=OK",
                )
            ).encode("utf-8") + b"\x00"
            conn.sendall(response)
