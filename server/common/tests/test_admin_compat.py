"""Compatibility for relocated shared account administration."""
import importlib
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class SharedAccountAdministrationTests(unittest.TestCase):
    def test_old_import_shares_the_canonical_module(self):
        self.assertIs(importlib.import_module('classic.admin.auth'),
                      importlib.import_module('common.admin.auth'))

    def test_sqlite_admin_does_not_import_game_packages(self):
        with TemporaryDirectory() as temp:
            config = Path(temp) / 'server.toml'
            config.write_text('[server]\nPUBLIC_HOST="127.0.0.1"\n'
                              'LOCAL_ADVERTISE_HOST="127.0.0.1"\n'
                              'IPC_SECRET="test-only-test-only-test-only-test-only"\n')
            env = os.environ.copy()
            env['NFS_IPC_SECRET'] = 'test-only-test-only-test-only-test-only'
            env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
            code = ("import importlib.abc, runpy, sys; "
                    "Block=type('Block',(importlib.abc.MetaPathFinder,),"
                    "{'find_spec':lambda self,name,*a: "
                    "(_ for _ in ()).throw(ImportError(name)) "
                    "if name in ('classic','carbon') or "
                    "name.startswith(('classic.','carbon.')) else None}); "
                    "sys.meta_path.insert(0,Block()); "
                    "sys.argv=['common.admin.auth','--config',sys.argv[1],'list']; "
                    "runpy.run_module('common.admin.auth',run_name='__main__')")
            subprocess.run([sys.executable, '-c', code, str(config)], env=env,
                           capture_output=True, text=True, check=True)

    def test_legacy_json_account_store_still_works(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'legacy.cfg'
            config.write_text('AUTH_DATA=' + str(root / 'auth.json') + '\n')
            env = os.environ.copy()
            env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
            def admin(*arguments):
                return subprocess.run([sys.executable, '-m', 'common.admin.auth',
                                       '--config', str(config), *arguments],
                                      env=env, capture_output=True, text=True, check=True).stdout
            admin('create', 'LegacyUser', '--password', 'test-only-password')
            self.assertIn('account=LegacyUser persona=LegacyUser', admin('list'))

    def test_both_account_entry_points_use_the_same_database(self):
        with TemporaryDirectory() as temp:
            config = Path(temp) / 'server.toml'
            config.write_text('[server]\nPUBLIC_HOST="127.0.0.1"\n'
                              'LOCAL_ADVERTISE_HOST="127.0.0.1"\n'
                              'IPC_SECRET="test-only-test-only-test-only-test-only"\n')
            env = os.environ.copy()
            env['NFS_IPC_SECRET'] = 'test-only-test-only-test-only-test-only'
            server = Path(__file__).resolve().parents[2]
            env['PYTHONPATH'] = os.pathsep.join((str(server), env.get('PYTHONPATH', '')))
            def admin(module, *arguments):
                return subprocess.run([sys.executable, '-m', module, '--config', str(config), *arguments],
                                      env=env, capture_output=True, text=True, check=True).stdout
            admin('common.admin.auth', 'create', 'ExampleUser', '--password', 'test-only-password',
                  '--email', 'driver@example.invalid', '--alias', 'DriverAlias',
                  '--extra-persona', 'SecondPersona')
            self.assertIn('account=ExampleUser persona=ExampleUser enabled=1',
                          admin('classic.admin.auth', 'list'))
            admin('classic.admin.auth', 'enabled', 'ExampleUser', 'off')
            self.assertIn('account=ExampleUser persona=ExampleUser enabled=0',
                          admin('common.admin.auth', 'list'))
            admin('common.admin.auth', 'enabled', 'ExampleUser', 'on')
            admin('common.admin.auth', 'ban', 'ExampleUser')
            self.assertIn('banned=1', admin('classic.admin.auth', 'list'))
            admin('classic.admin.auth', 'unban', 'ExampleUser')
            admin('common.admin.auth', 'password', 'ExampleUser', '--password', 'new-test-password')
            from common.accounts import SQLiteAccountDatabase
            database = SQLiteAccountDatabase(Path(temp) / 'data/accounts.sqlite3', Path(temp) / 'data/users')
            self.assertTrue(database.authenticate('DriverAlias', 'new-test-password').accepted)
            self.assertIn('SecondPersona', database.resolve_account('ExampleUser').personas)
            admin('common.admin.auth', 'ban-email', 'driver@example.invalid')
            self.assertIn('blocked_email=driver@example.invalid', admin('classic.admin.auth', 'list'))
            admin('classic.admin.auth', 'unban-email', 'driver@example.invalid')
            self.assertFalse(database.is_email_blocked('driver@example.invalid'))
