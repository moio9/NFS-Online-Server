import socket
import struct
import unittest
from threading import RLock
from types import SimpleNamespace
from unittest.mock import patch

from classic.app import ClassicOnlineApplication
from classic.ea.directory import SessionDirectory, SessionState
from classic.protocols.race import ClassicRaceRelay


class U2LiveRoomStatusTests(unittest.TestCase):
    def setUp(self):
        self.sessions = SessionDirectory()
        self.game = self.sessions.create_game(0, 10, capacity=2)
        self.sessions.join_game(self.game.game_id, 20)
        self.app = ClassicOnlineApplication.__new__(ClassicOnlineApplication)
        self.app.enable_u2 = True
        self.prelogin = SimpleNamespace(_connections_lock=RLock(), _connections={})
        self.app.u2 = SimpleNamespace(sessions=self.sessions, prelogin=self.prelogin)

    def visible(self):
        with patch('classic.app.time.time', return_value=1000):
            return self.app._u2_live_room_snapshot()

    def test_abandoned_active_and_finished_rooms_expire(self):
        for state in (SessionState.ACTIVE, SessionState.FINISHED):
            self.game.state = state
            self.game.started_at = 100
            self.game.finished_at = 200
            self.game.last_race_activity_at = 500
            self.assertEqual(self.visible(), [])
            self.assertIs(self.sessions.get_game(self.game.game_id), self.game)

    def test_race_with_recent_udp_survives_closed_lobby(self):
        self.game.state = SessionState.ACTIVE
        self.game.started_at = 100
        self.game.last_race_activity_at = 950
        self.assertEqual(len(self.visible()), 1)

    def test_loading_and_postrace_grace(self):
        self.game.state = SessionState.ACTIVE
        self.game.started_at = 950
        self.assertEqual(len(self.visible()), 1)
        self.game.state = SessionState.FINISHED
        self.game.started_at = 100
        self.game.finished_at = 950
        self.assertEqual(len(self.visible()), 1)

    def test_only_current_lobby_room_is_visible(self):
        self.game.created_at = 100
        self.prelogin._connections[10] = SimpleNamespace(lobby_game_id=self.game.game_id)
        self.assertEqual(len(self.visible()), 1)
        self.prelogin._connections[10].lobby_game_id = self.game.game_id + 1
        self.assertEqual(self.visible(), [])

    def test_valid_udp_refreshes_activity_but_invalid_wrapper_does_not(self):
        relay = ClassicRaceRelay(virtual_network='198.18.0.0/29')
        virtual = relay.register_u2_virtual_game(self.game)
        packet = struct.pack('!H', 3658) + socket.inet_aton(virtual[20]) + b'race-data'
        with patch('classic.protocols.race.time.time', return_value=950):
            relay.handle_channel(packet, ('10.0.0.10', 41020), 0)
        self.assertEqual(self.game.last_race_activity_at, 950)
        with patch('classic.protocols.race.time.time', return_value=999):
            relay.handle(b'bad', ('10.0.0.10', 41020))
        self.assertEqual(self.game.last_race_activity_at, 950)
        self.game.state = SessionState.ACTIVE
        self.game.started_at = 100
        self.assertEqual(len(self.visible()), 1)


if __name__ == '__main__':
    unittest.main()
