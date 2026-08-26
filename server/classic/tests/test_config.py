from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from classic.core.config import ServerSettings


class ClassicConfigurationTests(unittest.TestCase):
    def test_sectioned_server_ini_can_select_only_u2(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config" / "server.ini"
            path.parent.mkdir(parents=True)
            path.write_text(
                "[server]\nPUBLIC_HOST=server.example\nIPC_SECRET=AUTO\n"
                "[underground2]\nGAME_SIZE_POLICY=server\nGAME_MIN_PLAYERS=2\nGAME_MAX_PLAYERS=6\n"
                "[most_wanted]\nCREATE_ACCOUNT_ON_FIRST_LOGIN=0\n"
                "[carbon]\n",
                encoding="utf-8",
            )
            environment = {
                "NFS_PUBLIC_HOST": "server.example",
                "NFS_PUBLIC_IPV4": "198.51.100.42",
                "NFS_LOCAL_IPV4": "192.168.1.50",
                "NFS_IPC_SECRET": "x" * 64,
            }
            with mock.patch.dict("os.environ", environment, clear=False):
                settings = ServerSettings.load(path, games=("u2",))
            self.assertTrue(settings.enable_u2)
            self.assertFalse(settings.enable_mw)
            self.assertEqual(settings.underground2.bootstrap_public.host, "198.51.100.42")
            self.assertEqual(settings.account_db_path, str(root / "data" / "accounts.sqlite3"))

    def test_mw_auto_enroll_can_be_enabled_without_enabling_u2(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "classic.cfg"
            path.write_text(
                "AUTH_AUTO_ENROLL=0\n"
                "U2_AUTH_AUTO_ENROLL=0\n"
                "MW_AUTH_AUTO_ENROLL=1\n",
                encoding="utf-8",
            )
            settings = ServerSettings.load(path)
            self.assertFalse(settings.auth_auto_enroll)
            self.assertFalse(settings.u2_auth_auto_enroll)
            self.assertTrue(settings.mw_auth_auto_enroll)

    def test_per_game_auto_enroll_falls_back_to_global_policy(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "classic.cfg"
            path.write_text("AUTH_AUTO_ENROLL=1\n", encoding="utf-8")
            settings = ServerSettings.load(path)
            self.assertTrue(settings.u2_auth_auto_enroll)
            self.assertTrue(settings.mw_auth_auto_enroll)

    def test_u2_game_size_policy_loads(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "classic.cfg"
            path.write_text(
                "U2_GAME_SIZE_POLICY=server\n"
                "U2_GAME_MIN_PLAYERS=3\n"
                "U2_GAME_MAX_PLAYERS=6\n",
                encoding="utf-8",
            )
            settings = ServerSettings.load(path)
            self.assertEqual(settings.u2_game_size_policy, "server")
            self.assertEqual(settings.u2_game_min_players, 3)
            self.assertEqual(settings.u2_game_max_players, 6)

    def test_u2_game_size_policy_rejects_invalid_range(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "classic.cfg"
            path.write_text(
                "U2_GAME_SIZE_POLICY=client\n"
                "U2_GAME_MIN_PLAYERS=4\n"
                "U2_GAME_MAX_PLAYERS=3\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "must not exceed"):
                ServerSettings.load(path)


if __name__ == "__main__":
    unittest.main()
