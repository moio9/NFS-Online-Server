"""Retail FESL association replies backed by shared, persistent social history."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from common.accounts import SQLiteAccountDatabase
from common.social import SocialService
from carbon.app import CarbonApplication
from carbon.accounts.identity import IdentityStore
from carbon.fesl.frame import FESLFrame
from carbon.fesl.service import CarbonEndpoints, CarbonFESLService, FESLConnection


class CarbonAssociationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
        for name in ("Alice", "Bob", "Stranger"):
            self.database.create_account(name.lower(), "pw", persona=name)
        self.social = SocialService(database=self.database)
        for name in ("Alice", "Bob"):
            self.social.set_game_session(name, name, "carbon", "room")
        self.social.clear_game_session("Bob")
        app = object.__new__(CarbonApplication)
        app.account_database = self.database
        self.service = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
            IdentityStore(), association_members=app._association_members,
        )
        self.connection = FESLConnection()
        self.connection.identity = self.database.identity_for_persona("Alice")

    def query(self, kind="PlasmaRecentPlayers", transaction=1):
        request = FESLFrame.from_fields("asso", {
            "TXN": "GetAssociations", "type": kind,
            "owner.id": "999999", "owner.name": "Stranger",
            "domainPartition.domain": "eagames",
            "domainPartition.subDomain": "NFS-2007",
        }, transaction=transaction)
        replies = self.service.dispatch(request, self.connection)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].command, "asso")
        return replies[0].fields

    def test_recent_reply_has_retail_member_fields_and_authenticated_owner(self):
        fields = self.query()
        bob = self.database.identity_for_persona("Bob")
        self.assertEqual(fields["owner.id"], str(self.connection.identity.profile_id))
        self.assertEqual(fields["owner.name"], "Alice")
        self.assertEqual(fields["type"], "PlasmaRecentPlayers")
        self.assertEqual(fields["domainPartition.subDomain"], "NFS-2007")
        self.assertEqual(fields["maxListSize"], "100")
        self.assertEqual(fields["members.[]"], "1")
        self.assertEqual(fields["members.0.id"], str(bob.profile_id))
        self.assertEqual(fields["members.0.name"], "Bob")
        self.assertEqual(fields["members.0.type"], "1")
        self.assertEqual(fields["members.0.xuid"], "0")

    def test_recent_includes_friends_but_refreshes_cross_process_blocks(self):
        self.social.request_friend("Alice", "Bob")
        self.social.respond_friend("Bob", "Alice", True)
        self.assertEqual(self.social.recent_player_snapshot("Alice", "carbon"), ())
        self.assertEqual(self.query()["members.0.name"], "Bob")
        self.assertEqual(self.query("PlasmaFriends", 2)["members.0.name"], "Bob")
        other = SocialService(database=self.database)
        other.set_blocked("Bob", "Alice", True)
        self.assertEqual(self.query(transaction=3)["members.[]"], "0")

    def test_unauthenticated_request_cannot_read_history(self):
        self.connection.identity = None
        self.service.association_members = lambda *_: self.fail("unauthenticated history read")
        fields = self.query()
        self.assertEqual(fields["members.[]"], "0")
        self.assertEqual(fields["owner.id"], "0")

    def test_unknown_list_is_empty_and_has_metadata(self):
        fields = self.query("UnknownList")
        self.assertEqual(fields["members.[]"], "0")
        self.assertEqual(fields["owner.type"], "1")


if __name__ == "__main__":
    unittest.main()
