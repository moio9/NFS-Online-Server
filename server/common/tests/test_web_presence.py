import json
from pathlib import Path
import tempfile
import time
import unittest

from common.accounts import SQLiteAccountDatabase
from common.social import SocialService
from common.web_social import WebSocialEventPump, ensure_web_social_schema
from classic.protocols.control import ClassicControlContext, ClassicControlProfile, ClassicControlService


class WebPresenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = SQLiteAccountDatabase(root / 'accounts.sqlite3', root / 'users')
        self.alice = self.db.create_account('Alice', 'pw', persona='Alice')
        self.db.create_account('Bob', 'pw', persona='Bob')
        self.social = SocialService(database=self.db)
        self.social.request_friend('Alice', 'Bob')
        self.social.respond_friend('Bob', 'Alice', True)
        self.events = []
        self.social.register_lobby('bob', 'Bob', 'Bob', '127.0.0.1', game_id='mostwanted')
        self.social.register_control('bob-control', '127.0.0.1', 'Bob',
                                     lambda verb, fields: not self.events.append((verb, dict(fields))))
        ensure_web_social_schema(self.db.path)
        with self.db.transaction() as conn:
            conn.execute('CREATE TABLE web_sessions(session_hash TEXT PRIMARY KEY, account_id INTEGER, expires_at REAL)')
            for token in ('tab1', 'tab2'):
                conn.execute('INSERT INTO web_sessions VALUES(?,?,?)', (token, self.alice.account_id, time.time()+3600))
                conn.execute('INSERT INTO web_social_presence VALUES(?,?,?)', (token, 'Alice', time.time()+30))
        self.pump = WebSocialEventPump(self.db.path, self.social)
        self.pump._sync_presence()

    def test_online_reply_and_multiple_tabs_without_duplicate_delivery(self):
        row = self.social.presence_row('Bob', 'Alice')
        self.assertTrue(row.online)
        self.assertEqual(row.presence.show, 'CHAT')
        self.assertTrue(any(verb == 'PGET' and fields['SHOW'] == 'CHAT' for verb, fields in self.events))
        self.assertEqual(self.social.deliver('Alice', 'RECV', (('USER', 'Bob'), ('T', 'hello from game'))), 1)
        with self.db.transaction() as conn:
            rows = conn.execute('SELECT source_persona,target_persona,body FROM web_social_messages').fetchall()
        self.assertEqual([tuple(row) for row in rows], [('Bob', 'Alice', 'hello from game')])
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM web_sessions WHERE session_hash='tab1'")
        self.pump._sync_presence()
        self.assertTrue(self.social.presence_row('Bob', 'Alice').online)
        with self.db.transaction() as conn:
            conn.execute('DELETE FROM web_sessions')
        self.pump._sync_presence()
        self.assertFalse(self.social.presence_row('Bob', 'Alice').online)
        self.assertEqual(self.events[-1][1]['SHOW'], 'AWAY')

    def test_expiry_blocks_delivery_before_next_sync(self):
        with self.db.transaction() as conn:
            conn.execute('UPDATE web_social_presence SET expires_at=0')
        self.assertEqual(self.social.deliver('Alice', 'RECV', (('USER', 'Bob'), ('T', 'expired'))), 0)
        self.pump._sync_presence()
        self.assertFalse(self.social.presence_row('Bob', 'Alice').online)

    def test_game_presence_wins_and_survives_web_logout(self):
        self.social.register_lobby('alice-game', 'Alice', 'Alice', '127.0.0.2', game_id='carbon')
        self.social.set_presence('Alice', show='GAME', stat='Racing', product='carbon', attr='J')
        self.assertEqual(self.social.presence_row('Bob', 'Alice').presence.show, 'GAME')
        with self.db.transaction() as conn:
            conn.execute('DELETE FROM web_sessions')
        self.pump._sync_presence()
        row = self.social.presence_row('Bob', 'Alice')
        self.assertTrue(row.online)
        self.assertEqual(row.presence.show, 'GAME')
        self.assertEqual(row.presence.attr, 'J')

    def test_web_stays_online_when_game_disconnects(self):
        self.social.register_lobby('alice-game', 'Alice', 'Alice', '127.0.0.2', game_id='carbon')
        self.social.set_presence('Alice', show='GAME', attr='J')
        self.social.unregister_lobby('alice-game')
        row = self.social.presence_row('Bob', 'Alice')
        self.assertTrue(row.online)
        self.assertEqual(row.presence.show, 'CHAT')
        self.assertEqual(row.presence.attr, '')

    def test_block_and_ban_prevent_web_delivery(self):
        self.social.set_blocked('Alice', 'Bob', True)
        self.assertEqual(self.social.deliver('Alice', 'RECV', (('USER', 'Bob'), ('T', 'blocked'))), 0)
        self.social.set_blocked('Alice', 'Bob', False)
        with self.db.transaction() as conn:
            conn.execute('UPDATE accounts SET banned=1 WHERE account_id=?', (self.alice.account_id,))
        self.assertEqual(self.social.deliver('Alice', 'RECV', (('USER', 'Bob'), ('T', 'banned'))), 0)
        self.pump._sync_presence()
        self.assertFalse(self.social.presence_row('Bob', 'Alice').online)

    def test_web_send_announces_online_before_message(self):
        result = self.pump._process({'source_persona': 'Alice', 'target_persona': 'Bob',
                                     'action': 'message', 'payload_json': json.dumps({'body': 'hello'})})
        self.assertTrue(result['accepted'])
        recv_index = next(i for i, event in enumerate(self.events) if event[0] == 'RECV')
        self.assertTrue(any(verb == 'PGET' and fields['SHOW'] == 'CHAT' for verb, fields in self.events[:recv_index]))

    def test_native_send_and_private_message_reach_inbox(self):
        service = ClassicControlService(self.social, profile=ClassicControlProfile(game_id="mostwanted"))
        context = ClassicControlContext('bob', '127.0.0.1', identity=self.social.resolve_lobby('127.0.0.1', 'Bob'), authenticated=True)
        reply = service._send({'USER': 'Alice', 'BODY': '"native reply"'}, context)
        self.assertIn(b'DELIVERED=1', reply.frames[0])
        service._message('PMSG', {'USER': 'Alice', 'TEXT': 'private reply'}, context)
        with self.db.transaction() as conn:
            rows = conn.execute('SELECT body FROM web_social_messages ORDER BY message_id').fetchall()
        self.assertEqual([row[0] for row in rows], ['native reply', 'private reply'])


if __name__ == '__main__':
    unittest.main()
