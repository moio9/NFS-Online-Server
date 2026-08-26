"""Runtime smoke tests for config and the real Carbon TCP listener."""

import socket
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch

from common.enforcement import AccountPolicyEvent, LiveAccountConnectionRegistry
from carbon.accounts.identity import IdentityStore
from carbon.core.catalog import GameId
from carbon.core.config import Endpoint, ServerSettings
from carbon.app import CarbonApplication
from carbon.fesl.frame import FESLFrame, FESLStreamDecoder
from carbon.runtime.theater import handle_theater_connection
from carbon.theater.directory import CarbonGameDirectory
from carbon.theater.service import CarbonTheaterService


class ConfigurationTests(unittest.TestCase):
    def test_sectioned_server_ini_loads_carbon_directly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config" / "server.ini"
            path.parent.mkdir(parents=True)
            path.write_text(
                "[server]\nPUBLIC_HOST=server.example\nIPC_SECRET=AUTO\n"
                "[underground2]\n"
                "[most_wanted]\n"
                "[carbon]\nJOIN_TIMEOUT_SECONDS=45\nRACE_IDLE_TIMEOUT_SECONDS=60\n",
                encoding="utf-8",
            )
            environment = {
                "NFS_PUBLIC_HOST": "server.example",
                "NFS_PUBLIC_IPV4": "198.51.100.42",
                "NFS_LOCAL_IPV4": "192.168.1.50",
                "NFS_IPC_SECRET": "x" * 64,
            }
            with patch.dict("os.environ", environment, clear=False):
                settings = ServerSettings.load(path)
            self.assertEqual(settings.fesl_public.host, "198.51.100.42")
            self.assertEqual(
                settings.account_db_path,
                str(root / "data" / "accounts.sqlite3"),
            )
            self.assertEqual(settings.carbon_join_timeout_seconds, 45.0)
            self.assertEqual(settings.carbon_loading_ready_fallback_seconds, 8.0)

    def test_loads_strict_release_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.cfg"
            path.write_text(
                "GAME=carbon\nFESL_LISTEN=127.0.0.1:0\n"
                "MESSENGER_IPC_SECRET=test-secret\n"
                "FESL_PUBLIC=carbon.example:18210\n"
                "FESL_ACTIVITY_TIMEOUT=600\n"
                "FESL_HEARTBEAT_INTERVAL=25\n"
                "MESSENGER_PUBLIC=carbon.example:13505\n"
                "THEATER_PUBLIC=carbon.example:18215\n"
                "RACE_LOCAL=192.168.1.150:19118\n"
                "AUTH_MODE=password\n"
                "AUTH_DATA=auth.json\n"
                "AUTH_AUTO_ENROLL=1\n"
                "AUTH_FAILURE_LIMIT=3\n"
                "AUTH_LOCKOUT_SECONDS=45\n"
                "AUTH_LOGIN_ERROR_PROBE_CODE=101\n"
                "CARBON_CHALLENGE_QUICK_JOIN_BEFORE_READY=1\n"
                "CARBON_CHALLENGE_QUICK_JOIN_AFTER_READY=1\n"
                "CARBON_JOIN_TIMEOUT_SECONDS=12\n"
                "CARBON_RACE_IDLE_TIMEOUT_SECONDS=34\n"
                "CARBON_LOADING_READY_FALLBACK_SECONDS=7\n"
                "CARBON_DLC_CATALOG=dlc_catalog.json\n"
                "CARBON_DLC_ASSIGNMENTS=dlc_assignments.json\n"
                "MAD_CAMPAIGNS=campaigns.json\n"
                "MAD_ROTATION_SECONDS=120\n"
                "MAD_SESSION_TIMEOUT_SECONDS=30\n"
                "MAD_IMPRESSION_LOG=impressions.jsonl\n",
                encoding="utf-8",
            )
            settings = ServerSettings.load(path)
        self.assertIs(settings.game, GameId.CARBON)
        self.assertEqual(settings.fesl_listen, Endpoint("127.0.0.1", 0))
        self.assertEqual(settings.theater_public.port, 18215)
        self.assertEqual(settings.fesl_activity_timeout, 600)
        self.assertEqual(settings.fesl_heartbeat_interval, 25.0)
        self.assertEqual(settings.race_local, Endpoint("192.168.1.150", 19118))
        self.assertEqual(settings.auth_mode, "password")
        self.assertEqual(settings.auth_data_path, "auth.json")
        self.assertTrue(settings.auth_auto_enroll)
        self.assertEqual(settings.auth_failure_limit, 3)
        self.assertEqual(settings.auth_lockout_seconds, 45.0)
        self.assertEqual(settings.auth_login_error_probe_code, 101)
        self.assertTrue(settings.carbon_challenge_quick_join_before_ready)
        self.assertTrue(settings.carbon_challenge_quick_join_after_ready)
        self.assertEqual(settings.carbon_join_timeout_seconds, 12.0)
        self.assertEqual(settings.carbon_race_idle_timeout_seconds, 34.0)
        self.assertEqual(settings.carbon_loading_ready_fallback_seconds, 7.0)
        self.assertEqual(settings.carbon_dlc_catalog_path, "dlc_catalog.json")
        self.assertEqual(settings.carbon_dlc_assignments_path, "dlc_assignments.json")
        self.assertFalse(settings.carbon_dlc_store_enabled)
        self.assertEqual(settings.mad_campaigns_path, "campaigns.json")
        self.assertEqual(settings.mad_rotation_seconds, 120)
        self.assertEqual(settings.mad_session_timeout_seconds, 30.0)
        self.assertEqual(settings.mad_impression_log_path, "impressions.jsonl")

    def test_rejects_out_of_range_login_error_probe_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.cfg"
            path.write_text(
                "GAME=carbon\nMESSENGER_IPC_SECRET=test-secret\n"
                "AUTH_LOGIN_ERROR_PROBE_CODE=65536\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError,
                "AUTH_LOGIN_ERROR_PROBE_CODE",
            ):
                ServerSettings.load(path)


    def test_rejects_negative_mad_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.cfg"
            path.write_text(
                "GAME=carbon\nMESSENGER_IPC_SECRET=test-secret\nMAD_ROTATION_SECONDS=-1\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "MAD_ROTATION_SECONDS"):
                ServerSettings.load(path)

    def test_rejects_non_positive_mad_session_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.cfg"
            path.write_text(
                "GAME=carbon\nMESSENGER_IPC_SECRET=test-secret\nMAD_SESSION_TIMEOUT_SECONDS=0\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "MAD_SESSION_TIMEOUT_SECONDS"):
                ServerSettings.load(path)

    def test_rejects_non_positive_carbon_lifecycle_timeouts(self) -> None:
        for key in (
            "CARBON_JOIN_TIMEOUT_SECONDS",
            "CARBON_RACE_IDLE_TIMEOUT_SECONDS",
            "CARBON_LOADING_READY_FALLBACK_SECONDS",
        ):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "server.cfg"
                path.write_text(
                    f"GAME=carbon\nMESSENGER_IPC_SECRET=test-secret\n{key}=0\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, key):
                    ServerSettings.load(path)


class CarbonRuntimeTests(unittest.TestCase):
    def test_public_race_hostname_is_advertised_as_numeric_ipv4(self) -> None:
        settings = ServerSettings(
            game=GameId.CARBON,
            fesl_listen=Endpoint("127.0.0.1", 0),
            fesl_public=Endpoint("127.0.0.1", 0),
            messenger_public=Endpoint("127.0.0.1", 13505),
            theater_public=Endpoint("127.0.0.1", 18215),
            race_public=Endpoint("server.example.com", 19118),
            race_local=Endpoint("192.168.1.150", 19118),
        )
        resolved = [
            (
                socket.AF_INET,
                socket.SOCK_DGRAM,
                socket.IPPROTO_UDP,
                "",
                ("203.0.113.234", 19118),
            )
        ]

        with patch("carbon.app.socket.getaddrinfo", return_value=resolved):
            app = CarbonApplication(settings)

        self.assertEqual(
            app.games.race_endpoint,
            Endpoint("203.0.113.234", 19118),
        )
        self.assertEqual(
            app.games.local_race_endpoint,
            Endpoint("192.168.1.150", 19118),
        )

    def test_real_tcp_hello_round_trip(self) -> None:
        settings = ServerSettings(
            game=GameId.CARBON,
            fesl_listen=Endpoint("127.0.0.1", 0),
            fesl_public=Endpoint("127.0.0.1", 0),
            messenger_public=Endpoint("127.0.0.1", 13505),
            theater_public=Endpoint("127.0.0.1", 18215),
            race_public=Endpoint("127.0.0.1", 19118),
            theater_listen=Endpoint("127.0.0.1", 0),
            race_listen=Endpoint("127.0.0.1", 0),
            connection_timeout=0.05,
            fesl_heartbeat_interval=0.1,
        )
        app = CarbonApplication(settings)
        endpoint = app.start()
        try:
            with socket.create_connection((endpoint.host, endpoint.port), timeout=2.0) as client:
                request = FESLFrame.from_fields("fsys", {"TXN": "Hello"}, transaction=11)
                client.sendall(request.encode())
                client.settimeout(2.0)
                decoder = FESLStreamDecoder()
                replies = []
                while len(replies) < 2:
                    replies.extend(decoder.feed(client.recv(8192)))
            self.assertEqual([reply.fields["TXN"] for reply in replies], ["Hello", "MemCheck"])
            self.assertEqual(replies[0].transaction, 11)

            udp_endpoint = app.race_listener.bound_endpoint
            connect = bytes.fromhex("00005cc5de2eb7abc2a6cea5146dc038618c927b")
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.settimeout(2.0)
                udp.sendto(connect, (udp_endpoint.host, udp_endpoint.port))
                response, _ = udp.recvfrom(2048)
            self.assertEqual(
                response.hex(),
                "0005b3623213fa0a68dcfdd54cfdbaed8aa2b34bee8fa1b4d842ce529bbc",
            )
        finally:
            app.stop()

    def test_idle_fesl_socket_survives_poll_timeout_and_echoes(self) -> None:
        settings = ServerSettings(
            game=GameId.CARBON,
            fesl_listen=Endpoint("127.0.0.1", 0),
            fesl_public=Endpoint("127.0.0.1", 0),
            messenger_public=Endpoint("127.0.0.1", 13505),
            theater_public=Endpoint("127.0.0.1", 18215),
            theater_listen=Endpoint("127.0.0.1", 0),
            race_public=Endpoint("127.0.0.1", 19118),
            race_listen=Endpoint("127.0.0.1", 0),
            connection_timeout=0.05,
            fesl_heartbeat_interval=0.1,
        )
        app = CarbonApplication(settings)
        endpoint = app.start()
        try:
            with socket.create_connection((endpoint.host, endpoint.port), timeout=2.0) as client:
                time.sleep(0.2)
                client.sendall(
                    FESLFrame.from_fields(
                        "ECHO", {"TID": "1", "TYPE": "1", "TXN": "ECHO"}, transaction=0
                    ).encode()
                )
                client.settimeout(2.0)
                decoder = FESLStreamDecoder()
                frames = []
                while not any(frame.command == "ECHO" for frame in frames):
                    frames.extend(decoder.feed(client.recv(8192)))
                self.assertTrue(
                    any(
                        frame.command == "fsys" and frame.fields.get("TXN") == "Ping"
                        for frame in frames
                    )
                )
                reply = next(frame for frame in frames if frame.command == "ECHO")
                self.assertEqual(reply.command, "ECHO")
                self.assertEqual(reply.fields["ERR"], "0")
        finally:
            app.stop()

    def test_authenticated_fesl_receives_periodic_ping_and_accepts_ack(self) -> None:
        settings = ServerSettings(
            game=GameId.CARBON,
            fesl_listen=Endpoint("127.0.0.1", 0),
            fesl_public=Endpoint("127.0.0.1", 0),
            messenger_public=Endpoint("127.0.0.1", 13505),
            theater_public=Endpoint("127.0.0.1", 18215),
            theater_listen=Endpoint("127.0.0.1", 0),
            race_public=Endpoint("127.0.0.1", 19118),
            race_listen=Endpoint("127.0.0.1", 0),
            connection_timeout=0.05,
            fesl_heartbeat_interval=0.1,
            auth_mode="open",
        )
        app = CarbonApplication(settings)
        endpoint = app.start()
        try:
            with socket.create_connection((endpoint.host, endpoint.port), timeout=2.0) as client:
                client.sendall(
                    FESLFrame.from_fields(
                        "acct",
                        {"TXN": "Login", "name": "Driver"},
                        transaction=8,
                    ).encode()
                )
                client.settimeout(2.0)
                decoder = FESLStreamDecoder()
                frames = []
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and not any(
                    frame.command == "fsys" and frame.fields.get("TXN") == "Ping"
                    for frame in frames
                ):
                    frames.extend(decoder.feed(client.recv(8192)))

                ping = next(
                    frame
                    for frame in frames
                    if frame.command == "fsys" and frame.fields.get("TXN") == "Ping"
                )
                self.assertEqual(ping.transaction, 0)
                client.sendall(
                    FESLFrame.from_fields(
                        "fsys",
                        {"TXN": "Ping"},
                        transaction=0x80000000,
                    ).encode()
                )
                time.sleep(0.05)
        finally:
            app.stop()

    def test_pre_auth_fesl_receives_periodic_ping(self) -> None:
        settings = ServerSettings(
            game=GameId.CARBON,
            fesl_listen=Endpoint("127.0.0.1", 0),
            fesl_public=Endpoint("127.0.0.1", 0),
            messenger_public=Endpoint("127.0.0.1", 13505),
            theater_public=Endpoint("127.0.0.1", 18215),
            theater_listen=Endpoint("127.0.0.1", 0),
            race_public=Endpoint("127.0.0.1", 19118),
            race_listen=Endpoint("127.0.0.1", 0),
            connection_timeout=0.05,
            fesl_heartbeat_interval=0.1,
        )
        app = CarbonApplication(settings)
        endpoint = app.start()
        try:
            with socket.create_connection((endpoint.host, endpoint.port), timeout=2.0) as client:
                client.settimeout(2.0)
                ping = FESLStreamDecoder().feed(client.recv(8192))[0]
                self.assertEqual(ping.command, "fsys")
                self.assertEqual(ping.fields, {"TXN": "Ping"})
                self.assertEqual(ping.transaction, 0)
        finally:
            app.stop()

    def test_live_ban_drains_native_banned_message_then_closes_fesl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = ServerSettings(
                game=GameId.CARBON,
                fesl_listen=Endpoint("127.0.0.1", 0),
                fesl_public=Endpoint("127.0.0.1", 0),
                messenger_public=Endpoint("127.0.0.1", 13505),
                theater_public=Endpoint("127.0.0.1", 18215),
                theater_listen=Endpoint("127.0.0.1", 0),
                race_public=Endpoint("127.0.0.1", 19118),
                race_listen=Endpoint("127.0.0.1", 0),
                connection_timeout=0.05,
                fesl_heartbeat_interval=30.0,
                auth_mode="password",
                account_db_path=str(root / "accounts.sqlite3"),
                account_files_path=str(root / "users"),
            )
            app = CarbonApplication(settings)
            self.assertIsNotNone(app.account_database)
            assert app.account_database is not None
            app.account_database.create_account(
                "Driver",
                "secret",
                persona="Driver",
            )
            endpoint = app.start()
            try:
                with socket.create_connection(
                    (endpoint.host, endpoint.port), timeout=2.0
                ) as client:
                    client.settimeout(2.0)
                    decoder = FESLStreamDecoder()
                    client.sendall(
                        FESLFrame.from_fields(
                            "acct",
                            {
                                "TXN": "Login",
                                "name": "Driver",
                                "password": "secret",
                            },
                            transaction=0x80000008,
                        ).encode()
                    )
                    frames = []
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline and not any(
                        frame.command == "acct"
                        and frame.fields.get("TXN") == "Login"
                        and "lkey" in frame.fields
                        for frame in frames
                    ):
                        frames.extend(decoder.feed(client.recv(8192)))
                    self.assertTrue(
                        any(
                            frame.command == "acct"
                            and frame.fields.get("TXN") == "Login"
                            and "lkey" in frame.fields
                            for frame in frames
                        )
                    )

                    app.account_database.set_banned("Driver", True)
                    policy = None
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline and policy is None:
                        data = client.recv(8192)
                        if not data:
                            break
                        for frame in decoder.feed(data):
                            if (
                                frame.command == "acct"
                                and frame.fields.get("TXN") == "Login"
                                and frame.fields.get("errorCode") == "103"
                            ):
                                policy = frame
                                break
                    self.assertIsNotNone(policy)
                    assert policy is not None
                    self.assertEqual(
                        policy.fields["localizedMessage"],
                        '"This account has been banned. Contact Customer Support."',
                    )
                    # The native message must be observable before EOF.  The
                    # transport remains read-only for a bounded drain window,
                    # then closes without requiring another client command.
                    client.settimeout(0.25)
                    with self.assertRaises(socket.timeout):
                        client.recv(8192)
                    client.settimeout(3.0)
                    try:
                        closed = client.recv(8192)
                    except ConnectionResetError:
                        closed = b""
                    self.assertEqual(closed, b"")

                with app.account_database.connect() as connection:
                    active = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM active_sessions"
                        ).fetchone()[0]
                    )
                self.assertEqual(active, 0)
            finally:
                app.stop()

    def test_live_ban_quiesces_theater_until_shared_drain_deadline(self) -> None:
        identities = IdentityStore(token_factory=lambda: "theater-key.")
        identity, token = identities.login("DriverAccount", "Driver")
        service = CarbonTheaterService(
            identities,
            CarbonGameDirectory(Endpoint("127.0.0.1", 19118)),
        )
        registry = LiveAccountConnectionRegistry(name="theater-policy-test")
        settings = SimpleNamespace(connection_timeout=0.05, max_frame_size=65_535)
        stop_event = Event()
        server, client = socket.socketpair()

        def run_server() -> None:
            try:
                handle_theater_connection(
                    server,
                    ("127.0.0.1", 4501),
                    stop_event,
                    settings=settings,
                    service=service,
                    live_connections=registry,
                )
            finally:
                server.close()

        thread = Thread(target=run_server, daemon=True)
        thread.start()
        try:
            client.settimeout(2.0)
            client.sendall(
                FESLFrame.from_fields(
                    "CONN",
                    {"TID": "1", "PROT": "2"},
                    transaction=0,
                ).encode()
            )
            client.sendall(
                FESLFrame.from_fields(
                    "USER",
                    {"TID": "2", "LKEY": token},
                    transaction=0,
                ).encode()
            )
            decoder = FESLStreamDecoder()
            frames: list[FESLFrame] = []
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not any(
                frame.command == "USER" and frame.fields.get("NAME") == "Driver"
                for frame in frames
            ):
                frames.extend(decoder.feed(client.recv(8192)))
            self.assertTrue(
                any(
                    frame.command == "USER"
                    and frame.fields.get("NAME") == "Driver"
                    for frame in frames
                )
            )

            result = registry.enforce(
                AccountPolicyEvent(
                    1,
                    identity.user_id,
                    identity.account_name,
                    "ban",
                    1.0,
                )
            )
            self.assertEqual(result.matched, 1)
            self.assertEqual(result.notified, 0)
            self.assertEqual(result.closing, 1)

            client.settimeout(0.25)
            with self.assertRaises(socket.timeout):
                client.recv(8192)
            client.settimeout(3.0)
            try:
                closed = client.recv(8192)
            except ConnectionResetError:
                closed = b""
            self.assertEqual(closed, b"")
            thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(registry), 0)
        finally:
            stop_event.set()
            client.close()
            server.close()
            thread.join(timeout=1.0)


class ApplicationLifecycleTests(unittest.TestCase):
    @staticmethod
    def _shell() -> CarbonApplication:
        app = object.__new__(CarbonApplication)
        app.account_policy_monitor = Mock()
        app.settings = Mock(
            auth_login_error_probe_code=None,
            auth_mode="password",
        )
        app.messenger_ipc = Mock()
        app.listener = Mock()
        app.listener.start.return_value = Endpoint("127.0.0.1", 1)
        app.theater_listener = Mock()
        app.theater_listener.start.return_value = Endpoint("127.0.0.1", 2)
        app.race_listener = Mock()
        app.race_listener.start.return_value = Endpoint("127.0.0.1", 3)
        app.mad_listener = None
        app.mad = None
        return app

    def test_policy_monitor_stops_when_messenger_ipc_start_fails(self) -> None:
        app = self._shell()
        app.messenger_ipc.start.side_effect = RuntimeError("IPC start failed")

        with self.assertRaisesRegex(RuntimeError, "IPC start failed"):
            app.start()

        app.account_policy_monitor.start.assert_called_once_with()
        app.account_policy_monitor.stop.assert_called_once_with()
        app.listener.start.assert_not_called()

    def test_all_started_services_roll_back_when_mad_start_fails(self) -> None:
        app = self._shell()
        app.mad_listener = Mock()
        app.mad = Mock()
        app.mad.start.side_effect = RuntimeError("MAD start failed")

        with self.assertRaisesRegex(RuntimeError, "MAD start failed"):
            app.start()

        app.mad_listener.start.assert_not_called()
        app.race_listener.stop.assert_called_once_with()
        app.theater_listener.stop.assert_called_once_with()
        app.listener.stop.assert_called_once_with()
        app.messenger_ipc.stop.assert_called_once_with()
        app.account_policy_monitor.stop.assert_called_once_with()
