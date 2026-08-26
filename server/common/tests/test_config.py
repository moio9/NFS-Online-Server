from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import common.config as config_module

from common.config import (
    ConfigurationError,
    carbon_service_values,
    classic_service_values,
    prepare_runtime_context,
    read_configuration_file,
)


MINIMAL_CONFIG = """\
[server]
PUBLIC_HOST = "server.example"
IPC_SECRET = "AUTO"
DEFAULT_GAMES = ["u2", "mw", "carbon"]

[underground2]
GAME_SIZE_POLICY = "server"
GAME_MIN_PLAYERS = 2
GAME_MAX_PLAYERS = 6

[most_wanted]
CREATE_ACCOUNT_ON_FIRST_LOGIN = false

[carbon]
JOIN_TIMEOUT_SECONDS = 45
RACE_IDLE_TIMEOUT_SECONDS = 60
LOADING_READY_FALLBACK_SECONDS = 8
DLC_STORE_ENABLED = false
"""


class DeploymentConfigurationTests(unittest.TestCase):
    def _config(self, root: Path) -> Path:
        path = root / "config" / "server.toml"
        path.parent.mkdir(parents=True)
        path.write_text(MINIMAL_CONFIG, encoding="utf-8")
        return path

    def test_sectioned_toml_is_strict_and_uses_documented_defaults(self) -> None:
        with TemporaryDirectory() as temporary:
            path = self._config(Path(temporary))
            values = read_configuration_file(path)
            self.assertEqual(values["global"]["PUBLIC_HOST"], "server.example")
            self.assertEqual(values["u2"]["LOBBY_PUBLIC_PORT"], "20922")
            self.assertEqual(values["carbon"]["FESL_PUBLIC_PORT"], "18210")
            with mock.patch.object(config_module, "_tomllib", None):
                fallback = read_configuration_file(path)
            self.assertEqual(fallback, values)

            path.write_text(MINIMAL_CONFIG + "\n[obsolete]\nA=1\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "unknown"):
                read_configuration_file(path)

    def test_legacy_ini_remains_readable_for_migration(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "server.ini"
            path.write_text(
                "[server]\nPUBLIC_HOST=legacy.example\n[carbon]\nDLC_STORE_ENABLED=0\n",
                encoding="utf-8",
            )
            values = read_configuration_file(path)
            self.assertEqual(values["global"]["PUBLIC_HOST"], "legacy.example")
            self.assertEqual(values["carbon"]["DLC_STORE_ENABLED"], "0")

    def test_runtime_state_is_persistent_but_service_derivation_is_read_only(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self._config(root)
            state = root / "data" / "server-state.json"
            environment = {
                "NFS_PUBLIC_HOST": "server.example",
                "NFS_PUBLIC_IPV4": "198.51.100.42",
                "NFS_LOCAL_IPV4": "192.168.1.50",
            }
            first = prepare_runtime_context(
                path,
                ("u2",),
                state_path=state,
                environment=environment,
            )
            second = prepare_runtime_context(
                path,
                ("u2",),
                state_path=state,
                environment=environment,
            )
            self.assertEqual(first.ipc_secret, second.ipc_secret)
            self.assertEqual(first.environment()["NFS_GAMES"], "u2")

            before = state.read_bytes()
            classic = classic_service_values(
                path,
                games=("u2",),
                environment=first.environment(),
            )
            carbon = carbon_service_values(path, environment=first.environment())
            self.assertEqual(state.read_bytes(), before)
            self.assertEqual(classic["ENABLE_U2"], "1")
            self.assertEqual(classic["ENABLE_MW"], "0")
            self.assertEqual(carbon["MESSENGER_IPC_SECRET"], first.ipc_secret)
            self.assertEqual(
                carbon["CARBON_LOADING_READY_FALLBACK_SECONDS"],
                "8",
            )

    def test_service_derivation_never_creates_runtime_config_files(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self._config(root)
            environment = {
                "NFS_PUBLIC_HOST": "server.example",
                "NFS_PUBLIC_IPV4": "198.51.100.42",
                "NFS_LOCAL_IPV4": "192.168.1.50",
                "NFS_IPC_SECRET": "x" * 64,
            }
            classic_service_values(path, games=("u2", "mw"), environment=environment)
            carbon_service_values(path, environment=environment)
            self.assertFalse((root / "runtime" / "classic.cfg").exists())
            self.assertFalse((root / "runtime" / "carbon.cfg").exists())


if __name__ == "__main__":
    unittest.main()
