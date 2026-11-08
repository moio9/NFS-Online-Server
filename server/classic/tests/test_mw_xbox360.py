"""Xbox MW account and byte-exact protocol boundaries."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
from pathlib import Path
import os
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from classic.games.most_wanted.xbox360.__main__ import create_runtime
from classic.games.most_wanted.xbox360.clock import xbox_lobby_heartbeat
from classic.games.most_wanted.xbox360.client_bridge import Bridge, local_news
from classic.accounts.credentials import CredentialStore
from classic.accounts.identity import IdentityStore
from classic.core.config import Endpoint
from classic.ea.directory import SessionDirectory
from classic.games.most_wanted.auth import create_auth_service
from classic.games.most_wanted.xbox360.lobby import XboxPrelogin
from classic.protocols.auth import ClassicAuthContext
from classic.protocols.frame import ClassicEAFrame, ClassicOpaqueValue
from classic.protocols.password import make_password_token
from classic.protocols.prelogin import ClassicPreloginContext, ClassicPreloginProfile, ClassicUserset
from classic.tests import test_combined_runtime


class Xbox360Tests(unittest.TestCase):
    def test_bridge_news_preserves_other_fields_and_resigns_local_urls(self):
        original = ClassicEAFrame.signed(
            'news', b'TOSA_URL=http://server.example/tos\nTOSAC_URL=http://server.example/tos\n'
            b'NEWS_URL=http://server.example/mw?lang=en\nBUDDY_SERVER=server.example\n\0',
            256, reserved=0x6e657737)
        wire = local_news(original, '127.0.0.6', 18080)
        frame = ClassicEAFrame.decode_one(wire)[0]
        self.assertEqual(frame.fields()['TOSA_URL'], 'http://127.0.0.6:18080/tos')
        self.assertEqual(frame.fields()['NEWS_URL'], 'http://127.0.0.6:18080/mw?lang=en')
        self.assertEqual(frame.fields()['BUDDY_SERVER'], 'server.example')
        self.assertEqual(frame.reserved, original.reserved)
        self.assertEqual(wire[-8:], hashlib.md5(wire[:-8]).digest()[:8])
        rejected = ClassicEAFrame('news', b'', 0x62616463)
        self.assertEqual(local_news(rejected, '127.0.0.6', 18080), rejected.encode())

    def test_lobby_heartbeat_uses_utc_across_timezone_boundaries(self):
        local = datetime(2026, 11, 8, 0, 1, 2, tzinfo=timezone(timedelta(hours=2)))
        frame = ClassicEAFrame.decode_one(xbox_lobby_heartbeat(local))[0]
        self.assertEqual(frame.command, "~png")
        self.assertEqual(frame.fields()["REF"], "2026.11.7-22:01:02")

    def test_xbox_join_uses_only_a_userset_the_player_actually_joined(self):
        credentials, identities = CredentialStore(), IdentityStore()
        outbox = {}
        service = XboxPrelogin(
            create_auth_service(credentials, identities, verify_passwords=False),
            profile=ClassicPreloginProfile(game_id="most_wanted"),
            control_endpoint=Endpoint("127.0.0.1", 13505), sessions=SessionDirectory(),
        )

        def context(name):
            credentials.create_account(name, "synthetic-password", persona=name)
            identity, token = identities.login(name, name)
            outbox[name] = []
            return ClassicPreloginContext(
                auth=ClassicAuthContext(connection_id=name, account=credentials.resolve_account(name),
                                        identity=identity, session_token=token, persona=name),
                authenticated=True, persona_selected=True, client_address="127.0.0.1",
                send_wire=lambda data: (outbox[name].append(data) or True),
            )

        host, guest, outsider = context("Host"), context("Guest"), context("Outsider")
        guest.auth.client_ip = "127.0.0.1"
        service.dispatch(ClassicEAFrame.from_fields("addr", {"ADDR": "127.0.0.2", "PORT": 3658}), guest)
        self.assertEqual(guest.client_address, "127.0.0.1")
        guest.auth.xbox_maddr = "$\xff\x80\xd0"
        selection = service.dispatch(ClassicEAFrame.from_fields("sele", {"MYGAME": 1}), guest)
        selected_frames = [ClassicEAFrame.decode_one(wire)[0] for wire in selection.frames]
        self.assertEqual([frame.command for frame in selected_frames[:2]], ["sele", "+who"])
        self.assertEqual(selected_frames[1].fields()["MA"], "$\xff\x80\xd0")
        self.assertEqual(selected_frames[1].fields()["I"], "2")
        game = service.sessions.create_game(0, host.auth.identity.user_id, name="AccountPersona",
                                            host_persona="Host", host_address="127.0.0.1")
        host.lobby_game_id = game.game_id
        userset = ClassicUserset(42, host.auth.identity.user_id, "Host", "XboxGamertag",
                                 game_id=game.game_id,
                                 members={host.auth.identity.user_id, guest.auth.identity.user_id})
        service._usersets[userset.userset_id] = userset
        for player in (host, guest, outsider):
            service._connections[player.auth.identity.user_id] = player
        host.userset_id = userset.userset_id
        updated_room = service.dispatch(ClassicEAFrame.from_fields(
            "uadm", {"CUSTFLAGS": "IJKMY-"}), host)
        self.assertEqual(updated_room.reason, "userset_updated")
        self.assertEqual(userset.custflags, "IJKMY-")
        self.assertEqual(game.custflags, "0")
        guest.userset_id = outsider.userset_id = userset.userset_id
        guest.mw_userset_staged_game_id = outsider.mw_userset_staged_game_id = game.game_id
        request = ClassicEAFrame.from_fields("gjoi", {"NAME": "XboxGamertag"})
        self.assertEqual(service.dispatch(request, outsider).reason, "game_join_rejected")
        joined = service.dispatch(request, guest)
        self.assertEqual(joined.reason, "game_joined")
        self.assertIn(guest.auth.identity.user_id, game.participants)
        host_frames = [ClassicEAFrame.decode_one(wire)[0] for wire in outbox["Host"]]
        self.assertEqual([frame.command for frame in host_frames],
                         ["+mgm", "+ust", "+usm", "+mgm"])
        self.assertEqual(host_frames[1].fields()["C"], "2")
        self.assertEqual(host_frames[2].fields()["N"], "Guest")
        self.assertEqual(host_frames[2].fields()["G"], str(game.game_id))
        guest_presence = ClassicEAFrame.decode_one(service._xbox_user_frame(guest, game))[0]
        self.assertEqual(guest_presence.fields().get("MA"), "$\xff\x80\xd0")
        self.assertTrue(any(frame.command == "+mgm" for frame in host_frames))
        self.assertEqual([ClassicEAFrame.decode_one(wire)[0].command for wire in joined.frames],
                         ["gjoi", "+who"])
        outbox["Host"].clear()
        reply = service.dispatch(ClassicEAFrame.from_fields(
            "auxi", {"TEXT": "SCF%3d0%0aLT%3d0%0a"}), guest)
        self.assertEqual([ClassicEAFrame.decode_one(wire)[0].command for wire in reply.frames],
                         ["auxi", "+usr", "+usm"])
        updated = [ClassicEAFrame.decode_one(wire)[0] for wire in outbox["Host"]]
        self.assertEqual([frame.command for frame in updated], ["+usr", "+usm"])
        self.assertEqual(updated[0].fields()["X"], "SCF%3d0%0aLT%3d0%0a")
        outbox["Host"].clear()
        reply = service.dispatch(ClassicEAFrame.from_fields(
            "mesg", {"TEXT": "42", "ATTR": "EGS"}), guest)
        self.assertEqual([ClassicEAFrame.decode_one(wire)[0].command for wire in reply.frames],
                         ["mesg"])
        message = ClassicEAFrame.decode_one(outbox["Host"][0])[0]
        self.assertEqual((message.command, message.fields()["F"], message.fields()["T"]),
                         ("+msg", "EGS", "42"))

    def test_game_bridge_and_sqlite_runtime_share_an_authenticated_account(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(
                test_combined_runtime.CombinedRuntimeTests._settings(root),
                enable_u2=False, enable_mw=True, auth_mode="password",
                account_db_path=str(root / "accounts.sqlite3"),
                account_files_path=str(root / "users"),
            )
            app, runtime = create_runtime(settings, bind_ip="127.0.0.11")
            self.assertIsNot(runtime.prelogin.ranking, app.ranking)
            app.credentials.create_account("Driver", "synthetic-password", persona="Driver")
            runtime.start()
            bridge = None
            try:
                bridge = subprocess.Popen(
                    [sys.executable, "-u", "-m", "classic.games.most_wanted.xbox360.client_bridge",
                     "--account", "Driver", "--listen-ip", "127.0.0.12", "--upstream-ip", "127.0.0.11"],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=os.environ.copy(),
                )
                bridge.stdin.write(b"synthetic-password\n")
                bridge.stdin.close()
                deadline = time.monotonic() + 8
                while True:
                    if bridge.poll() is not None:
                        self.fail("bridge failed to start")
                    try:
                        with socket.create_connection(("127.0.0.12", 30960), timeout=.2):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail("bridge not ready")
                        time.sleep(.05)

                def reply(sock):
                    header = b""
                    while len(header) < 12:
                        chunk = sock.recv(12 - len(header))
                        if not chunk:
                            raise EOFError("connection closed before response")
                        header += chunk
                    length = int.from_bytes(header[8:12], "big")
                    body = b""
                    while len(body) < length - 12:
                        chunk = sock.recv(length - 12 - len(body))
                        if not chunk:
                            raise EOFError("connection closed during response")
                        body += chunk
                    return ClassicEAFrame.decode_one(header + body)[0]

                with socket.create_connection(("127.0.0.12", 30960), timeout=3) as directory_socket:
                    directory_socket.sendall(ClassicEAFrame.from_fields("@dir", {"PROD": "nfs-xbox-2006"}).encode())
                    self.assertEqual(reply(directory_socket).fields()["ADDR"], "127.0.0.12")
                with socket.create_connection(("127.0.0.12", 10901), timeout=3) as lobby:
                    lobby.sendall(ClassicEAFrame.from_fields("auth", {"GTAG": "XboxTest"}).encode())
                    self.assertEqual(reply(lobby).fields()["NAME"], "Driver")
                    lobby.sendall(ClassicEAFrame.from_fields("pers", {"GTAG": "XboxTest"}).encode())
                    self.assertEqual(reply(lobby).fields()["PERS"], "Driver")
            finally:
                if bridge is not None:
                    bridge.terminate()
                    try:
                        bridge.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        bridge.kill()
                        bridge.wait()
                runtime.stop()

    def test_opaque_values_preserve_bytes_without_changing_text_encoding(self):
        frame = ClassicEAFrame.from_fields("gset", {
            "SESS": ClassicOpaqueValue(b"$\xff\x80\xd0"), "NAME": "\u0219ofer",
        }).encode()
        self.assertIn(b"SESS=$\xff\x80\xd0\n", frame)
        self.assertIn("NAME=\u0219ofer\n".encode("utf-8"), frame)

    def test_password_backed_xbox_runtime_uses_shared_accounts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(
                test_combined_runtime.CombinedRuntimeTests._settings(root),
                enable_u2=False, enable_mw=True, auth_mode="password",
                account_db_path=str(root / "accounts.sqlite3"),
                account_files_path=str(root / "users"),
            )
            with self.assertRaises(ValueError):
                create_runtime(settings, bind_ip="0.0.0.0")
            app, runtime = create_runtime(settings, directory_port=0, lobby_port=0)
            self.assertIs(runtime.lobby_heartbeat_wire, xbox_lobby_heartbeat)
            app.credentials.create_account("Driver", "synthetic-password", persona="Driver")
            directory_endpoint, lobby_endpoint = runtime.start()
            try:
                for password, accepted in (("wrong-password", False), ("synthetic-password", True)):
                    with socket.create_connection((directory_endpoint.host, directory_endpoint.port), timeout=3) as sock:
                        sock.sendall(ClassicEAFrame.from_fields("@dir", {"PROD": "nfs-xbox-2006"}).encode())
                        header = sock.recv(12)
                        while len(header) < 12:
                            header += sock.recv(12 - len(header))
                        length = int.from_bytes(header[8:12], "big")
                        body = b""
                        while len(body) < length - 12:
                            body += sock.recv(length - 12 - len(body))
                        mask = ClassicEAFrame.decode_one(header + body)[0].fields()["MASK"]
                    with socket.create_connection((lobby_endpoint.host, lobby_endpoint.port), timeout=3) as sock:
                        sock.sendall(ClassicEAFrame.from_fields("auth", {
                            "GTAG": "XboxTest", "NAME": "Driver",
                            "PASS": make_password_token(password, mask),
                        }).encode())
                        header = sock.recv(12)
                        while len(header) < 12:
                            header += sock.recv(12 - len(header))
                        length = int.from_bytes(header[8:12], "big")
                        body = b""
                        while len(body) < length - 12:
                            body += sock.recv(length - 12 - len(body))
                        reply = ClassicEAFrame.decode_one(header + body)[0]
                        self.assertEqual(reply.fields().get("NAME") == "Driver", accepted)
            finally:
                runtime.stop()


class XboxWebBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_proxy_forwards_request_and_response(self):
        received = []
        async def upstream(reader, writer):
            received.append(await reader.readuntil(b'\r\n\r\n'))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\nnews')
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(upstream, '127.0.0.1', 0)
        bridge = Bridge('127.0.0.1', '127.0.0.1', 'test', 'test',
                        web_upstream_port=server.sockets[0].getsockname()[1])
        proxy = await asyncio.start_server(bridge.handle_web, '127.0.0.1', 0)
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', proxy.sockets[0].getsockname()[1])
            writer.write(b'GET /mw HTTP/1.1\r\nHost: localhost\r\n\r\n')
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 3)
            self.assertTrue(response.endswith(b'\r\n\r\nnews'))
            self.assertTrue(received[0].startswith(b'GET /mw '))
            writer.close()
            await writer.wait_closed()
        finally:
            proxy.close()
            server.close()
            await proxy.wait_closed()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
