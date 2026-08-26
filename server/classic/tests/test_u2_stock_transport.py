from __future__ import annotations

from hashlib import md5
import socket
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import time
import unittest

from classic.app import ClassicOnlineApplication
from classic.core.catalog import GameId
from classic.core.config import Endpoint, ClassicGameSettings, ServerSettings
from classic.protocols.frame import ClassicEAFrame
from classic.protocols.u2_stock_transport import (
    U2StockBootstrapTransport,
    _CHALLENGE,
    _RSA_N,
)


class U2StockTransportTests(unittest.TestCase):
    def _settings(self, root: Path) -> ServerSettings:
        return ServerSettings(
            underground2=ClassicGameSettings(
                GameId.UNDERGROUND2,
                Endpoint("127.0.0.1", 0),
                Endpoint("127.0.0.1", 0),
                Endpoint("127.0.0.1", 0),
                Endpoint("127.0.0.1", 0),
                "517",
                "U2 Public Key",
            ),
            most_wanted=ClassicGameSettings(
                GameId.MOST_WANTED,
                Endpoint("127.0.0.1", 0),
                Endpoint("127.0.0.1", 0),
                Endpoint("127.0.0.1", 0),
                Endpoint("127.0.0.1", 0),
                "618",
                "MW Public Key",
            ),
            messenger_listen=Endpoint("127.0.0.1", 0),
            messenger_public=Endpoint("127.0.0.1", 0),
            web_listen=Endpoint("127.0.0.1", 0),
            web_public=Endpoint("127.0.0.1", 0),
            auth_data_path=str(root / "auth.json"),
            social_data_path=str(root / "social.json"),
            stats_data_path=str(root / "stats.json"),
            connection_timeout=3.0,
        )

    @staticmethod
    def _recv_exact(sock: socket.socket, expected: int) -> bytes:
        deadline = time.monotonic() + 3.0
        output = bytearray()
        while len(output) < expected:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"timed out at {len(output)}/{expected} bytes")
            sock.settimeout(remaining)
            chunk = sock.recv(expected - len(output))
            if not chunk:
                raise AssertionError("peer closed")
            output.extend(chunk)
        return bytes(output)

    def test_secure_frame_crypto_round_trip(self) -> None:
        key = b"0123456789abcdef"
        state = U2StockBootstrapTransport._ksa(key)
        next_state, frame = U2StockBootstrapTransport._make_secure_frame(
            key,
            state,
            0,
            b"@dir-test",
        )
        self.assertNotEqual(next_state, state)
        _recv_state, body = U2StockBootstrapTransport._decrypt_secure_frame(state, frame)
        self.assertEqual(body, b"@dir-test")

    def test_stock_secure_directory_reply_uses_one_based_mac_sequence(self) -> None:
        with TemporaryDirectory() as temporary:
            app = ClassicOnlineApplication(self._settings(Path(temporary)))
            app.start()
            try:
                endpoint = app.u2.bootstrap_listener.bound_endpoint
                with socket.create_connection((endpoint.host, endpoint.port), timeout=2) as client:
                    token = bytes(range(0x30, 0x40))
                    hello = bytes.fromhex("801c010002000300000010010080") + token
                    client.sendall(hello)
                    certificate = self._recv_exact(client, 832)
                    self.assertEqual(certificate[-16:], _CHALLENGE)

                    work = bytes(range(0x40, 0x50))
                    padding = b"\xff" * (128 - len(work) - 3)
                    rsa_plain = b"\x00\x02" + padding + b"\x00" + work
                    cipher = pow(int.from_bytes(rsa_plain, "big"), 3, _RSA_N).to_bytes(128, "big")
                    client.sendall(struct.pack("!H", 0x8000 | 138) + b"\x00" * 10 + cipher)

                    server_recv_key = md5(work + b"0" + token + _CHALLENGE).digest()
                    server_send_key = md5(work + b"1" + token + _CHALLENGE).digest()
                    server_recv_state = U2StockBootstrapTransport._ksa(server_recv_key)
                    server_send_state = U2StockBootstrapTransport._ksa(server_send_key)

                    server_recv_state, first_plain = U2StockBootstrapTransport._rc4_apply(
                        server_recv_state,
                        self._recv_exact(client, 19)[2:],
                    )
                    self.assertEqual(first_plain[16:], b"Q")
                    self.assertEqual(
                        first_plain[:16],
                        md5(server_recv_key + b"Q" + struct.pack(">I", 1)).digest(),
                    )

                    client_send_state, confirmation = U2StockBootstrapTransport._make_secure_frame(
                        server_send_key,
                        server_send_state,
                        1,
                        b"\x03" + _CHALLENGE,
                    )
                    client.sendall(confirmation)
                    server_recv_state, second_plain = U2StockBootstrapTransport._rc4_apply(
                        server_recv_state,
                        self._recv_exact(client, 19)[2:],
                    )
                    self.assertEqual(second_plain[16:], b"7")
                    self.assertEqual(
                        second_plain[:16],
                        md5(server_recv_key + b"7" + struct.pack(">I", 2)).digest(),
                    )

                    for sequence, request in enumerate(
                        (
                            ClassicEAFrame("@tic", b"REGN=NA\n\x00").encode(),
                            ClassicEAFrame("@dir", b"PROD=nfs-pc-2005\n\x00").encode(),
                        ),
                        start=2,
                    ):
                        client_send_state, wire = U2StockBootstrapTransport._make_secure_frame(
                            server_send_key,
                            client_send_state,
                            sequence,
                            request,
                        )
                        client.sendall(wire)

                    header = self._recv_exact(client, 2)
                    reply_size = (struct.unpack("!H", header)[0] & 0x7FFF)
                    encrypted_reply = self._recv_exact(client, reply_size)
                    _state, reply_plain = U2StockBootstrapTransport._rc4_apply(
                        server_recv_state,
                        encrypted_reply,
                    )
                    reply_body = reply_plain[16:]
                    self.assertEqual(
                        reply_plain[:16],
                        md5(server_recv_key + reply_body + struct.pack(">I", 3)).digest(),
                    )
                    frame, trailing = ClassicEAFrame.decode_one(reply_body)
                    self.assertFalse(trailing)
                    fields = frame.fields()
                    self.assertEqual(frame.command, "@dir")
                    self.assertEqual(fields["ADDR"], "127.0.0.1")
                    self.assertEqual(int(fields["PORT"]), app.u2.lobby_listener.bound_endpoint.port)
                    self.assertTrue(fields["SESS"])
                    self.assertTrue(fields["MASK"])
            finally:
                app.stop()


if __name__ == "__main__":
    unittest.main()
