"""Isolated socket-level checks for the opt-in Xbox PlayNow experiment."""
import socket
import threading
import unittest

from carbon.accounts.credentials import CredentialStore
from carbon.accounts.identity import IdentityStore
from carbon.core.catalog import GameId
from carbon.core.config import Endpoint, ServerSettings
from carbon.fesl.frame import FESLFrame, FESLStreamDecoder
from carbon.fesl.service import CarbonEndpoints, CarbonFESLService
from carbon.runtime.fesl import handle_fesl_connection
from common.enforcement import LiveAccountConnectionRegistry


class SlowDirectory:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def resolve_play_now(self, identity, fields):
        self.entered.set()
        if not self.release.wait(3):
            raise TimeoutError('test did not release matchmaking')
        return None


class PlayNowEarlyStartTests(unittest.TestCase):
    def exercise(self, *, enabled, xbox):
        endpoint = Endpoint('127.0.0.1', 19000)
        settings = ServerSettings(game=GameId.CARBON, fesl_listen=endpoint,
                                  fesl_public=endpoint, messenger_public=endpoint,
                                  theater_public=endpoint, race_public=endpoint,
                                  carbon_original_play_now_early_start=enabled)
        credentials = CredentialStore()
        credentials.create_account('ExamplePlayer', 'local-test-password')
        games = SlowDirectory()
        service = CarbonFESLService(CarbonEndpoints('127.0.0.1', 19001, '127.0.0.1'),
                                    IdentityStore(), games, credentials=credentials)
        client, server = socket.socketpair()
        client.settimeout(.3)
        stop = threading.Event()
        worker = threading.Thread(target=handle_fesl_connection,
                                  args=(server, ('127.0.0.1', 12345), stop),
                                  kwargs={'settings': settings, 'service': service,
                                          'live_connections': LiveAccountConnectionRegistry(name='test')},
                                  daemon=True)
        decoder = FESLStreamDecoder()
        worker.start()
        try:
            login = {'TXN': 'Login', 'name': 'ExamplePlayer', 'password': 'local-test-password'}
            if xbox:
                login['clientPlatform'] = 'xbox360'
            client.sendall(FESLFrame.from_fields('acct', login, transaction=1).encode())
            login_reply = decoder.feed(client.recv(65535))
            self.assertEqual(login_reply[0].fields.get('TXN'), 'Login')
            self.assertIn('lkey', login_reply[0].fields)
            client.sendall(FESLFrame.from_fields('pnow', {'TXN': 'Start'}, transaction=2).encode())
            self.assertTrue(games.entered.wait(1), 'matchmaking did not start')
            replies = []
            if enabled and xbox:
                replies.extend(decoder.feed(client.recv(65535)))
                self.assertEqual([r.fields['TXN'] for r in replies], ['Start'])
                self.assertEqual(replies[0].transaction, 2)
            else:
                with self.assertRaises(socket.timeout):
                    client.recv(65535)
            games.release.set()
            client.settimeout(2)
            while len(replies) < 2:
                replies.extend(decoder.feed(client.recv(65535)))
            self.assertEqual([r.fields['TXN'] for r in replies], ['Start', 'Status'])
            self.assertEqual(replies[0].fields['id.id'], replies[1].fields['id.id'])
            self.assertEqual(replies[1].fields['props.{resultType}'], 'NOSERVER')
        finally:
            games.release.set()
            stop.set()
            client.close()
            worker.join(3)
            server.close()
            self.assertFalse(worker.is_alive(), 'test connection did not close')

    def test_opt_in_sends_start_before_slow_allocation(self):
        self.exercise(enabled=True, xbox=True)

    def test_default_preserves_existing_behavior(self):
        self.exercise(enabled=False, xbox=True)

    def test_pc_behavior_is_unchanged(self):
        self.exercise(enabled=True, xbox=False)


if __name__ == '__main__':
    unittest.main()
