"""Boundaries for the optional original MW lobby bridge."""
import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from classic.games.most_wanted.xbox360.__main__ import create_runtime
from classic.games.most_wanted.xbox360.client_bridge import read_frame
from classic.games.most_wanted.xbox360.original_host import OriginalHostBridge, dll_identity_frame
from classic.protocols.frame import ClassicEAFrame
from classic.protocols.password import make_password_token
from classic.tests import test_combined_runtime


class OriginalIdentityTests(unittest.TestCase):
    def test_identity_mapping_removes_credentials_and_preserves_opaque_bytes(self):
        source = ClassicEAFrame("auth", b"GTAG=Guest\nNAME=Guest\nPASS=secret\nPSES=mask\n"
                                b"MADDR=$\xff\x80\n\0")
        result = ClassicEAFrame.decode_one(dll_identity_frame(source, "Driver"))[0]
        self.assertEqual(result.fields()["GTAG"], "Driver")
        self.assertEqual(result.fields()["NAME"], "Driver")
        self.assertNotIn("PASS", result.fields())
        self.assertNotIn("PSES", result.fields())
        self.assertIn(b"MADDR=$\xff\x80\n", result.payload)
        persona = ClassicEAFrame("pers", b"GTAG=Guest\0")
        result = ClassicEAFrame.decode_one(dll_identity_frame(persona, "Driver"))[0]
        self.assertEqual(result.fields()["PERS"], "Driver")

    def test_invalid_persona_and_nonlocal_endpoints_are_rejected(self):
        with self.assertRaises(ValueError):
            dll_identity_frame(ClassicEAFrame("pers", b"GTAG=A\0"), "has\ninjection")
        with self.assertRaises(ValueError):
            OriginalHostBridge("0.0.0.0", "127.0.0.1", "127.0.0.1")


class OriginalBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_does_not_start_without_the_original_host(self):
        unused = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = unused.sockets[0].getsockname()[1]
        unused.close()
        await unused.wait_closed()
        bridge = OriginalHostBridge("127.0.0.1", "127.0.0.1", "127.0.0.1", dll_port=port)
        with self.assertRaisesRegex(ConnectionError, "not reachable"):
            await bridge.serve()

    async def test_shared_account_runtime_rejects_bad_password_before_dll(self):
        received = []

        async def dll_service(reader, writer):
            try:
                frame = await read_frame(reader)
                received.append(frame)
                writer.write(ClassicEAFrame.from_fields("auth", {"NAME": "Driver"}).encode())
                await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()

        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(test_combined_runtime.CombinedRuntimeTests._settings(root), enable_u2=False, enable_mw=True,
                               auth_mode="password", account_db_path=str(root / "accounts.sqlite3"),
                               account_files_path=str(root / "users"))
            app, runtime = create_runtime(settings, bind_ip="127.0.0.14")
            app.credentials.create_account("Driver", "synthetic-password", persona="Driver")
            runtime.start()
            dll_server = await asyncio.start_server(dll_service, "127.0.0.1", 0)
            bridge = OriginalHostBridge("127.0.0.15", "127.0.0.14", "127.0.0.1",
                                        dll_port=dll_server.sockets[0].getsockname()[1])
            directory_server = await asyncio.start_server(bridge.directory, bridge.listen_ip, 30960)
            proxy_server = await asyncio.start_server(bridge.lobby, bridge.listen_ip, 10901)
            writer = None
            try:
                directory_reader, directory_writer = await asyncio.open_connection(bridge.listen_ip, 30960)
                directory_writer.write(ClassicEAFrame.from_fields("@dir", {"PROD": "nfs-xbox-2006"}).encode())
                await directory_writer.drain()
                mask = (await asyncio.wait_for(read_frame(directory_reader), 3)).fields()["MASK"]
                directory_writer.close()
                await directory_writer.wait_closed()
                reader, writer = await asyncio.open_connection(bridge.listen_ip, 10901)

                async def login(password):
                    frame = ClassicEAFrame.from_fields("auth", {
                        "NAME": "Driver", "GTAG": "XboxPlayer", "PSES": mask,
                        "PASS": make_password_token(password, mask),
                    })
                    writer.write(frame.encode())
                    await writer.drain()
                    return await asyncio.wait_for(read_frame(reader), 4)

                self.assertNotEqual((await login("bad-password")).reserved, 0)
                self.assertEqual(received, [])
                self.assertEqual((await login("synthetic-password")).reserved, 0)
                self.assertEqual(received[0].fields()["GTAG"], "Driver")
                self.assertNotIn("PASS", received[0].fields())
            finally:
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()
                for server in (proxy_server, directory_server, dll_server):
                    server.close()
                    await server.wait_closed()
                runtime.stop()

    async def test_login_is_checked_before_forwarding_and_persona_is_bound(self):
        received = []

        async def python_service(reader, writer):
            try:
                while True:
                    frame = await read_frame(reader)
                    if frame.command == "auth":
                        if frame.fields().get("PASS") == "good":
                            reply = ClassicEAFrame.from_fields("auth", {"NAME": "Driver"})
                        else:
                            reply = ClassicEAFrame("auth", b"", 0x70617373)
                    elif frame.command == "pers":
                        reply = ClassicEAFrame.from_fields("pers", {"PERS": "Driver"})
                    elif frame.command == "user":
                        reply = ClassicEAFrame.from_fields("user", {"PERS": "Driver", "STAT": "actual"})
                    else:
                        continue
                    writer.write(reply.encode())
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()

        async def dll_service(reader, writer):
            try:
                while True:
                    frame = await read_frame(reader)
                    received.append(frame)
                    if frame.command in ("auth", "pers", "gcre"):
                        writer.write(ClassicEAFrame.from_fields(frame.command, {"OK": "1"}).encode())
                        await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()

        python_server = await asyncio.start_server(python_service, "127.0.0.1", 0)
        dll_server = await asyncio.start_server(dll_service, "127.0.0.1", 0)
        python_port = python_server.sockets[0].getsockname()[1]
        dll_port = dll_server.sockets[0].getsockname()[1]
        bridge = OriginalHostBridge("127.0.0.1", "127.0.0.1", "127.0.0.1",
                                    lobby_port=python_port, dll_port=dll_port)
        proxy = await asyncio.start_server(bridge.lobby, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.sockets[0].getsockname()[1])
        try:
            async def exchange(command, fields):
                writer.write(ClassicEAFrame.from_fields(command, fields).encode())
                await writer.drain()
                return await asyncio.wait_for(read_frame(reader), 2)

            denied = await exchange("auth", {"NAME": "Driver", "GTAG": "Guest", "PASS": "bad"})
            self.assertNotEqual(denied.reserved, 0)
            self.assertEqual(received, [])
            accepted = await exchange("auth", {"NAME": "Driver", "GTAG": "Guest", "PASS": "good", "PSES": "secret"})
            self.assertEqual(accepted.command, "auth")
            self.assertEqual(received[0].fields()["GTAG"], "Driver")
            self.assertNotIn("PASS", received[0].fields())
            self.assertNotIn("PSES", received[0].fields())
            await exchange("pers", {"GTAG": "Impersonated"})
            self.assertEqual(received[1].fields()["PERS"], "Driver")
            stats = await exchange("user", {"PERS": "Driver"})
            self.assertEqual(stats.fields()["STAT"], "actual")
            await exchange("gcre", {"NAME": "room"})
            self.assertEqual(received[-1].command, "gcre")
        finally:
            writer.close()
            await writer.wait_closed()
            for server in (proxy, dll_server, python_server):
                server.close()
                await server.wait_closed()
