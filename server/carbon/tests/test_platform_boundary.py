"""Behavioral and compatibility checks for the platform separation."""

import importlib
import unittest

from carbon.accounts.identity import ClientPlatform
from carbon.platforms.session_policy import policy_for
from carbon.platforms.xbox360 import live, prototunnel as xbox_tunnel
from carbon.platforms.xbox360.clear_ingress import ClearIngress, classify_clear_ingress
from carbon.rebroadcaster.service import CarbonRebroadcasterService
from carbon.transport import prototunnel as common_tunnel


class PlatformBoundaryTests(unittest.TestCase):
    def test_pc_unknown_and_xbox_keep_existing_session_conventions(self):
        pc = policy_for(ClientPlatform.PC)
        unknown = policy_for(ClientPlatform.UNKNOWN)
        xbox = policy_for(ClientPlatform.XBOX360)
        self.assertIs(pc, unknown)
        self.assertEqual((pc.use_account_profile_id, pc.wait_for_self_join_ack,
                          pc.defer_peer_publications, pc.permit_clear_loopback_ticket),
                         (False, False, False, False))
        self.assertEqual((xbox.use_account_profile_id, xbox.wait_for_self_join_ack,
                          xbox.defer_peer_publications, xbox.permit_clear_loopback_ticket),
                         (True, True, True, True))

    def test_xbox_preference_only_describes_authenticated_wire_format(self):
        self.assertEqual(live.unverified_xuid_lookup_fields(),
                         {"TXN": "GetUserIdsByXuids", "userIds.[]": "0"})
        self.assertIs(live.authenticated_platform_preference({"clientPlatform": " XBoX360 "}),
                      ClientPlatform.XBOX360)
        self.assertIs(live.authenticated_platform_preference({"xuid": "123"}),
                      ClientPlatform.UNKNOWN)
        self.assertEqual(live.login_platform_fields(ClientPlatform.UNKNOWN), {})
        self.assertEqual(live.login_platform_fields(ClientPlatform.XBOX360),
                         {"clientPlatform": "xbox360"})

    def test_xbox_clear_codec_preserves_the_existing_wire_and_public_api(self):
        packet = common_tunnel.TunnelDatagram(0, (
            common_tunnel.TunnelPacket(7, bytes.fromhex("000000071000")),
            common_tunnel.TunnelPacket(1, bytes.fromhex("00000001dcf7b035")),
        ))
        expected = bytes.fromhex("00000067008100000007100000000001dcf7b035")
        self.assertEqual(xbox_tunnel.encode_clear_datagram(packet), expected)
        self.assertEqual(common_tunnel.encode_clear_datagram(packet), expected)
        self.assertEqual(xbox_tunnel.decode_clear_datagram(expected), packet)
        self.assertEqual(common_tunnel.decode_clear_datagram(expected), packet)
        with self.assertRaises(common_tunnel.ProtoTunnelError):
            xbox_tunnel.decode_clear_datagram(expected[:-1])
        with self.assertRaises(common_tunnel.ProtoTunnelError):
            common_tunnel.decode_clear_datagram(expected[:-1])

    def test_legacy_gateway_import_shares_canonical_module(self):
        self.assertIs(importlib.import_module("carbon.xbox_gateway"),
                      importlib.import_module("carbon.platforms.xbox360.live_gateway"))

    def test_clear_ingress_keeps_loopback_and_authorization_boundaries(self):
        raw = bytes.fromhex("00000067008100000007100000000001dcf7b035")
        checks = []

        def authorize(addr):
            checks.append(addr)
            return False

        def classify(addr, enabled, previous, selected, payload=raw, authorizer=authorize):
            return classify_clear_ingress(
                payload, addr, enabled=enabled, previous_exists=previous,
                clear_selected=selected,
                control_packets=CarbonRebroadcasterService._control_packets,
                authorizer=authorizer,
            )[0]

        local = ("127.0.0.1", 41000)
        remote = ("192.0.2.10", 41000)
        self.assertIs(classify(remote, True, False, False), ClearIngress.ENCRYPTED)
        self.assertIs(classify(local, False, False, False), ClearIngress.ENCRYPTED)
        self.assertIs(classify(local, True, True, False), ClearIngress.ENCRYPTED)
        self.assertEqual(checks, [])
        self.assertIs(classify(local, True, False, False), ClearIngress.REJECTED_AUTHORIZATION)
        self.assertEqual(checks, [local])
        self.assertIs(classify(local, True, False, False, authorizer=None), ClearIngress.ACCEPTED)
        self.assertIs(classify(local, True, True, True, authorizer=None), ClearIngress.ACCEPTED)
        self.assertIs(classify(local, True, True, True, payload=b"\x00"),
                      ClearIngress.REJECTED_SELECTED)
        self.assertIs(classify(remote, True, True, True), ClearIngress.REJECTED_SELECTED)


if __name__ == "__main__":
    unittest.main()
