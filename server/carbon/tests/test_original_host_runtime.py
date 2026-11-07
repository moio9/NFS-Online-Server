import socket
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from carbon.app import CarbonApplication
from carbon.core.catalog import GameId
from carbon.core.config import Endpoint, ServerSettings
from carbon.accounts.sqlite_backend import SQLiteCredentialStore
from common.accounts import SQLiteAccountDatabase

DATA = Path(__file__).resolve().parents[3] / 'data' / 'carbon'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class RuntimeTests(unittest.TestCase):
    def settings(self, marker, port, **changes):
        values = dict(game=GameId.CARBON, fesl_listen=Endpoint('127.0.0.1', 0),
                      fesl_public=Endpoint('127.0.0.1', 18210), messenger_public=Endpoint('127.0.0.1', 13505),
                      theater_public=Endpoint('127.0.0.1', 18215), theater_listen=Endpoint('127.0.0.1', 0),
                      race_public=Endpoint('127.0.0.1', 19119), race_listen=Endpoint('127.0.0.1', 0),
                      auth_mode='password', account_data_path=None, auth_data_path=None,
                      carbon_dlc_catalog_path=str(DATA / 'dlc_catalog.json'),
                      carbon_dlc_assignments_path=str(DATA / 'dlc_assignments.json'),
                      carbon_original_host_account='Host', carbon_original_host_udp_port=19140,
                      carbon_original_stream_port=port, carbon_original_restart_marker=str(marker))
        values.update(changes)
        return ServerSettings(**values)

    def test_optional_runtime_starts_on_loopback_and_releases_port(self):
        with TemporaryDirectory() as tmp:
            port = free_port()
            app = CarbonApplication(self.settings(Path(tmp) / 'restart', port))
            app.start()
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=2) as client:
                    client.sendall(b'\x01x')
                    self.assertEqual(client.recv(1), b'')  # Unknown session cannot open a stream.
            finally:
                app.stop()
            with socket.socket() as check:
                check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                check.bind(('127.0.0.1', port))

    def test_feature_requires_password_and_marker(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                CarbonApplication(self.settings(Path(tmp) / 'restart', free_port(), auth_mode='open'))
            with self.assertRaises(ValueError):
                CarbonApplication(self.settings('', free_port()))

    def test_toml_carries_explicit_original_host_configuration(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'server.toml'
            config.write_text('[server]\nPUBLIC_HOST="127.0.0.1"\nLOCAL_ADVERTISE_HOST="127.0.0.1"\n'
                              'IPC_SECRET="test-only-test-only-test-only-test-only"\n[carbon]\nORIGINAL_HOST_ACCOUNT="Host"\n'
                              'ORIGINAL_HOST_UDP_PORT="19340"\nORIGINAL_STREAM_PORT="19341"\n'
                              'ORIGINAL_RESTART_MARKER="runtime/original.restart"\n'
                              'ORIGINAL_SEED_ACCOUNT="Seed"\nORIGINAL_PLAY_NOW_EARLY_START=true\n')
            settings = ServerSettings.load(config)
            self.assertEqual(settings.carbon_original_host_account, 'Host')
            self.assertEqual(settings.carbon_original_host_udp_port, 19340)
            self.assertEqual(settings.carbon_original_stream_port, 19341)
            self.assertEqual(Path(settings.carbon_original_restart_marker), root / 'runtime/original.restart')
            self.assertEqual(settings.carbon_original_seed_account, 'Seed')
            self.assertTrue(settings.carbon_original_play_now_early_start)

    def test_runtime_rejects_missing_enabled_or_shared_seed_identity(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = SQLiteAccountDatabase(root/'accounts.sqlite3', root/'users')
            credentials = SQLiteCredentialStore(database)
            credentials.create_account('Seed', 'discarded-test-password')
            common = dict(account_db_path=str(database.path), account_files_path=str(root/'users'))
            with self.assertRaises(ValueError):
                CarbonApplication(self.settings(root/'marker', free_port(), carbon_original_seed_account='Missing', **common))
            with self.assertRaises(ValueError):
                CarbonApplication(self.settings(root/'marker', free_port(), carbon_original_seed_account='Seed', **common))
            with self.assertRaises(ValueError):
                CarbonApplication(self.settings(root/'marker', free_port(), carbon_original_seed_account='Host', **common))

    def test_unrelated_account_without_persona_does_not_block_initializer(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = SQLiteAccountDatabase(root/'accounts.sqlite3', root/'users')
            credentials = SQLiteCredentialStore(database)
            credentials.create_account('Incomplete', 'test-password')
            credentials.create_account('Seed', 'discarded-test-password', enabled=False)
            with database.transaction() as connection:
                connection.execute("DELETE FROM personas WHERE account_id=(SELECT account_id FROM accounts WHERE account_name_key='incomplete')")
            with self.assertRaises(ValueError):
                credentials.accounts()
            app = CarbonApplication(self.settings(root/'marker', free_port(),
                account_db_path=str(database.path), account_files_path=str(root/'users'),
                carbon_original_seed_account='Seed'))
            app.start()
            try:
                self.assertIsNotNone(app.original_host.seed_coordinator)
            finally:
                app.stop()

    def test_runtime_installs_and_removes_seed_coordinator_and_uses_bound_theater_port(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = SQLiteAccountDatabase(root/'accounts.sqlite3', root/'users')
            credentials = SQLiteCredentialStore(database)
            credentials.create_account('Seed', 'discarded-test-password')
            credentials.set_enabled('Seed', False)
            app = CarbonApplication(self.settings(root/'marker', free_port(),
                account_db_path=str(database.path), account_files_path=str(root/'users'),
                carbon_original_seed_account='Seed'))
            app.start()
            try:
                self.assertIsNotNone(app.original_host.seed_coordinator)
                self.assertIsNotNone(app.original_host.stream.on_roster_confirmed)
                with patch('carbon.original_host.runtime.OriginalSeedWorker') as worker:
                    app.original_host._start_seed('version', '123')
                    self.assertEqual(worker.call_args.kwargs['theater_port'], app.theater_listener.bound_endpoint.port)
                    self.assertNotEqual(worker.call_args.kwargs['theater_port'], 0)
                    worker.return_value.start.assert_called_once_with('version', '123')
                    app.stop()
                    worker.return_value.close.assert_called_once()
            finally:
                if app.original_host._thread is not None:
                    app.stop()
            self.assertIsNone(app.original_host.seed_coordinator)
            self.assertIsNone(app.original_host.stream.on_roster_confirmed)

    def test_stream_is_stopped_even_if_seed_worker_close_fails(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = SQLiteAccountDatabase(root/'accounts.sqlite3', root/'users')
            credentials = SQLiteCredentialStore(database)
            credentials.create_account('Seed', 'discarded-test-password')
            credentials.set_enabled('Seed', False)
            port = free_port()
            app = CarbonApplication(self.settings(root/'marker', port,
                account_db_path=str(database.path), account_files_path=str(root/'users'),
                carbon_original_seed_account='Seed'))
            app.start()
            try:
                with patch('carbon.original_host.runtime.OriginalSeedWorker') as worker:
                    app.original_host._start_seed('version', '123')
                    worker.return_value.close.side_effect = RuntimeError('worker failed')
                    with self.assertRaises(RuntimeError):
                        app.original_host.stop()
                self.assertIsNone(app.original_host._thread)
                with socket.socket() as check:
                    check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    check.bind(('127.0.0.1', port))
            finally:
                app.stop()


if __name__ == '__main__':
    unittest.main()
