import subprocess
import sys
import unittest

from carbon.accounts.credentials import CredentialStore
from carbon.accounts.identity import ClientPlatform, IdentityStore
from carbon.fesl.frame import FESLFrame
from carbon.fesl.service import CarbonEndpoints, CarbonFESLService, FESLConnection
from carbon.xbox_gateway import GatewayError, XboxPasswordBridge


def request(operation, transaction=0x80000001, **fields):
    return FESLFrame.from_fields("acct", {"TXN": operation, **fields}, transaction=transaction)


class XboxPasswordBridgeTests(unittest.TestCase):
    def setUp(self):
        credentials = CredentialStore(salt_factory=lambda n: b"\x01" * n)
        credentials.create_account("Driver", "fake-password", persona="Racer")
        self.identities = IdentityStore(token_factory=lambda: "fake-session")
        self.server = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
            self.identities, credentials=credentials, authentication_mode="password",
        )
        self.connection = FESLConnection()

    def _authenticate(self, bridge):
        lookup = request("GetUserIdsByXuids", **{"xuids.[]": "1", "xuids.0": "fake_xuid"})
        action, = bridge.client_frame(lookup)
        self.assertEqual(action.destination, "upstream")
        self.assertEqual(action.frame.fields["TXN"], "Login")
        self.assertNotIn("fake-password", repr(action))
        reply, = self.server.dispatch(action.frame, self.connection)
        action, = bridge.upstream_frame(reply)
        self.assertEqual(action.destination, "client")
        self.assertEqual(action.frame.fields["userIds.{fake_xuid}.userId"], str(self.connection.identity.user_id))
        self.assertIs(self.connection.identity.client_platform, ClientPlatform.XBOX360)
        return reply

    def test_password_login_maps_only_current_xuid_and_keeps_server_token(self):
        bridge = XboxPasswordBridge("Driver", "fake-password")
        reply = self._authenticate(bridge)
        login = request("XBL360Login", transaction=0x80000002, xuid="fake_xuid", gamertag="Any")
        action, = bridge.client_frame(login)
        self.assertEqual(action.destination, "client")
        self.assertEqual(action.frame.transaction, login.transaction)
        self.assertEqual(action.frame.fields["TXN"], "XBL360Login")
        self.assertEqual(action.frame.fields["lkey"], reply.fields["lkey"])
        self.assertNotIn("clientPlatform", action.frame.fields)
        empty, = bridge.client_frame(request("GetUserIdsByXuids", **{"xuids.[]": "1", "xuids.0": "another"}))
        self.assertEqual(empty.frame.fields["userIds.[]"], "0")
        update, = bridge.client_frame(request("XBL360UpdateAccount", xuid="fake_xuid", email="ignored"))
        self.assertEqual(update.frame.fields, {"TXN": "XBL360UpdateAccount"})
        with self.assertRaises(GatewayError):
            bridge.client_frame(login)

    def test_wrong_password_never_returns_a_fake_mapping_or_session(self):
        bridge = XboxPasswordBridge("Driver", "wrong-password")
        action, = bridge.client_frame(request("GetUserIdsByXuids", **{"xuids.[]": "1", "xuids.0": "fake_xuid"}))
        reply, = self.server.dispatch(action.frame, self.connection)
        with self.assertRaises(GatewayError):
            bridge.upstream_frame(reply)
        self.assertIsNone(self.connection.identity)
        with self.assertRaises(GatewayError):
            bridge.client_frame(request("XBL360Login", xuid="fake_xuid"))

    def test_unverified_open_server_or_wrong_xuid_cannot_issue_xbox_login(self):
        bridge = XboxPasswordBridge("Driver", "fake-password")
        action, = bridge.client_frame(request("GetUserIdsByXuids", **{"xuids.[]": "1", "xuids.0": "fake_xuid"}))
        open_reply = FESLFrame.from_fields("acct", {
            "TXN": "Login", "lkey": "fake", "userId": "123", "displayName": "Racer",
        }, transaction=action.frame.transaction)
        with self.assertRaises(GatewayError):
            bridge.upstream_frame(open_reply)
        second = XboxPasswordBridge("Driver", "fake-password")
        self._authenticate(second)
        with self.assertRaises(GatewayError):
            second.client_frame(request("XBL360Login", xuid="different_xuid"))

    def test_lookup_uses_server_user_id_without_deriving_it_from_persona(self):
        bridge = XboxPasswordBridge("Driver", "fake-password")
        outbound, = bridge.client_frame(request("GetUserIdsByXuids", **{"xuids.[]": "1", "xuids.0": "fake_xuid"}))
        response = FESLFrame.from_fields("acct", {
            "TXN": "Login", "lkey": "fake-session", "userId": "305419896",
            "profileId": "42", "displayName": "DifferentPersona", "clientPlatform": "xbox360",
        }, transaction=outbound.frame.transaction)
        lookup, = bridge.upstream_frame(response)
        self.assertEqual(lookup.frame.fields["userIds.{fake_xuid}.userId"], "305419896")

    def test_credentials_cannot_inject_fesl_fields(self):
        for account, password in (("Driver\nTXN=bad", "p"), ("Driver", "p\nclientPlatform=pc")):
            with self.assertRaises(GatewayError):
                XboxPasswordBridge(account, password)

    def test_cli_rejects_noninteractive_password_input(self):
        result = subprocess.run(
            [sys.executable, "-m", "carbon.xbox_gateway"],
            input="fake-account\nfake-password\n", text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("fake-password", result.stdout + result.stderr)
        self.assertNotIn("Password:", result.stdout + result.stderr)

    def test_local_services_are_advertised_only_when_configured(self):
        hello = FESLFrame.from_fields("fsys", {
            "TXN": "Hello", "theaterIp": "example.com", "theaterPort": "18215",
            "messengerIp": "example.com", "messengerPort": "13505",
        }, transaction=8)
        normal, = XboxPasswordBridge("Driver", "fake-password").upstream_frame(hello)
        self.assertEqual(normal.frame.fields["theaterIp"], "example.com")
        bridge = XboxPasswordBridge("Driver", "fake-password", local_services=(28235, 23535))
        translated, = bridge.upstream_frame(hello)
        self.assertEqual(translated.frame.fields["theaterIp"], "127.0.0.1")
        self.assertEqual(translated.frame.fields["theaterPort"], "28235")
        self.assertEqual(translated.frame.fields["messengerIp"], "127.0.0.1")
        self.assertEqual(translated.frame.fields["messengerPort"], "23535")
        self.assertEqual(translated.frame.transaction, 8)
        with self.assertRaises(GatewayError):
            XboxPasswordBridge("Driver", "fake-password", local_services=(23, 23535))

    def test_race_transport_follows_authenticated_session_lifetime(self):
        started, stopped = [], []
        bridge = XboxPasswordBridge(
            "Driver", "fake-password",
            on_session=lambda token: (started.append(token), lambda: stopped.append(True))[1],
        )
        self.assertFalse(started)
        reply = self._authenticate(bridge)
        self.assertEqual(started, [reply.fields["lkey"]])
        bridge.close()
        bridge.close()
        self.assertEqual(stopped, [True])
        wrong = XboxPasswordBridge("Driver", "wrong-password", on_session=started.append)
        action, = wrong.client_frame(request("GetUserIdsByXuids", **{"xuids.[]": "1", "xuids.0": "fake_xuid"}))
        response, = self.server.dispatch(action.frame, FESLConnection())
        with self.assertRaises(GatewayError):
            wrong.upstream_frame(response)
        self.assertEqual(len(started), 1)

    def test_transport_failure_does_not_issue_xbox_login(self):
        def fail_transport(_token):
            raise TimeoutError("unavailable")

        bridge = XboxPasswordBridge("Driver", "fake-password", on_session=fail_transport)
        outbound, = bridge.client_frame(request("GetUserIdsByXuids", **{"xuids.[]": "1", "xuids.0": "fake_xuid"}))
        response, = self.server.dispatch(outbound.frame, self.connection)
        with self.assertRaises(GatewayError):
            bridge.upstream_frame(response)
        with self.assertRaises(GatewayError):
            bridge.client_frame(request("XBL360Login", xuid="fake_xuid"))
        bridge.close()


if __name__ == "__main__":
    unittest.main()
