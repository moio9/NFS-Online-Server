from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("nfs_online", ROOT / "nfs_online.py")
assert SPEC and SPEC.loader
nfs_online = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nfs_online)

from common.config import (  # noqa: E402 - nfs_online adds server/ to sys.path
    carbon_service_values,
    classic_service_values,
    prepare_runtime_context as build_runtime_context,
)


class LauncherTests(unittest.TestCase):
    def test_normalize_games_preserves_canonical_order(self) -> None:
        self.assertEqual(nfs_online.normalize_games("carbon,u2"), ("u2", "carbon"))
        self.assertEqual(nfs_online.normalize_games("all"), nfs_online.VALID_GAMES)

    def test_normalize_aliases(self) -> None:
        self.assertEqual(
            nfs_online.normalize_games(["underground2", "most-wanted", "nfsc"]),
            nfs_online.VALID_GAMES,
        )

    def test_empty_selection_requires_explicit_permission(self) -> None:
        with self.assertRaises(nfs_online.LauncherError):
            nfs_online.normalize_games([], allow_empty=False)
        self.assertEqual(nfs_online.normalize_games([], allow_empty=True), ())

    def test_invalid_game_is_rejected(self) -> None:
        with self.assertRaises(nfs_online.LauncherError):
            nfs_online.normalize_games("prostreet")

    def test_output_labels(self) -> None:
        self.assertEqual(nfs_online.output_label("carbon", "anything"), "Carbon")
        self.assertEqual(nfs_online.output_label("classic", "underground2 connected"), "U2")
        self.assertEqual(nfs_online.output_label("classic", "most_wanted connected"), "MW")
        self.assertEqual(nfs_online.output_label("classic", "EA Messenger client"), "Messenger")
        self.assertEqual(nfs_online.output_label("classic", "EA race UDP route"), "Race")

    def test_service_commands_use_the_single_toml(self) -> None:
        for service in ("classic", "carbon"):
            command = nfs_online.command_for(service)
            self.assertEqual(command[-2:], ["--config", str(nfs_online.CONFIG_FILE)])
            self.assertNotIn("runtime", str(nfs_online.SERVICES[service]["config"]))

    def test_runtime_context_preserves_secret_without_generating_service_configs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "data" / "server-state.json"
            runtime_path = root / "runtime"
            runtime_path.mkdir()
            (runtime_path / "classic.cfg").write_text("obsolete=1\n", encoding="utf-8")
            (runtime_path / "carbon.cfg").write_text("obsolete=1\n", encoding="utf-8")
            environment = {
                "NFS_PUBLIC_HOST": "server.example",
                "NFS_PUBLIC_IPV4": "198.51.100.42",
                "NFS_LOCAL_IPV4": "192.168.1.50",
            }
            with (
                mock.patch.object(nfs_online, "STATE_PATH", state_path),
                mock.patch.object(nfs_online, "RUN_ROOT", runtime_path),
                mock.patch.dict("os.environ", environment, clear=False),
            ):
                first = nfs_online.prepare_runtime_context(("u2",))
                second = nfs_online.prepare_runtime_context(("u2",))
            self.assertGreaterEqual(len(first.ipc_secret), 32)
            self.assertEqual(first.ipc_secret, second.ipc_secret)
            self.assertEqual(first.environment()["NFS_GAMES"], "u2")
            self.assertFalse((runtime_path / "classic.cfg").exists())
            self.assertFalse((runtime_path / "carbon.cfg").exists())

    def test_session_listing_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            db = Path(raw) / "accounts.sqlite3"
            connection = sqlite3.connect(db)
            connection.executescript(
                """
                CREATE TABLE accounts(account_id INTEGER PRIMARY KEY, account_name TEXT, account_name_key TEXT);
                CREATE TABLE personas(persona_id INTEGER PRIMARY KEY, account_id INTEGER, display_name TEXT, display_name_key TEXT);
                CREATE TABLE active_sessions(
                    account_id INTEGER PRIMARY KEY, persona_id INTEGER, game TEXT,
                    server_id TEXT, connected_at REAL, heartbeat_at REAL, expires_at REAL
                );
                INSERT INTO accounts VALUES(1, 'Driver', 'driver');
                INSERT INTO personas VALUES(2, 1, 'Driver', 'driver');
                INSERT INTO active_sessions VALUES(1, 2, 'carbon', 'carbon-main', 1, 2, 4102444800);
                """
            )
            connection.commit()
            connection.close()
            with mock.patch.object(nfs_online, "configured_account_db", return_value=db):
                lines = nfs_online.session_lines()
                self.assertTrue(any("Driver" in line and "carbon" in line for line in lines))
                self.assertTrue(nfs_online.release_session("Driver"))
                self.assertEqual(nfs_online.session_lines(), ["No active sessions."])

    def test_top_level_configuration_loads(self) -> None:
        values = nfs_online.load_configuration()
        self.assertTrue(values["global"]["PUBLIC_HOST"])
        self.assertEqual(values["u2"]["LOBBY_PUBLIC_PORT"], "20922")
        self.assertEqual(values["u2"]["GAME_SIZE_POLICY"], "server")
        self.assertEqual(values["u2"]["GAME_MIN_PLAYERS"], "2")
        self.assertEqual(values["u2"]["GAME_MAX_PLAYERS"], "6")
        self.assertEqual(values["mw"]["LOBBY_PUBLIC_PORT"], "30920")
        self.assertEqual(values["mw"]["CREATE_ACCOUNT_ON_FIRST_LOGIN"], "0")
        self.assertEqual(values["carbon"]["FESL_PUBLIC_PORT"], "18210")

    def test_runtime_advertises_resolved_ipv4_and_clean_data_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_path = root / "config" / "server.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                nfs_online.CONFIG_FILE.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            environment = {
                "NFS_PUBLIC_HOST": "server.example",
                "NFS_PUBLIC_IPV4": "198.51.100.42",
                "NFS_LOCAL_IPV4": "192.168.1.50",
                "NFS_IPC_SECRET": "x" * 64,
            }
            context = build_runtime_context(
                config_path,
                ("u2",),
                state_path=root / "data" / "server-state.json",
                environment=environment,
            )
            classic_values = classic_service_values(
                config_path,
                games=("u2",),
                environment=context.environment(),
            )
            carbon_values = carbon_service_values(
                config_path,
                environment=context.environment(),
            )

            self.assertEqual(classic_values["ENABLE_U2"], "1")
            self.assertEqual(classic_values["ENABLE_MW"], "0")
            self.assertEqual(classic_values["U2_BOOTSTRAP_PUBLIC"], "198.51.100.42:20921")
            self.assertEqual(classic_values["U2_LOBBY_PUBLIC"], "198.51.100.42:20922")
            self.assertEqual(classic_values["MW_LOBBY_PUBLIC"], "198.51.100.42:30920")
            self.assertEqual(classic_values["MESSENGER_PUBLIC"], "198.51.100.42:13505")
            self.assertEqual(classic_values["AUTH_DATA"], str(root / "data" / "classic" / "auth.json"))
            self.assertEqual(classic_values["STATS_DATA"], str(root / "data" / "classic" / "stats.json"))
            self.assertEqual(carbon_values["FESL_PUBLIC"], "198.51.100.42:18210")
            self.assertEqual(carbon_values["THEATER_PUBLIC"], "198.51.100.42:18215")
            self.assertEqual(carbon_values["ACCOUNT_DATA"], str(root / "data" / "carbon" / "progression.json"))
            self.assertEqual(carbon_values["CARBON_DLC_CATALOG"], str(root / "data" / "carbon" / "dlc_catalog.json"))
            self.assertEqual(carbon_values["CARBON_JOIN_TIMEOUT_SECONDS"], "45")
            self.assertEqual(carbon_values["CARBON_RACE_IDLE_TIMEOUT_SECONDS"], "60")
            self.assertFalse((root / "runtime" / "classic.cfg").exists())
            self.assertFalse((root / "runtime" / "carbon.cfg").exists())

    def test_admin_passthrough_preserves_child_options(self) -> None:
        with mock.patch.object(nfs_online, "stats_command", return_value=17) as stats:
            result = nfs_online.main(
                ["stats", "--game", "mw", "list", "circuit", "--limit", "1"]
            )
        self.assertEqual(result, 17)
        stats.assert_called_once_with(
            ["--game", "mw", "list", "circuit", "--limit", "1"]
        )

        with mock.patch.object(nfs_online, "account_command", return_value=23) as account:
            result = nfs_online.main(
                ["account", "create", "Driver", "--persona", "Driver"]
            )
        self.assertEqual(result, 23)
        account.assert_called_once_with(
            ["create", "Driver", "--persona", "Driver"]
        )

        with mock.patch.object(nfs_online, "account_command", return_value=29) as kick:
            result = nfs_online.main(["kick", "Driver"])
        self.assertEqual(result, 29)
        kick.assert_called_once_with(["kick", "Driver"])

    def test_readiness_endpoint_honors_configured_port(self) -> None:
        self.assertEqual(
            nfs_online.readiness_endpoint("0.0.0.0:24567"),
            ("127.0.0.1", 24567),
        )
        self.assertEqual(
            nfs_online.readiness_endpoint("127.0.0.1:34567"),
            ("127.0.0.1", 34567),
        )

    def test_readiness_rejects_stale_listener_when_new_child_exits(self) -> None:
        process = mock.Mock()
        process.poll.side_effect = (None, 1)
        with mock.patch.object(nfs_online, "port_ready", return_value=True):
            self.assertFalse(
                nfs_online.wait_for_readiness(
                    process,
                    "127.0.0.1",
                    13506,
                    1.0,
                )
            )

    def test_readiness_accepts_stable_new_child(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        clock = iter((0.0, 0.0, 0.1, 0.25))
        with (
            mock.patch.object(nfs_online, "port_ready", return_value=True),
            mock.patch.object(nfs_online.time, "monotonic", side_effect=clock),
            mock.patch.object(nfs_online.time, "sleep"),
        ):
            self.assertTrue(
                nfs_online.wait_for_readiness(
                    process,
                    "127.0.0.1",
                    13506,
                    1.0,
                    stable_for=0.2,
                )
            )

    def test_color_policy(self) -> None:
        stream = io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(nfs_online.should_use_colors("always", stream))
            self.assertFalse(nfs_online.should_use_colors("never", stream))
            with mock.patch.dict("os.environ", {"NO_COLOR": "1"}):
                self.assertFalse(nfs_online.should_use_colors("always", stream))

    def test_colored_log_prefix_and_warning(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            printer = nfs_online.ConsolePrinter()
            printer.set_color_mode("always", persist=False)
            rendered = printer._format_log("U2", "2026-01-01 WARNING: probe")
        self.assertIn("\x1b[", rendered)
        self.assertIn("[U2]", rendered)
        self.assertIn("WARNING", rendered)

    def test_unknown_configuration_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = Path(raw) / "server.ini"
            config.write_text("[server]\nUNKNOWN_KEY=1\n", encoding="utf-8")
            with mock.patch.object(nfs_online, "CONFIG_FILE", config):
                with self.assertRaises(nfs_online.LauncherError):
                    nfs_online.load_named_config("global")

    def test_unknown_configuration_section_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = Path(raw) / "server.ini"
            config.write_text("[server]\nPUBLIC_HOST=localhost\n[obsolete]\nA=1\n", encoding="utf-8")
            with self.assertRaises(nfs_online.LauncherError):
                nfs_online.read_configuration_file(config)

    def test_u2_game_size_configuration_rejects_invalid_range(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = Path(raw) / "server.ini"
            config.write_text(
                "[server]\nPUBLIC_HOST=localhost\n"
                "[underground2]\nGAME_SIZE_POLICY=client\n"
                "GAME_MIN_PLAYERS=4\nGAME_MAX_PLAYERS=3\n",
                encoding="utf-8",
            )
            with mock.patch.object(nfs_online, "CONFIG_FILE", config):
                with self.assertRaisesRegex(nfs_online.LauncherError, "GAME_MIN_PLAYERS"):
                    nfs_online.load_configuration()

    def test_update_config_value_preserves_comments(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = Path(raw) / "server.ini"
            config.write_text(
                "# title\n[server]\n# public endpoint\nPUBLIC_HOST=old.example\n",
                encoding="utf-8",
            )
            nfs_online.update_config_value(config, "server", "PUBLIC_HOST", "new.example")
            text = config.read_text(encoding="utf-8")
            self.assertIn("# title", text)
            self.assertIn("# public endpoint", text)
            self.assertIn("PUBLIC_HOST=new.example", text)

    def test_update_client_hosts_preserves_ports_and_sections(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            client_root = Path(raw)
            folder = client_root / "carbon"
            folder.mkdir()
            config = folder / "net_carbon.ini"
            config.write_text(
                "[network]\nhost = old.example\nplasma_host = old.example\nport = 9000\n",
                encoding="utf-8",
            )
            with mock.patch.object(nfs_online, "CLIENT_ROOT", client_root):
                self.assertEqual(nfs_online.update_client_hosts("new.example"), 1)
            text = config.read_text(encoding="utf-8")
            self.assertIn("host = new.example", text)
            self.assertIn("plasma_host = new.example", text)
            self.assertIn("port = 9000", text)

    def test_update_config_value_writes_valid_toml_string(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = Path(raw) / "server.toml"
            config.write_text(
                "# title\n[server]\nCOLOR_MODE = \"auto\"\n",
                encoding="utf-8",
            )
            nfs_online.update_config_value(config, "server", "COLOR_MODE", "never")
            text = config.read_text(encoding="utf-8")
            self.assertIn("# title", text)
            self.assertIn('COLOR_MODE = "never"', text)
            values = nfs_online.read_configuration_file(config)
            self.assertEqual(values["global"]["COLOR_MODE"], "never")

    def test_dlc_admin_passthrough_preserves_child_options(self) -> None:
        with mock.patch.object(nfs_online, "dlc_admin_command", return_value=0) as command:
            self.assertEqual(
                nfs_online.main(["dlc", "list", "--category", "cars"]),
                0,
            )
        command.assert_called_once_with(["list", "--category", "cars"])


if __name__ == "__main__":
    unittest.main()
