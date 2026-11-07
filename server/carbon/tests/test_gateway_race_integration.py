"""Exercise the Xbox login bridge and real TLS race adapter together."""
import asyncio
import importlib.util
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
from tempfile import TemporaryDirectory
import unittest

from carbon.accounts.credentials import CredentialStore
from carbon.accounts.identity import IdentityStore
from carbon.fesl.frame import FESLFrame
from carbon.fesl.service import CarbonEndpoints, CarbonFESLService, FESLConnection
from carbon.xbox_gateway import XboxPasswordBridge


@unittest.skipUnless(importlib.util.find_spec("websockets") and shutil.which("openssl"),
                     "requires optional websockets and openssl")
class GatewayRaceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_gateway_starts_and_closes_secure_race_adapter(self):
        from websockets.asyncio.server import serve
        from carbon.race_client import OriginalRaceClient

        with TemporaryDirectory() as tmp:
            key, cert = Path(tmp) / "key.pem", Path(tmp) / "cert.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", str(key), "-out", str(cert), "-days", "1",
                            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_tls.load_cert_chain(cert, key)
            client_tls = ssl.create_default_context(cafile=str(cert))
            disconnected = asyncio.Event()

            async def echo(ws):
                try:
                    self.assertEqual(await ws.recv(), b"\x0cfake-session")
                    await ws.send(b"\x01")
                    async for frame in ws:
                        await ws.send(frame)
                finally:
                    disconnected.set()

            wss = await serve(echo, "127.0.0.1", 0, ssl=server_tls)
            url = f"wss://localhost:{wss.sockets[0].getsockname()[1]}/race"
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as game:
                game.bind(("127.0.0.1", 0))
                game.setblocking(False)
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
                    reserve.bind(("127.0.0.1", 0))
                    adapter_port = reserve.getsockname()[1]

                credentials = CredentialStore()
                credentials.create_account("Driver", "fake-password", persona="Racer")
                service = CarbonFESLService(
                    CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
                    IdentityStore(token_factory=lambda: "fake-session"),
                    credentials=credentials, authentication_mode="password",
                )
                adapters = []

                def start_race(token):
                    adapter = OriginalRaceClient(url, token, listen_port=adapter_port,
                                                client_port=game.getsockname()[1], ssl_context=client_tls)
                    adapters.append(adapter)
                    adapter.start()
                    return adapter.close

                bridge = XboxPasswordBridge("Driver", "fake-password", on_session=start_race)
                try:
                    lookup = FESLFrame.from_fields("acct", {
                        "TXN": "GetUserIdsByXuids", "xuids.[]": "1", "xuids.0": "fake_xuid",
                    })
                    request, = bridge.client_frame(lookup)
                    response, = service.dispatch(request.frame, FESLConnection())
                    action, = await asyncio.to_thread(bridge.upstream_frame, response)
                    self.assertEqual(action.frame.fields["userIds.[]"], "1")
                    loop = asyncio.get_running_loop()
                    payload = b"synthetic-game-datagram"
                    await loop.sock_sendto(game, payload, ("127.0.0.1", adapter_port))
                    received, _ = await asyncio.wait_for(loop.sock_recvfrom(game, 2048), 3)
                    self.assertEqual(received, payload)
                finally:
                    await asyncio.to_thread(bridge.close)
                    for adapter in adapters:
                        await asyncio.to_thread(adapter.close)
                    wss.close()
                    await wss.wait_closed()
                self.assertTrue(disconnected.is_set())
