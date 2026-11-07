from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from carbon.accounts.identity import ClientPlatform, IdentityStore
from carbon.accounts.credentials import CredentialStore
from carbon.accounts.sqlite_backend import SQLiteIdentityStore
from carbon.core.config import Endpoint
from carbon.fesl.frame import FESLFrame
from carbon.fesl.service import CarbonEndpoints, CarbonFESLService, FESLConnection
from carbon.rebroadcaster.service import CarbonRebroadcasterService
from carbon.theater.directory import CarbonGameDirectory
from carbon.theater.service import CarbonTheaterService, TheaterConnection
from common.accounts import SQLiteAccountDatabase


class SessionPlatformTests(unittest.TestCase):
    def test_memory_sessions_keep_platform_separate_and_revoke_it(self):
        tokens = iter(("xbox-token", "pc-token"))
        store = IdentityStore(token_factory=lambda: next(tokens))
        xbox, xbox_token = store.login("Driver", client_platform=ClientPlatform.XBOX360)
        pc, pc_token = store.login("Driver")
        self.assertEqual(xbox.user_id, pc.user_id)
        self.assertIs(store.resolve_session(xbox_token).client_platform, ClientPlatform.XBOX360)
        self.assertIs(store.resolve_session(pc_token).client_platform, ClientPlatform.UNKNOWN)
        self.assertTrue(store.revoke_session(xbox_token))
        self.assertIsNone(store.resolve_session(xbox_token))
        self.assertIs(store.resolve_session(pc_token).client_platform, ClientPlatform.UNKNOWN)
        with self.assertRaises(ValueError):
            store.login("Driver", client_platform="unrecognized")

    def test_sqlite_sessions_keep_platform_separate(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account("Driver", "secret", persona="Racer")
            tokens = iter(("xbox-token", "pc-token"))
            store = SQLiteIdentityStore(database, token_factory=lambda: next(tokens))
            xbox, xbox_token = store.login("Driver", "Racer", client_platform=ClientPlatform.XBOX360)
            pc, pc_token = store.login("Driver", "Racer")
            self.assertEqual(xbox.user_id, pc.user_id)
            self.assertIs(store.resolve_session(xbox_token).client_platform, ClientPlatform.XBOX360)
            self.assertIs(store.resolve_session(pc_token).client_platform, ClientPlatform.UNKNOWN)
            self.assertTrue(store.revoke_session(xbox_token))
            self.assertIsNone(store.resolve_session(xbox_token))

    def test_authenticated_theater_session_carries_platform_to_rebroadcaster(self):
        tokens = iter(("xbox-token", "pc-token"))
        store = IdentityStore(token_factory=lambda: next(tokens))
        xbox, xbox_token = store.login("Driver", client_platform=ClientPlatform.XBOX360)
        pc, _ = store.login("Driver")
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        theater = CarbonTheaterService(store, games)
        connection = TheaterConnection(peer_ip="127.0.0.1", peer_port=1234)
        theater.dispatch(FESLFrame.from_fields("USER", {"TID": "1", "LKEY": xbox_token}), connection)
        self.assertIs(connection.identity.client_platform, ClientPlatform.XBOX360)
        game = games.create(connection.identity)
        participant = games.enter(game.gid, connection.identity)
        assert participant is not None
        resolution = games.resolve_ticket(games.ticket(game, participant))
        assert resolution is not None
        rebroadcaster = CarbonRebroadcasterService(games)
        self.assertTrue(rebroadcaster.session_bootstrap.use_account_profile_id(resolution))
        self.assertTrue(rebroadcaster.session_bootstrap.wait_for_self_join_ack(resolution))
        refreshed = games.enter(game.gid, pc)
        assert refreshed is not None
        self.assertEqual(refreshed.descriptor_handle, participant.descriptor_handle)
        pc_resolution = games.resolve_ticket(games.ticket(game, refreshed))
        assert pc_resolution is not None
        self.assertIs(pc_resolution.participant.identity.client_platform, ClientPlatform.UNKNOWN)
        self.assertFalse(rebroadcaster.session_bootstrap.use_account_profile_id(pc_resolution))
        self.assertFalse(rebroadcaster.session_bootstrap.wait_for_self_join_ack(pc_resolution))

    def test_client_claimed_platform_and_xuid_do_not_authenticate_xbox(self):
        store = IdentityStore(token_factory=lambda: "token")
        service = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215), store,
            authentication_mode="open",
        )
        connection = FESLConnection()
        service.dispatch(FESLFrame.from_fields("acct", {
            "TXN": "RegisterGame", "platform": "xbox360", "game": "Carbon",
        }), connection)
        service.dispatch(FESLFrame.from_fields("acct", {
            "TXN": "Login", "name": "Driver", "platform": "xbox360", "clientPlatform": "xbox360", "xuid": "123",
        }), connection)
        self.assertIs(connection.identity.client_platform, ClientPlatform.UNKNOWN)
        other = FESLConnection()
        service.dispatch(FESLFrame.from_fields("acct", {
            "TXN": "XBL360Login", "xuid": "123", "gamertag": "Driver",
        }), other)
        self.assertIsNone(other.identity)
        self.assertEqual(other.session_key, "")

    def test_password_authenticated_platform_is_session_preference_only(self):
        credentials = CredentialStore(salt_factory=lambda n: b"\x01" * n)
        credentials.create_account("Driver", "synthetic-password", persona="Racer")
        tokens = iter(("xbox-session", "pc-session"))
        store = IdentityStore(token_factory=lambda: next(tokens))
        service = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215), store,
            credentials=credentials, authentication_mode="password",
        )
        rejected = FESLConnection()
        service.dispatch(FESLFrame.from_fields("acct", {
            "TXN": "Login", "name": "Driver", "password": "incorrect", "clientPlatform": "xbox360",
        }), rejected)
        self.assertIsNone(rejected.identity)
        self.assertEqual(rejected.session_key, "")
        xbox = FESLConnection()
        xbox_reply = service.dispatch(FESLFrame.from_fields("acct", {
            "TXN": "Login", "name": "Driver", "password": "synthetic-password", "clientPlatform": "xbox360",
        }), xbox)[0]
        self.assertIs(xbox.identity.client_platform, ClientPlatform.XBOX360)
        self.assertEqual(xbox_reply.fields.get("clientPlatform"), "xbox360")
        pc = FESLConnection()
        pc_reply = service.dispatch(FESLFrame.from_fields("acct", {
            "TXN": "Login", "name": "Driver", "password": "synthetic-password",
        }), pc)[0]
        self.assertIs(pc.identity.client_platform, ClientPlatform.UNKNOWN)
        self.assertNotIn("clientPlatform", pc_reply.fields)
        self.assertIs(store.resolve_session(xbox.session_key).client_platform, ClientPlatform.XBOX360)

    def test_displaced_session_can_browse_but_cannot_change_rooms(self):
        tokens = iter(("old-token", "new-token"))
        store = IdentityStore(token_factory=lambda: next(tokens))
        old, old_token = store.login("Driver", client_platform=ClientPlatform.XBOX360)
        active, _ = store.login("Driver", forced_logoff_reason="DUPL")
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        game = games.create(active)
        theater = CarbonTheaterService(store, games)
        connection = TheaterConnection(peer_ip="127.0.0.1", peer_port=1234)
        theater.dispatch(FESLFrame.from_fields("USER", {"TID": "1", "LKEY": old_token}), connection)
        self.assertEqual(connection.forced_logoff_reason, "DUPL")
        for command, fields in (("CGAM", {}), ("EGAM", {"GID": game.gid}),
                                ("ECNL", {"GID": game.gid})):
            response = theater.dispatch(FESLFrame.from_fields(command, {"TID": "2", **fields}), connection)
            self.assertEqual(response[0].fields.get("ERR"), "INVALID_SESSION")
        self.assertIn(active.user_id, game.participants)
        self.assertEqual(len(games.list()), 1)
        self.assertIs(old.client_platform, ClientPlatform.XBOX360)

    def test_existing_theater_connection_loses_write_access_on_takeover(self):
        tokens = iter(("old-token", "new-token"))
        store = IdentityStore(token_factory=lambda: next(tokens))
        old, token = store.login("Driver", client_platform=ClientPlatform.XBOX360)
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        theater = CarbonTheaterService(store, games)
        connection = TheaterConnection(peer_ip="127.0.0.1", peer_port=1234)
        theater.dispatch(FESLFrame.from_fields("USER", {"TID": "1", "LKEY": token}), connection)
        active, _ = store.login("Driver", forced_logoff_reason="DUPL")
        game = games.create(active)
        store._forced_logoffs.clear()  # Existing connection retains the displaced lease.
        for command, fields in (("CGAM", {}), ("EGAM", {"GID": game.gid}),
                                ("ECNL", {"GID": game.gid})):
            response = theater.dispatch(FESLFrame.from_fields(command, {"TID": "2", **fields}), connection)
            self.assertEqual(response[0].fields.get("ERR"), "INVALID_SESSION")
        response = theater.dispatch(FESLFrame.from_fields("ECNL", {"TID": "3", "GID": game.gid}), connection)
        self.assertEqual(response[0].fields.get("ERR"), "INVALID_SESSION")
        self.assertIn(old.user_id, game.participants)
        self.assertEqual(len(games.list()), 1)

    def test_sqlite_existing_connection_retains_displacement_after_marker_eviction(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account("Driver", "secret", persona="Racer")
            tokens = iter(("old-token", "new-token"))
            store = SQLiteIdentityStore(database, token_factory=lambda: next(tokens))
            old, token = store.login("Driver", "Racer", client_platform=ClientPlatform.XBOX360)
            games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
            theater = CarbonTheaterService(store, games)
            connection = TheaterConnection(peer_ip="127.0.0.1", peer_port=1234)
            theater.dispatch(FESLFrame.from_fields("USER", {"TID": "1", "LKEY": token}), connection)
            active, _ = store.login("Driver", "Racer", forced_logoff_reason="DUPL")
            game = games.create(active)
            store._forced_logoffs.clear()
            reply = theater.dispatch(FESLFrame.from_fields("ECNL", {"TID": "2", "GID": game.gid}), connection)
            self.assertEqual(reply[0].fields.get("ERR"), "INVALID_SESSION")
            self.assertIn(old.user_id, game.participants)


if __name__ == "__main__":
    unittest.main()
