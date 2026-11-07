"""Real sockets and signed IPC for the optional Carbon-only Messenger."""
import json
import os
import subprocess
import sys
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import time
import unittest

from carbon.app import CarbonApplication
from carbon.core.catalog import GameId
from carbon.core.config import Endpoint, ServerSettings
from common.ipc import authenticated_envelope
from common.messenger import EAMessengerFrame, EAMessengerStreamDecoder
from common.messenger.standalone import StandaloneMessenger, StandaloneSettings


class Peer:
    def __init__(self, endpoint):
        self.socket = socket.create_connection((endpoint.host, endpoint.port), timeout=2)
        self.socket.settimeout(2)
        self.decoder = EAMessengerStreamDecoder()
        self.pending = []

    def send(self, command, fields):
        self.socket.sendall(EAMessengerFrame.from_fields(command, fields, transaction=0).encode())

    def receive(self, command):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            for index, frame in enumerate(self.pending):
                if frame.command == command:
                    return self.pending.pop(index)
            chunk = self.socket.recv(8192)
            if not chunk:
                raise AssertionError('Messenger disconnected before ' + command)
            self.pending.extend(self.decoder.feed(chunk))
        raise AssertionError('Missing Messenger response ' + command)

    def close(self):
        self.socket.close()


class StandaloneMessengerTests(unittest.TestCase):
    def test_startup_failure_releases_the_ipc_port(self):
        local = Endpoint('127.0.0.1', 0)
        with socket.socket() as blocked, socket.socket() as reserved:
            blocked.bind(('127.0.0.1', 0))
            blocked.listen()
            reserved.bind(('127.0.0.1', 0))
            ipc_port = reserved.getsockname()[1]
            reserved.close()
            app = StandaloneMessenger(StandaloneSettings(
                messenger_listen=local, messenger_public=local,
                web_listen=Endpoint('127.0.0.1', blocked.getsockname()[1]),
                ipc_listen=Endpoint('127.0.0.1', ipc_port), ipc_secret='test-only-secret'))
            with self.assertRaises(OSError):
                app.start()
            with socket.socket() as check:
                check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                check.bind(('127.0.0.1', ipc_port))
            app.stop()

    def test_standalone_module_runs_as_a_separate_process(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            sockets = []
            for _ in range(3):
                sock = socket.socket()
                sock.bind(('127.0.0.1', 0))
                sockets.append(sock)
            messenger_port, web_port, ipc_port = (sock.getsockname()[1] for sock in sockets)
            for sock in sockets:
                sock.close()
            config = root / 'config' / 'server.toml'
            config.parent.mkdir()
            secret = 'test-only-messenger-secret-for-ipc'
            config.write_text('[server]\nPUBLIC_HOST="127.0.0.1"\n'
                              'LOCAL_ADVERTISE_HOST="127.0.0.1"\n'
                              f'IPC_SECRET="{secret}"\n'
                              f'MESSENGER_LISTEN="127.0.0.1:{messenger_port}"\n'
                              f'WEB_LISTEN="127.0.0.1:{web_port}"\n'
                              f'MESSENGER_IPC_LISTEN="127.0.0.1:{ipc_port}"\n')
            env = os.environ.copy()
            env['NFS_IPC_SECRET'] = secret
            env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
            process = subprocess.Popen([sys.executable, '-m', 'common.messenger.standalone',
                                        '--config', str(config)], cwd=root, env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            peer = None
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail('standalone Messenger exited during startup')
                    try:
                        with socket.create_connection(('127.0.0.1', ipc_port), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    self.fail('standalone Messenger IPC did not start')
                identity = {'account_name': 'Driver', 'persona': 'Driver',
                            'profile_id': 101, 'user_id': 101, 'wire_player_id': 1101}
                payload = {'game': 'carbon', 'kind': 'snapshot', 'instance_id': 'client-test',
                           'sessions': {'driver-key': identity}, 'known_identities': [identity], 'rooms': {}}
                envelope = authenticated_envelope(secret=secret, instance_id='process-test',
                                                   sequence=1, sent_at=time.time(), payload=payload)
                with socket.create_connection(('127.0.0.1', ipc_port), timeout=2) as ipc:
                    ipc.sendall(json.dumps(envelope).encode() + b'\n')
                deadline = time.monotonic() + 5
                while True:
                    try:
                        peer = Peer(Endpoint('127.0.0.1', messenger_port))
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)
                peer.send('AUTH', {'LKEY': 'driver-key', 'RSRC': '/EAGAMES/NFS-2007', 'ID': '1'})
                self.assertNotIn('ERR', peer.receive('AUTH').fields)
            finally:
                if peer is not None:
                    peer.close()
                if process.poll() is None:
                    process.terminate()
                try:
                    _, errors = process.communicate(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _, errors = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 0, errors.decode(errors='replace'))

    def test_carbon_application_publisher_reaches_standalone_messenger(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            local = Endpoint('127.0.0.1', 0)
            secret = 'test-only-messenger-secret'
            db, users = str(root / 'accounts.sqlite3'), str(root / 'users')
            catalog = Path(__file__).resolve().parents[3] / 'data/carbon/dlc_catalog.json'
            assignments = root / 'assignments.json'
            assignments.write_text('{"version": 1, "default": [], "accounts": {}, "personas": {}}')
            messenger = StandaloneMessenger(StandaloneSettings(
                messenger_listen=local, messenger_public=local, web_listen=local,
                ipc_listen=local, ipc_secret=secret, account_db=db, account_files=users,
                max_age=30, auth_wait=0.25))
            messenger.start()
            app = None
            peer = None
            try:
                settings = ServerSettings(
                    game=GameId.CARBON, fesl_listen=local, fesl_public=local,
                    messenger_public=messenger.messenger_listener.bound_endpoint,
                    theater_public=local, theater_listen=local, race_public=local, race_listen=local,
                    messenger_ipc_endpoint=messenger.receiver.bound_endpoint,
                    messenger_ipc_secret=secret, account_db_path=db, account_files_path=users,
                    auth_mode='password', account_data_path=None,
                    carbon_dlc_catalog_path=str(catalog), carbon_dlc_assignments_path=str(assignments))
                app = CarbonApplication(settings)
                app.runtime_status.path = root / 'status.json'
                app.credentials.create_account('Driver', 'test-only-password')
                app.start()
                _, token = app.identities.login('Driver', 'Driver')
                deadline = time.monotonic() + 5
                while messenger.state.resolve_session(token) is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertIsNotNone(messenger.state.resolve_session(token))
                peer = Peer(messenger.messenger_listener.bound_endpoint)
                peer.send('AUTH', {'LKEY': token, 'RSRC': '/EAGAMES/NFS-2007', 'ID': '1'})
                self.assertNotIn('ERR', peer.receive('AUTH').fields)
            finally:
                if peer is not None: peer.close()
                if app is not None: app.stop()
                messenger.stop()

    def test_signed_ipc_authentication_messages_invites_and_policy(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            local = Endpoint('127.0.0.1', 0)
            secret = 'test-only-messenger-secret'
            app = StandaloneMessenger(StandaloneSettings(
                messenger_listen=local, messenger_public=local, web_listen=local,
                ipc_listen=local, ipc_secret=secret, account_db=str(root / 'accounts.sqlite3'),
                account_files=str(root / 'users'), max_age=30, auth_wait=0.25))
            app.database.create_account('Alice', 'test-only-password')
            app.database.create_account('Bob', 'test-only-password')
            identities = [app.database.identity_for_persona(name, require_carbon_wire_id=True)
                          for name in ('Alice', 'Bob')]
            def record(identity):
                return {'account_name': identity.account_name, 'persona': identity.persona,
                        'profile_id': identity.profile_id, 'user_id': identity.user_id,
                        'wire_player_id': identity.carbon_wire_player_id}
            payload = {'game': 'carbon', 'kind': 'snapshot', 'instance_id': 'test',
                       'sessions': {'alice-key': record(identities[0]), 'bob-key': record(identities[1])},
                       'known_identities': [record(identity) for identity in identities],
                       'rooms': {'alice': {'persona': 'Alice', 'session_id': 'room-1',
                                           'inviteable': True, 'details': {'game_type': '1'}}}}
            app.start()
            alice = bob = None
            try:
                envelope = authenticated_envelope(secret=secret, instance_id='test-publisher',
                                                   sequence=1, sent_at=time.time(), payload=payload)
                target = app.receiver.bound_endpoint
                with socket.create_connection((target.host, target.port), timeout=2) as ipc:
                    ipc.sendall(json.dumps(envelope).encode() + b'\n')
                deadline = time.monotonic() + 3
                while app.state.resolve_session('alice-key') is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertIsNotNone(app.state.resolve_session('alice-key'))
                web = app.web_listener.bound_endpoint
                with socket.create_connection((web.host, web.port), timeout=2) as request:
                    request.sendall(b'GET /tos HTTP/1.1\r\nHost: localhost\r\n\r\n')
                    self.assertIn(b'200 OK', request.recv(4096))
                alice = Peer(app.messenger_listener.bound_endpoint)
                bob = Peer(app.messenger_listener.bound_endpoint)
                for peer, token in ((alice, 'alice-key'), (bob, 'bob-key')):
                    peer.send('AUTH', {'LKEY': token, 'RSRC': '/EAGAMES/NFS-2007', 'ID': '1'})
                    self.assertNotIn('ERR', peer.receive('AUTH').fields)
                    peer.send('PSET', {'SHOW': 'CHAT', 'ID': '2'})
                    self.assertNotIn('ERR', peer.receive('PSET').fields)
                alice.send('SEND', {'ID': '3', 'USER': 'Bob', 'BODY': 'hello', 'TYPE': 'C', 'SECS': '0'})
                self.assertNotIn('ERR', alice.receive('SEND').fields)
                self.assertEqual(bob.receive('RECV').fields['BODY'], 'hello')
                alice.send('GINV', {'ID': '4', 'USER': 'Bob', 'SESS': '0'})
                self.assertNotIn('ERR', alice.receive('GINV').fields)
                self.assertEqual(bob.receive('GNOT').fields['HOST'], 'Alice')
                app.database.set_banned('Alice', True)
                deadline = time.monotonic() + 4
                while len(app.connections) > 1 and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(len(app.connections), 1)
            finally:
                if alice is not None: alice.close()
                if bob is not None: bob.close()
                app.stop()
