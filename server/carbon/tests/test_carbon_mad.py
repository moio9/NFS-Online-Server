from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from carbon.core.config import Endpoint
from carbon.mad.campaigns import MADCampaignCatalog, MADCampaignError
from carbon.mad.service import (
    CarbonMADService,
    HORIZONTAL_ASSET_BODY,
    HORIZONTAL_PLACEMENT_NAMES,
    PANORAMIC_ASSET_BODY,
    PANORAMIC_PLACEMENT_NAMES,
    TEST_ASSET_PATH,
    VERTICAL_ASSET_BODY,
    VERTICAL_PLACEMENT_NAMES,
    normalize_request_target,
)
from carbon.mad.protocol import (
    MADAssetCatalogEntry,
    MADProtocolError,
    encode_authenticated_open_session_response,
    encode_asset_catalog_enter_zone_response,
    encode_asset_catalogs_enter_zone_response,
    encode_close_session_response,
    encode_empty_enter_zone_response,
    encode_exit_zone_response,
    encode_impression_update_response,
    encode_locate_service_response,
    encode_open_session_response,
    encode_single_asset_enter_zone_response,
    message_authenticator,
    parse_close_session_request,
    parse_enter_zone_request,
    parse_exit_zone_request,
    parse_impression_update_request,
    parse_locate_service_request,
    parse_open_session_request,
    verify_message_authenticator,
)


CAPTURED_LOCATE_REQUEST = bytes.fromhex(
    "03c900000019"
    "3d00106e66735f636172626f6e5f70635f6e61"
    "3e0003302e30"
)

WAREHOUSE_LOCATE_RESPONSE = bytes.fromhex(
    "03ca00000041"
    "0d00"
    "3b000001941f297c00"
    "22000e3139342e38372e3230322e323437"
    "2d000e3139342e38372e3230322e323437"
    "3a0004"
    "48000e3139342e38372e3230322e323437"
)

CAPTURED_OPEN_SESSION_REQUEST = bytes.fromhex(
    "03cb00000109"
    "3d00106e66735f636172626f6e5f70635f6e61"
    "3e0003302e30"
    "3c01"
    "4100203761623366333838626266353230306562663565353031323630656364646363"
    "4200025043"
    "450080"
    "29408f738bb877882e1f0bfedd57b73c26cd8e75845e0cd414905b255b00f37b"
    "6dc093dd546361c063cd82b043c8845a5fdced82bb833ace8ddabc88d805ec0d"
    "acf9fdb1805e63da08f63063dca6ffc6c87a038bb6186df8c56ebe69dcd3198f"
    "cb4a98157e2d40eb7252098787f215eea3e060bfa49322141fb1f5c19995ebc6"
    "1d00203133613962323339366234383464306239373931306635613564613864663461"
    "3b000001941f297c0a"
    "1e00144566823a09c63b6e197f112c8363726ba7c6a436"
)

WAREHOUSE_OPEN_SESSION_RESPONSE = bytes.fromhex(
    "03cc00000021"
    "2a00000001"
    "2b00001642"
    "1e0014a55610f3782f0ad09bd30dffa6629eceda7ff2bf"
)

CAPTURED_ENTER_ZONE_REQUEST = bytes.fromhex(
    "03cd00000033"
    "470006746e2e332e32"
    "2a00000001"
    "2b00000001"
    "3b000001941f2a2ae5"
    "1e0014aa35a641e152b23b7e747b3b94840dc6d0c915d4"
)


class CarbonMADProtocolTests(unittest.TestCase):
    def test_billboard_assets_match_native_dxt1_layout(self) -> None:
        for body, height, width, size in (
            (HORIZONTAL_ASSET_BODY, 128, 256, 21_952),
            (VERTICAL_ASSET_BODY, 256, 128, 21_952),
            (PANORAMIC_ASSET_BODY, 128, 512, 43_776),
        ):
            self.assertEqual(len(body), size)
            self.assertEqual(body[:4], b"DDS ")
            self.assertEqual(int.from_bytes(body[12:16], "little"), height)
            self.assertEqual(int.from_bytes(body[16:20], "little"), width)
            self.assertEqual(int.from_bytes(body[28:32], "little"), 5)
            self.assertEqual(body[84:88], b"DXT1")

    def test_asset_request_target_accepts_origin_and_retail_absolute_forms(self) -> None:
        self.assertEqual(normalize_request_target(TEST_ASSET_PATH), TEST_ASSET_PATH)
        self.assertEqual(
            normalize_request_target(
                f"http://server.example.com:9000{TEST_ASSET_PATH}?cache=1"
            ),
            TEST_ASSET_PATH,
        )

    def test_captured_locate_request_decodes(self) -> None:
        request = parse_locate_service_request(CAPTURED_LOCATE_REQUEST)
        self.assertEqual(request.product, "nfs_carbon_pc_na")
        self.assertEqual(request.version, "0.0")

    def test_locate_response_matches_warehouse_reference(self) -> None:
        self.assertEqual(
            encode_locate_service_response("194.87.202.247"),
            WAREHOUSE_LOCATE_RESPONSE,
        )

    def test_locate_request_rejects_wrong_declared_length(self) -> None:
        malformed = bytearray(CAPTURED_LOCATE_REQUEST)
        malformed[5] -= 1
        with self.assertRaisesRegex(MADProtocolError, "length mismatch"):
            parse_locate_service_request(bytes(malformed))

    def test_captured_open_session_request_decodes(self) -> None:
        request = parse_open_session_request(CAPTURED_OPEN_SESSION_REQUEST)
        self.assertEqual(request.product, "nfs_carbon_pc_na")
        self.assertEqual(request.version, "0.0")
        self.assertEqual(request.protocol, 1)
        self.assertEqual(request.client_id, "7ab3f388bbf5200ebf5e501260ecddcc")
        self.assertEqual(request.platform, "PC")
        self.assertEqual(len(request.key_exchange), 128)
        self.assertEqual(request.identity, b"13a9b2396b484d0b97910f5a5da8df4a")
        self.assertEqual(request.service_timestamp_ms, 1_735_689_600_010)
        self.assertEqual(len(request.authenticator), 20)

    def test_open_session_response_matches_warehouse_shape(self) -> None:
        self.assertEqual(
            encode_open_session_response(
                0x1642,
                bytes.fromhex("a55610f3782f0ad09bd30dffa6629eceda7ff2bf"),
            ),
            WAREHOUSE_OPEN_SESSION_RESPONSE,
        )

    def test_open_session_response_rejects_wrong_authenticator_size(self) -> None:
        with self.assertRaisesRegex(MADProtocolError, "20 bytes"):
            encode_open_session_response(1, b"too short")

    def test_authenticated_open_session_uses_massive_hmac_shape(self) -> None:
        session_key = b"7ab3f388bbf5200ebf5e501260ecddcc"
        response = encode_authenticated_open_session_response(0x1642, session_key)
        self.assertEqual(
            response[-20:],
            bytes.fromhex("490b698f5730415a449b072c9b580b8f2caf21e9"),
        )
        self.assertTrue(verify_message_authenticator(response, session_key))

    def test_patched_client_open_session_authenticator_verifies(self) -> None:
        session_key = b"7ab3f388bbf5200ebf5e501260ecddcc"
        request = bytearray(CAPTURED_OPEN_SESSION_REQUEST)
        request[-20:] = message_authenticator(bytes(request[1:-22]), session_key)
        self.assertTrue(verify_message_authenticator(bytes(request), session_key))
        request[-1] ^= 1
        self.assertFalse(verify_message_authenticator(bytes(request), session_key))

    def test_captured_enter_zone_request_decodes_and_verifies(self) -> None:
        session_key = b"7ab3f388bbf5200ebf5e501260ecddcc"
        request = parse_enter_zone_request(CAPTURED_ENTER_ZONE_REQUEST)
        self.assertEqual(request.zone, "tn.3.2")
        self.assertEqual(request.result, 1)
        self.assertEqual(request.session_id, 1)
        self.assertEqual(request.service_timestamp_ms, 1_735_689_644_773)
        self.assertIsNone(request.public_address)
        self.assertIsNone(request.private_address)
        self.assertIsNone(request.port)
        self.assertTrue(
            verify_message_authenticator(CAPTURED_ENTER_ZONE_REQUEST, session_key)
        )

    def test_empty_enter_zone_response_is_authenticated(self) -> None:
        session_key = b"7ab3f388bbf5200ebf5e501260ecddcc"
        response = encode_empty_enter_zone_response(session_key)
        self.assertEqual(response[:6], bytes.fromhex("03ce0000001d"))
        self.assertEqual(response[6:12], bytes.fromhex("010000250000"))
        self.assertTrue(verify_message_authenticator(response, session_key))

    def test_single_asset_enter_zone_response_has_retail_records(self) -> None:
        session_key = b"7ab3f388bbf5200ebf5e501260ecddcc"
        response = encode_single_asset_enter_zone_response(
            session_key,
            asset_url="http://server.example.com:9000/adsrv/assets/nfs-online-mad-probe.bin",
            asset_body=b"NFS-ONLINE-MAD-DOWNLOAD-PROBE-v1\n",
            placement_name="ADS_FASTFOOD_01_D",
        )
        payload = response[6:-23]
        self.assertEqual(response[0:2], bytes.fromhex("03ce"))
        self.assertEqual(response[6:12], bytes.fromhex("010001250001"))
        self.assertIn(b"\xde", payload)
        self.assertIn(b"\xdd", payload)
        self.assertIn(b"ADS_FASTFOOD_01_D", payload)
        self.assertIn(b"/adsrv/assets/nfs-online-mad-probe.bin", payload)
        self.assertIn(b"aad9034facd0521c45ed0a3114aff4c6", payload)
        self.assertTrue(verify_message_authenticator(response, session_key))

    def test_live_1_0_6_billboard_slot_inventory(self) -> None:
        self.assertEqual(len(HORIZONTAL_PLACEMENT_NAMES), 15)
        self.assertEqual(len(VERTICAL_PLACEMENT_NAMES), 8)
        self.assertEqual(len(PANORAMIC_PLACEMENT_NAMES), 8)
        self.assertIn("ADS_MAZDASPEED3_01_D", HORIZONTAL_PLACEMENT_NAMES)
        self.assertIn("ADS_FAKE2_02_D", VERTICAL_PLACEMENT_NAMES)
        self.assertIn("ADS_PROGRESSIVE_03_D", PANORAMIC_PLACEMENT_NAMES)

    def test_single_asset_enter_zone_response_rejects_empty_body(self) -> None:
        with self.assertRaisesRegex(MADProtocolError, "body is empty"):
            encode_single_asset_enter_zone_response(
                b"session-key",
                asset_url="http://example.invalid/asset",
                asset_body=b"",
                placement_name="ADS_FASTFOOD_02_D",
            )

    def test_asset_catalog_reuses_one_download_for_multiple_slots(self) -> None:
        session_key = b"session-key"
        response = encode_asset_catalog_enter_zone_response(
            session_key,
            asset_url="http://example.invalid/asset",
            asset_body=b"asset",
            placement_names=("ADS_FAKE1_01_D", "ADS_FASTFOOD_01_D"),
        )
        self.assertEqual(response[6:12], bytes.fromhex("010001250002"))
        self.assertEqual(response.count(b"\xde"), 1)
        self.assertEqual(response.count(b"\xdd"), 2)
        self.assertTrue(verify_message_authenticator(response, session_key))

    def test_multi_asset_catalog_separates_billboard_layouts(self) -> None:
        session_key = b"session-key"
        response = encode_asset_catalogs_enter_zone_response(
            session_key,
            catalogs=(
                MADAssetCatalogEntry(
                    asset_url="http://example.invalid/horizontal.dds",
                    asset_body=HORIZONTAL_ASSET_BODY,
                    placement_names=HORIZONTAL_PLACEMENT_NAMES,
                    asset_id=0xC011_0001,
                    placement_id=0xC011_1001,
                ),
                MADAssetCatalogEntry(
                    asset_url="http://example.invalid/vertical.dds",
                    asset_body=VERTICAL_ASSET_BODY,
                    placement_names=VERTICAL_PLACEMENT_NAMES,
                    asset_id=0xC011_0002,
                    placement_id=0xC011_2001,
                ),
                MADAssetCatalogEntry(
                    asset_url="http://example.invalid/panoramic.dds",
                    asset_body=PANORAMIC_ASSET_BODY,
                    placement_names=PANORAMIC_PLACEMENT_NAMES,
                    asset_id=0xC011_0003,
                    placement_id=0xC011_3001,
                ),
            ),
        )
        placement_count = (
            len(HORIZONTAL_PLACEMENT_NAMES)
            + len(VERTICAL_PLACEMENT_NAMES)
            + len(PANORAMIC_PLACEMENT_NAMES)
        )
        self.assertEqual(response[6:10], bytes.fromhex("01000325"))
        self.assertEqual(int.from_bytes(response[10:12], "big"), placement_count)
        self.assertIn(b"horizontal.dds", response)
        self.assertIn(b"vertical.dds", response)
        self.assertIn(b"panoramic.dds", response)
        for placement_name in (
            HORIZONTAL_PLACEMENT_NAMES
            + VERTICAL_PLACEMENT_NAMES
            + PANORAMIC_PLACEMENT_NAMES
        ):
            self.assertIn(placement_name.encode("ascii"), response)
        self.assertTrue(verify_message_authenticator(response, session_key))


def _authenticated_request(message_type: int, session_key: bytes, fields: bytes) -> bytes:
    payload_size = len(fields) + 1 + 2 + 20
    unsigned = bytes((message_type,)) + payload_size.to_bytes(4, "big") + fields + b"\x1e"
    authenticator = message_authenticator(unsigned, session_key)
    payload = fields + b"\x1e\x00\x14" + authenticator
    return b"\x03" + bytes((message_type,)) + len(payload).to_bytes(4, "big") + payload


class MADLifecycleProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session_key = b"7ab3f388bbf5200ebf5e501260ecddcc"
        self.session_id = 7
        self.timestamp = 1_735_689_700_000

    def test_exit_zone_round_trip_shape(self) -> None:
        zone = b"tn.3.2"
        fields = (
            b"\x47" + len(zone).to_bytes(2, "big") + zone
            + b"\x2a" + (1).to_bytes(4, "big")
            + b"\x2b" + self.session_id.to_bytes(4, "big")
            + b"\x3b" + self.timestamp.to_bytes(8, "big")
        )
        request_body = _authenticated_request(0xCF, self.session_key, fields)
        request = parse_exit_zone_request(request_body)
        self.assertEqual(request.zone, "tn.3.2")
        self.assertEqual(request.session_id, self.session_id)
        response = encode_exit_zone_response(self.session_key)
        self.assertEqual(response[:2], bytes.fromhex("03d0"))
        self.assertTrue(verify_message_authenticator(response, self.session_key))

    def test_close_session_round_trip_shape(self) -> None:
        fields = (
            b"\x2b" + self.session_id.to_bytes(4, "big")
            + b"\x2a" + (1).to_bytes(4, "big")
            + b"\x3b" + self.timestamp.to_bytes(8, "big")
        )
        request_body = _authenticated_request(0xD1, self.session_key, fields)
        request = parse_close_session_request(request_body)
        self.assertEqual(request.session_id, self.session_id)
        response = encode_close_session_response(self.session_key)
        self.assertEqual(response[:2], bytes.fromhex("03d2"))
        self.assertTrue(verify_message_authenticator(response, self.session_key))

    def test_impression_update_accepts_retail_nested_records(self) -> None:
        impression = b"\x26\x00\x00\x12\x34"
        interaction = b"\x21\x00"
        fields = (
            b"\x2a" + (1).to_bytes(4, "big")
            + b"\x2b" + self.session_id.to_bytes(4, "big")
            + b"\xdf" + len(impression).to_bytes(4, "big") + impression
            + b"\xe0" + len(interaction).to_bytes(4, "big") + interaction
            + b"\x3b" + self.timestamp.to_bytes(8, "big")
        )
        request_body = _authenticated_request(0xD3, self.session_key, fields)
        request = parse_impression_update_request(request_body)
        self.assertEqual(request.impression_records, (impression,))
        self.assertEqual(request.interaction_records, (interaction,))
        response = encode_impression_update_response(self.session_key)
        self.assertEqual(response[:2], bytes.fromhex("03d4"))
        self.assertTrue(verify_message_authenticator(response, self.session_key))


class MADCampaignTests(unittest.TestCase):
    def test_zone_campaigns_rotate_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for prefix in ("a", "b"):
                (root / f"{prefix}-horizontal.dds").write_bytes(HORIZONTAL_ASSET_BODY)
                (root / f"{prefix}-vertical.dds").write_bytes(VERTICAL_ASSET_BODY)
                (root / f"{prefix}-panoramic.dds").write_bytes(PANORAMIC_ASSET_BODY)
            config = root / "campaigns.json"
            config.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "rotation_seconds": 60,
                        "campaigns": [
                            {
                                "id": prefix,
                                "zones": ["tn.3.*"],
                                "assets": {
                                    layout: f"{prefix}-{layout}.dds"
                                    for layout in ("horizontal", "vertical", "panoramic")
                                },
                            }
                            for prefix in ("a", "b")
                        ],
                    }
                ),
                encoding="utf-8",
            )
            catalog = MADCampaignCatalog.load(
                config,
                fallback_assets={
                    "horizontal": HORIZONTAL_ASSET_BODY,
                    "vertical": VERTICAL_ASSET_BODY,
                    "panoramic": PANORAMIC_ASSET_BODY,
                },
                fallback_paths={},
            )
        first = catalog.select("tn.3.2", unix_time=0).campaign.campaign_id
        second = catalog.select("tn.3.2", unix_time=60).campaign.campaign_id
        self.assertNotEqual(first, second)
        self.assertEqual(
            catalog.select("tn.3.2", unix_time=120).campaign.campaign_id,
            first,
        )

    def test_specific_zone_campaign_has_priority_over_wildcard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for prefix in ("fallback", "downtown"):
                (root / f"{prefix}-horizontal.dds").write_bytes(HORIZONTAL_ASSET_BODY)
                (root / f"{prefix}-vertical.dds").write_bytes(VERTICAL_ASSET_BODY)
                (root / f"{prefix}-panoramic.dds").write_bytes(PANORAMIC_ASSET_BODY)
            config = root / "campaigns.json"
            config.write_text(
                json.dumps(
                    {
                        "campaigns": [
                            {
                                "id": "fallback",
                                "zones": ["*"],
                                "assets": {
                                    layout: f"fallback-{layout}.dds"
                                    for layout in ("horizontal", "vertical", "panoramic")
                                },
                            },
                            {
                                "id": "downtown",
                                "zones": ["tn.3.*"],
                                "assets": {
                                    layout: f"downtown-{layout}.dds"
                                    for layout in ("horizontal", "vertical", "panoramic")
                                },
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            catalog = MADCampaignCatalog.load(
                config,
                fallback_assets={},
                fallback_paths={},
            )
        self.assertEqual(
            catalog.select("tn.3.2", unix_time=0).campaign.campaign_id,
            "downtown",
        )
        self.assertEqual(
            catalog.select("other-zone", unix_time=0).campaign.campaign_id,
            "fallback",
        )

    def test_invalid_dds_dimensions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = bytearray(HORIZONTAL_ASSET_BODY)
            bad[16:20] = (128).to_bytes(4, "little")
            for layout, body in (
                ("horizontal", bytes(bad)),
                ("vertical", VERTICAL_ASSET_BODY),
                ("panoramic", PANORAMIC_ASSET_BODY),
            ):
                (root / f"{layout}.dds").write_bytes(body)
            config = root / "campaigns.json"
            config.write_text(
                json.dumps(
                    {
                        "campaigns": [
                            {
                                "id": "bad",
                                "zones": ["*"],
                                "assets": {
                                    layout: f"{layout}.dds"
                                    for layout in ("horizontal", "vertical", "panoramic")
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(MADCampaignError, "must be 256x128"):
                MADCampaignCatalog.load(
                    config,
                    fallback_assets={},
                    fallback_paths={},
                )

    def test_service_registers_campaign_asset_urls(self) -> None:
        service = CarbonMADService(
            Endpoint("mad.example", 9000),
            campaign_path=None,
        )
        catalogs = service._catalog_entries("default")
        self.assertEqual(len(catalogs), 3)
        self.assertTrue(all("default-" in item.asset_url for item in catalogs))


if __name__ == "__main__":
    unittest.main()
