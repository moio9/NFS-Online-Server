"""Wire and transaction tests for the clean Carbon FESL service."""

import unittest
import base64
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote

from common.legal import TERMS_OF_SERVICE_TEXT, TERMS_OF_SERVICE_VERSION

from carbon.accounts.identity import IdentityStore
from carbon.core.config import Endpoint
from carbon.dlc import (
    CarbonDLCAssignments,
    CarbonDLCCatalog,
    CarbonDLCInventory,
)
from carbon.fesl.blob import CarbonBlobStore
from carbon.fesl.frame import (
    FESLFrame,
    FESLFrameError,
    FESLStreamDecoder,
    decode_one,
    packetize_frame,
)
from carbon.fesl.service import CarbonEndpoints, CarbonFESLService, FESLConnection
from carbon.theater.directory import CarbonGameDirectory
from carbon.progression import CarbonProgressionStore


class CarbonFESLFrameTests(unittest.TestCase):
    def test_nonempty_payload_uses_retail_lf_nul_terminator(self) -> None:
        frame = FESLFrame.from_fields("fsys", {"TXN": "Hello"}, transaction=4)
        self.assertEqual(frame.payload, b"TXN=Hello\n\x00")

    def test_fragmented_and_coalesced_frames(self) -> None:
        first = FESLFrame.from_fields("fsys", {"TXN": "Hello"}, transaction=4).encode()
        second = FESLFrame.from_fields("acct", {"TXN": "Login", "name": "Driver"}, transaction=5).encode()
        decoder = FESLStreamDecoder()
        self.assertEqual(decoder.feed(first[:7]), [])
        frames = decoder.feed(first[7:] + second)
        self.assertEqual([(frame.command, frame.transaction) for frame in frames], [("fsys", 4), ("acct", 5)])
        self.assertEqual(frames[1].fields["name"], "Driver")

    def test_rejects_oversized_frame(self) -> None:
        raw = FESLFrame.from_fields("fsys", {"TXN": "Hello"}).encode()
        with self.assertRaises(FESLFrameError):
            decode_one(raw, max_frame_size=12)

    def test_large_reply_uses_retail_fesl_fragment_envelope(self) -> None:
        logical = FESLFrame.from_fields(
            "blob",
            {
                "TXN": "GetBlobContent",
                "blobId": "3",
                "content": "A" * 52_064,
            },
            transaction=0x80000027,
        )
        fragments = packetize_frame(logical)

        self.assertGreater(len(fragments), 1)
        self.assertTrue(all(frame.command == "blob" for frame in fragments))
        self.assertTrue(all(frame.transaction == 0xB0000027 for frame in fragments))
        self.assertTrue(all(len(frame.fields["data"]) <= 8_096 for frame in fragments))
        self.assertTrue(all(len(frame.encode()) < 8_192 for frame in fragments))

        encoded = "".join(
            frame.fields["data"].replace("%3d", "=")
            for frame in fragments
        )
        rebuilt = base64.b64decode(encoded)
        self.assertEqual(rebuilt + b"\x00", logical.payload)
        self.assertEqual(
            {frame.fields["decodedSize"] for frame in fragments},
            {str(len(rebuilt))},
        )
        self.assertEqual(
            {frame.fields["size"] for frame in fragments},
            {str(len(encoded))},
        )


class CarbonFESLServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        identities = IdentityStore(token_factory=lambda: "test-session-key.")
        self.service = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
            identities,
            clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
            authentication_mode="open",
        )
        self.connection = FESLConnection()

    def _login(
        self,
        name: str = "Driver",
        connection: FESLConnection | None = None,
        *,
        transaction: int = 40,
    ) -> FESLConnection:
        target = connection or self.connection
        self.service.dispatch(
            FESLFrame.from_fields(
                "acct",
                {"TXN": "Login", "name": name},
                transaction=transaction,
            ),
            target,
        )
        self.assertIsNotNone(target.identity)
        return target

    def test_hello_produces_endpoint_reply_and_memcheck(self) -> None:
        request = FESLFrame.from_fields("fsys", {"TXN": "Hello", "clientType": "client"}, transaction=7)
        replies = self.service.dispatch(request, self.connection)
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0].transaction, 7)
        self.assertEqual(replies[0].fields["domainPartition.subDomain"], "NFS-2007")
        self.assertEqual(replies[0].fields["theaterPort"], "18215")
        self.assertEqual(replies[0].fields["activityTimeoutSecs"], "0")
        self.assertEqual(replies[1].fields["TXN"], "MemCheck")

    def test_hello_advertises_configured_activity_timeout(self) -> None:
        service = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
            IdentityStore(),
            activity_timeout_seconds=600,
            authentication_mode="open",
        )
        request = FESLFrame.from_fields("fsys", {"TXN": "Hello"}, transaction=7)
        replies = service.dispatch(request, FESLConnection())
        self.assertEqual(replies[0].fields["activityTimeoutSecs"], "600")

    def test_negative_activity_timeout_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CarbonFESLService(
                CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
                IdentityStore(),
                activity_timeout_seconds=-1,
                authentication_mode="open",
            )

    def test_direct_echo_closes_the_client_liveness_transaction(self) -> None:
        request = FESLFrame.from_fields(
            "ECHO",
            {"TID": "7", "TYPE": "1", "UID": "12345", "TXN": "ECHO"},
            transaction=0,
        )
        reply = self.service.dispatch(request, self.connection)[0]
        self.assertEqual(reply.command, "ECHO")
        self.assertEqual(reply.transaction, 0)
        self.assertEqual(
            reply.fields,
            {"TID": "7", "TXN": "ECHO", "ERR": "0", "TYPE": "1", "UID": "12345"},
        )

    def test_retail_ping_is_async_and_client_response_is_consumed(self) -> None:
        ping = self.service.ping_frame()
        self.assertEqual(ping.command, "fsys")
        self.assertEqual(ping.transaction, 0)
        self.assertEqual(ping.fields, {"TXN": "Ping"})

        response = FESLFrame.from_fields("fsys", {"TXN": "Ping"}, transaction=0x80000000)
        self.assertEqual(self.service.dispatch(response, self.connection), [])
        self.assertEqual(self.connection.ping_responses, 1)

    def test_memcheck_challenge_uses_runtime_salt_and_response_is_consumed(self) -> None:
        request = FESLFrame.from_fields(
            "fsys",
            {"TXN": "MemCheck"},
            transaction=0x80000002,
        )
        challenge = self.service.dispatch(request, self.connection)[0]
        self.assertEqual(challenge.fields["TXN"], "MemCheck")
        self.assertEqual(challenge.fields["memcheck.[]"], "0")
        self.assertEqual(challenge.fields["type"], "0")
        self.assertTrue(challenge.fields["salt"].isdigit())
        self.assertNotEqual(challenge.fields["salt"], "STUB_SALT")

        response = FESLFrame.from_fields(
            "fsys",
            {"TXN": "MemCheck", "result": ""},
            transaction=0x80000000,
        )
        self.assertEqual(self.service.dispatch(response, self.connection), [])

    def test_virus_entitlement_inventory_and_metrics_ack_are_preserved(self) -> None:
        inventory = self.service.dispatch(
            FESLFrame.from_fields(
                "dobj",
                {
                    "TXN": "GetObjectInventory",
                    "domainId": "eagames",
                    "subdomainId": "nfs-2007",
                    "partitionKey": "download_content",
                    "objectIds.[]": "0",
                },
                transaction=0x80000004,
            ),
            self.connection,
        )[0]
        self.assertEqual(
            inventory.fields,
            self.service.dlc_inventory.fields_for(None),
        )
        self.assertEqual(inventory.fields["entitlements.[]"], "1")
        self.assertEqual(
            inventory.fields["entitlements.0.objectId"],
            "VIRUS_PURSUIT_PANDEMIC",
        )

        metrics = self.service.dispatch(
            FESLFrame.from_fields(
                "mtrx",
                {"TXN": "ReportMetrics", "events.[]": "0"},
                transaction=0x80000007,
            ),
            self.connection,
        )[0]
        self.assertEqual(metrics.transaction, 0x80000007)
        self.assertEqual(metrics.fields, {"TXN": "ReportMetrics"})

    def test_authenticated_dobj_merges_persistent_race_viruses(self) -> None:
        progression = CarbonProgressionStore()
        service = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
            IdentityStore(token_factory=lambda: "virus-session."),
            progression=progression,
            dlc_inventory=CarbonDLCInventory(
                CarbonDLCCatalog.from_path(
                    "../../data/carbon/dlc_catalog.json"
                ),
                CarbonDLCAssignments(
                    default=("default_dlc",),
                    accounts={"moio": ("all",)},
                    personas={},
                ),
            ),
            authentication_mode="open",
        )
        connection = FESLConnection()
        service.dispatch(
            FESLFrame.from_fields(
                "acct",
                {"TXN": "Login", "name": "FreshDriver"},
                transaction=1,
            ),
            connection,
        )
        assert connection.identity is not None
        progression.set_stat("FreshDriver", "Virus1", 1.0)

        reply = service.dispatch(
            FESLFrame.from_fields(
                "dobj",
                {"TXN": "GetObjectInventory"},
                transaction=2,
            ),
            connection,
        )[0]
        count = int(reply.fields["entitlements.[]"])
        object_ids = {
            reply.fields[f"entitlements.{index}.objectId"]
            for index in range(count)
        }
        self.assertEqual(count, 7)
        self.assertIn("VIRUS_KNOCKOUT_FEVER", object_ids)
        self.assertNotIn("VIRUS_CARBON_PLAGUE", object_ids)

    def test_static_all_assignment_seeds_original_style_carrier_stats(self) -> None:
        progression = CarbonProgressionStore()
        service = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
            IdentityStore(token_factory=lambda: "seed-session."),
            progression=progression,
            dlc_inventory=CarbonDLCInventory(
                CarbonDLCCatalog.from_path(
                    "../../data/carbon/dlc_catalog.json"
                ),
                CarbonDLCAssignments(
                    default=("default_dlc",),
                    accounts={"moio": ("all",)},
                    personas={},
                ),
            ),
            authentication_mode="open",
        )
        connection = FESLConnection()
        service.dispatch(
            FESLFrame.from_fields(
                "acct",
                {"TXN": "Login", "name": "moio"},
                transaction=1,
            ),
            connection,
        )
        assert connection.identity is not None
        self.assertEqual(progression.stat_for_profile(connection.identity.profile_id, "Virus1"), 1.0)
        self.assertEqual(progression.stat_for_profile(connection.identity.profile_id, "Virus2"), 1.0)
        self.assertEqual(progression.stat_for_profile(connection.identity.profile_id, "Virus3"), 1.0)

    def test_unknown_stateless_operations_are_not_acknowledged_as_success(self) -> None:
        requests = (
            FESLFrame.from_fields(
                "dobj",
                {"TXN": "UnknownInventory"},
                transaction=0x80000021,
            ),
            FESLFrame.from_fields(
                "mtrx",
                {"TXN": "UnknownMetrics"},
                transaction=0x80000022,
            ),
            FESLFrame.from_fields(
                "rank",
                {"TXN": "UnknownRanking"},
                transaction=0x80000023,
            ),
            FESLFrame.from_fields(
                "blob",
                {"TXN": "UnknownBlob"},
                transaction=0x80000024,
            ),
        )
        with self.assertLogs(
            "carbon.fesl.service",
            level="WARNING",
        ) as captured:
            for request in requests:
                self.assertEqual(
                    self.service.dispatch(request, self.connection),
                    [],
                )

        joined = "\n".join(captured.output)
        self.assertIn("operation=UnknownInventory", joined)
        self.assertIn("operation=UnknownMetrics", joined)
        self.assertIn("operation=UnknownRanking", joined)
        self.assertIn("operation=UnknownBlob", joined)

    def test_unknown_system_and_account_transactions_are_logged(self) -> None:
        with self.assertLogs(
            "carbon.fesl.service",
            level="WARNING",
        ) as captured:
            self.assertEqual(
                self.service.dispatch(
                    FESLFrame.from_fields(
                        "fsys",
                        {"TXN": "UnknownSystem", "probe": "1"},
                        transaction=0x80000011,
                    ),
                    self.connection,
                ),
                [],
            )
            self.assertEqual(
                self.service.dispatch(
                    FESLFrame.from_fields(
                        "acct",
                        {"TXN": "UnknownAccount", "probe": "2"},
                        transaction=0x80000012,
                    ),
                    self.connection,
                ),
                [],
            )
        joined = "\n".join(captured.output)
        self.assertIn("unhandled system transaction", joined)
        self.assertIn("operation=UnknownSystem", joined)
        self.assertIn("unhandled account transaction", joined)
        self.assertIn("operation=UnknownAccount", joined)

    def test_login_and_persona_share_one_session(self) -> None:
        login = FESLFrame.from_fields("acct", {"TXN": "Login", "name": "Driver"}, transaction=8)
        login_reply = self.service.dispatch(login, self.connection)[0]
        self.assertEqual(login_reply.fields["lkey"], "test-session-key.")
        self.assertEqual(login_reply.fields["displayName"], "Driver")

        personas = FESLFrame.from_fields("acct", {"TXN": "NuGetPersonas"}, transaction=9)
        persona_reply = self.service.dispatch(personas, self.connection)[0]
        self.assertEqual(persona_reply.fields["personas.0"], "Driver")

    def test_country_list_closes_the_account_transaction(self) -> None:
        request = FESLFrame.from_fields("acct", {"TXN": "GetCountryList"}, transaction=10)
        reply = self.service.dispatch(request, self.connection)[0]
        self.assertEqual(reply.transaction, 10)
        self.assertEqual(reply.fields["countryList.[]"], "10")
        self.assertEqual(reply.fields["countryList.1.ISOCode"], "RO")

    def test_terms_of_service_is_available_before_login(self) -> None:
        request = FESLFrame.from_fields(
            "acct",
            {"TXN": "GetTos"},
            transaction=0x80000003,
        )

        reply = self.service.dispatch(request, self.connection)[0]

        self.assertEqual(reply.command, "acct")
        self.assertEqual(reply.transaction, 0x80000003)
        self.assertEqual(reply.fields["TXN"], "GetTos")
        self.assertEqual(reply.fields["version"], TERMS_OF_SERVICE_VERSION)
        self.assertEqual(unquote(reply.fields["tos"]), TERMS_OF_SERVICE_TEXT)
        self.assertIsNone(self.connection.identity)

    def test_chunked_metrics_are_reassembled_and_acked_once(self) -> None:
        encoded = base64.b64encode(b"TXN=ReportMetrics\x00").decode("ascii")
        split = len(encoded) // 2
        first = FESLFrame.from_fields(
            "mtrx",
            {"data": encoded[:split], "size": str(len(encoded))},
            transaction=0xB0000004,
        )
        second = FESLFrame.from_fields(
            "mtrx",
            {"data": encoded[split:].replace("=", "%3d"), "size": str(len(encoded))},
            transaction=0xB0000004,
        )
        self.assertEqual(self.service.dispatch(first, self.connection), [])
        reply = self.service.dispatch(second, self.connection)[0]
        self.assertEqual(reply.transaction, 0x80000004)
        self.assertEqual(reply.fields["TXN"], "ReportMetrics")

    def test_oversized_fragment_envelope_is_rejected_without_buffering(self) -> None:
        request = FESLFrame.from_fields(
            "blob",
            {"data": "AAAA", "size": str((2 * 1024 * 1024) + 1)},
            transaction=0xB0000004,
        )
        with self.assertLogs(
            "carbon.fesl.service",
            level="WARNING",
        ) as captured:
            self.assertEqual(self.service.dispatch(request, self.connection), [])
        self.assertEqual(self.connection.chunk_buffers, {})
        self.assertIn("reason=size-limit", "\n".join(captured.output))

    def test_empty_blob_list_closes_the_shadow_lookup(self) -> None:
        request = FESLFrame.from_fields(
            "blob",
            {
                "TXN": "ListBlobInfo",
                "chunkSize": "10000",
                "maxRecords": "1",
                "name": "race-shadow",
                "nameCaseSensitive": "0",
                "nameWildcardMatch": "0",
                "ownerId": "260394189",
                "ownerType": "1",
                "searchAttributes.[]": "0",
                "type": "1",
            },
            transaction=0x8000002B,
        )
        reply = self.service.dispatch(request, self.connection)[0]
        self.assertEqual(reply.command, "blob")
        self.assertEqual(reply.transaction, 0x8000002B)
        self.assertEqual(reply.fields["TXN"], "ListBlobInfo")
        self.assertEqual(reply.fields["blobs.[]"], "0")
        self.assertEqual(reply.fields["nextChunkFlag"], "0")

    def test_blob_shadow_add_list_and_content_round_trip(self) -> None:
        self._login()
        owner_id = self.connection.identity.profile_id
        add = FESLFrame.from_fields(
            "blob",
            {
                "TXN": "AddBlob",
                "ownerId": str(owner_id),
                "ownerType": "1",
                "type": "7",
                "formatType": "2",
                "creator": "Driver",
                "name": "shadow-30F07253",
                "version": "1",
                "content": "R0hPU1Q=",
                "attributes.[]": "1",
                "attributes.0.name": "event",
                "attributes.0.type": "0",
                "attributes.0.value": "30F07253",
            },
            transaction=0xB0000041,
        )
        add_reply = self.service.dispatch(add, self.connection)[0]
        self.assertEqual(add_reply.transaction, 0x80000041)
        blob_id = add_reply.fields["blobId"]

        listing = FESLFrame.from_fields(
            "blob",
            {
                "TXN": "ListBlobInfo",
                "ownerId": str(owner_id),
                "ownerType": "1",
                "type": "7",
                "name": "shadow-*",
                "nameWildcardMatch": "1",
                "searchAttributes.[]": "1",
                "searchAttributes.0.name": "event",
                "searchAttributes.0.type": "0",
                "searchAttributes.0.value": "30F07253",
            },
            transaction=0x80000042,
        )
        list_reply = self.service.dispatch(listing, self.connection)[0]
        self.assertEqual(list_reply.fields["blobs.[]"], "1")
        self.assertEqual(list_reply.fields["blobs.0.blobId"], blob_id)
        self.assertEqual(list_reply.fields["blobs.0.name"], "shadow-30F07253")
        self.assertEqual(list_reply.fields["blobs.0.attributes.0.value"], "30F07253")

        content = FESLFrame.from_fields(
            "blob",
            {"TXN": "GetBlobContent", "blobId": blob_id},
            transaction=0x80000043,
        )
        content_reply = self.service.dispatch(content, self.connection)[0]
        self.assertEqual(content_reply.fields["content"], "R0hPU1Q=")
        self.assertEqual(content_reply.fields["nextChunkFlag"], "0")
        self.assertEqual(content_reply.fields["size"], "8")
        self.assertEqual(content_reply.fields["unencodedSize"], "5")

    def test_large_blob_content_reports_encoded_and_raw_sizes(self) -> None:
        self._login()
        raw_shadow = (bytes(range(256)) * 152) + bytes(range(136))
        encoded_shadow = base64.b64encode(raw_shadow).decode("ascii")
        self.assertEqual(len(raw_shadow), 39_048)
        self.assertEqual(len(encoded_shadow), 52_064)
        blob_id = self.service.dispatch(
            FESLFrame.from_fields(
                "blob",
                {
                    "TXN": "AddBlob",
                    "ownerId": str(self.connection.identity.profile_id),
                    "ownerType": "1",
                    "type": "11",
                    "name": "GHOST_A44B31B9_0",
                    "content": encoded_shadow,
                },
                transaction=0xB0000044,
            ),
            self.connection,
        )[0].fields["blobId"]

        reply = self.service.dispatch(
            FESLFrame.from_fields(
                "blob",
                {"TXN": "GetBlobContent", "blobId": blob_id},
                transaction=0x80000045,
            ),
            self.connection,
        )[0]
        self.assertEqual(reply.fields["size"], "52064")
        self.assertEqual(reply.fields["unencodedSize"], "39048")
        self.assertEqual(reply.fields["content"], encoded_shadow)
        self.assertLess(len(reply.encode()), 65_535)

    def test_blob_content_restores_percent_escaped_base64_padding(self) -> None:
        self._login()
        cases = (
            ("single-padding", "QUI%3d", "QUI="),
            ("double-padding", "QQ%3d%3D", "QQ=="),
        )

        for index, (name, escaped, normalized) in enumerate(cases):
            with self.subTest(name=name):
                reply = self.service.dispatch(
                    FESLFrame.from_fields(
                        "blob",
                        {
                            "TXN": "AddBlob",
                            "type": "12",
                            "name": name,
                            "content": escaped,
                        },
                        transaction=0xB0000060 + index,
                    ),
                    self.connection,
                )[0]
                blob = self.service.blobs.get(int(reply.fields["blobId"]))
                self.assertIsNotNone(blob)
                self.assertEqual(blob.content, normalized)

    def test_chunked_photo_add_restores_inner_base64_padding(self) -> None:
        self._login()
        raw_photo = b"P" * 6_100
        normalized = base64.b64encode(raw_photo).decode("ascii")
        escaped = normalized.replace("=", "%3d")
        logical = FESLFrame.from_fields(
            "blob",
            {
                "TXN": "AddBlob",
                "type": "12",
                "name": "PHOTO_2",
                "content": escaped,
            },
            transaction=0x80000062,
        )
        fragments = packetize_frame(logical)
        self.assertGreater(len(fragments), 1)

        replies = []
        for fragment in fragments:
            replies.extend(self.service.dispatch(fragment, self.connection))

        self.assertEqual(len(replies), 1)
        blob = self.service.blobs.get(int(replies[0].fields["blobId"]))
        self.assertIsNotNone(blob)
        self.assertEqual(blob.content, normalized)
        self.assertEqual(blob.unencoded_size, len(raw_photo))

    def test_blob_add_requires_login_and_ignores_spoofed_owner(self) -> None:
        unauthenticated = self.service.dispatch(
            FESLFrame.from_fields(
                "blob",
                {
                    "TXN": "AddBlob",
                    "ownerId": "123",
                    "type": "11",
                    "name": "spoofed",
                    "content": "R0hPU1Q=",
                },
                transaction=0xB0000046,
            ),
            self.connection,
        )[0]
        self.assertEqual(unauthenticated.fields["errorCode"], "120")
        self.assertEqual(self.service.blobs.search(), [])

        self._login(transaction=47)
        owner_id = self.connection.identity.profile_id
        add_request = FESLFrame.from_fields(
            "blob",
            {
                "TXN": "AddBlob",
                "ownerId": "123",
                "ownerType": "999",
                "creator": "NotDriver",
                "type": "11",
                "name": "owned",
                "content": "R0hPU1Q=",
            },
            transaction=0xB0000048,
        )
        added = self.service.dispatch(add_request, self.connection)[0]
        blob = self.service.blobs.get(int(added.fields["blobId"]))
        self.assertIsNotNone(blob)
        self.assertEqual(blob.owner_id, owner_id)
        self.assertEqual(blob.owner_type, 1)
        self.assertEqual(blob.creator, "Driver")
        self.assertEqual(self.service.dispatch(add_request, self.connection), [])
        self.assertEqual(len(self.service.blobs.search(owner_id=owner_id)), 1)

    def test_foreign_blob_cannot_be_updated_or_removed(self) -> None:
        owner = self._login("Owner", transaction=49)
        added = self.service.dispatch(
            FESLFrame.from_fields(
                "blob",
                {
                    "TXN": "AddBlob",
                    "type": "11",
                    "name": "protected",
                    "content": "T1JJR0lOQUw=",
                },
                transaction=0xB0000050,
            ),
            owner,
        )[0]
        blob_id = int(added.fields["blobId"])

        attacker = self._login(
            "Attacker",
            FESLConnection(),
            transaction=51,
        )
        update = self.service.dispatch(
            FESLFrame.from_fields(
                "blob",
                {
                    "TXN": "UpdateBlobContent",
                    "blobId": str(blob_id),
                    "content": "SEFDS0VE",
                },
                transaction=0xB0000052,
            ),
            attacker,
        )[0]
        remove = self.service.dispatch(
            FESLFrame.from_fields(
                "blob",
                {"TXN": "RemoveBlob", "blobId": str(blob_id)},
                transaction=0xB0000053,
            ),
            attacker,
        )[0]
        self.assertEqual(update.fields["errorCode"], "120")
        self.assertEqual(remove.fields["errorCode"], "120")
        self.assertEqual(self.service.blobs.get(blob_id).content, "T1JJR0lOQUw=")

        owner_update = self.service.dispatch(
            FESLFrame.from_fields(
                "blob",
                {
                    "TXN": "UpdateBlobContent",
                    "blobId": str(blob_id),
                    "content": "VVBEQVRFRA%3d%3D",
                },
                transaction=0xB0000054,
            ),
            owner,
        )[0]
        self.assertNotIn("errorCode", owner_update.fields)
        self.assertEqual(self.service.blobs.get(blob_id).content, "VVBEQVRFRA==")

    def test_oversized_blob_content_is_rejected(self) -> None:
        self._login(transaction=55)
        reply = self.service.dispatch(
            FESLFrame.from_fields(
                "blob",
                {
                    "TXN": "AddBlob",
                    "type": "12",
                    "name": "too-large",
                    "content": "A" * ((1024 * 1024) + 1),
                },
                transaction=0xB0000056,
            ),
            self.connection,
        )[0]
        self.assertEqual(reply.fields["errorCode"], "120")
        self.assertEqual(self.service.blobs.search(), [])

    def test_blob_store_persists_uploaded_shadow(self) -> None:
        with TemporaryDirectory() as temporary:
            blob_path = Path(temporary) / "carbon_blobs.json"
            first = CarbonFESLService(
                CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
                IdentityStore(),
                blobs=CarbonBlobStore(blob_path),
                clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
                authentication_mode="open",
            )
            connection = FESLConnection()
            first.dispatch(
                FESLFrame.from_fields(
                    "acct",
                    {"TXN": "Login", "name": "PersistentDriver"},
                    transaction=49,
                ),
                connection,
            )
            owner_id = connection.identity.profile_id
            first.dispatch(
                FESLFrame.from_fields(
                    "blob",
                    {
                        "TXN": "AddBlob",
                        "ownerId": str(owner_id),
                        "ownerType": "1",
                        "type": "7",
                        "name": "persisted-shadow",
                        "content": "DATA",
                    },
                    transaction=50,
                ),
                connection,
            )

            second = CarbonFESLService(
                CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
                IdentityStore(),
                blobs=CarbonBlobStore(blob_path),
                authentication_mode="open",
            )
            reply = second.dispatch(
                FESLFrame.from_fields(
                    "blob",
                    {
                        "TXN": "ListBlobInfo",
                        "ownerId": str(owner_id),
                        "ownerType": "1",
                        "type": "7",
                    },
                    transaction=51,
                ),
                FESLConnection(),
            )[0]
            self.assertEqual(reply.fields["blobs.[]"], "1")
            self.assertEqual(reply.fields["blobs.0.name"], "persisted-shadow")

    def test_play_now_reports_no_server_until_theater_has_a_game(self) -> None:
        request = FESLFrame.from_fields("pnow", {"TXN": "Start"}, transaction=12)
        replies = self.service.dispatch(request, self.connection)
        self.assertEqual([reply.fields["TXN"] for reply in replies], ["Start", "Status"])
        self.assertEqual(replies[1].fields["props.{resultType}"], "NOSERVER")

    def test_play_now_creation_is_visible_to_theater_directory(self) -> None:
        identities = IdentityStore(token_factory=lambda: "shared-key.")
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        service = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
            identities,
            games,
            authentication_mode="open",
        )
        connection = FESLConnection()
        service.dispatch(
            FESLFrame.from_fields("acct", {"TXN": "Login", "name": "Host"}, transaction=20),
            connection,
        )
        replies = service.dispatch(
            FESLFrame.from_fields(
                "pnow",
                {"TXN": "Start", "players.0.props.{sessionType}": "resetServer"},
                transaction=21,
            ),
            connection,
        )
        self.assertEqual(replies[1].fields["props.{resultType}"], "JOIN")
        gid = replies[1].fields["props.{games}.0.gid"]
        self.assertEqual(games.list()[0].gid, gid)

    def test_ranked_play_now_preferences_create_a_ranked_room(self) -> None:
        identities = IdentityStore(token_factory=lambda: "ranked-key.")
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        service = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
            identities,
            games,
            authentication_mode="open",
        )
        connection = FESLConnection()
        service.dispatch(
            FESLFrame.from_fields("acct", {"TXN": "Login", "name": "RankedHost"}, transaction=30),
            connection,
        )
        service.dispatch(
            FESLFrame.from_fields(
                "pnow",
                {
                    "TXN": "Start",
                    "players.0.props.{sessionType}": "resetServer",
                    "players.0.props.{filter-matchmaking_state}": "1",
                    "players.0.props.{filter-game_type}": "0",
                    "players.0.props.{pref-game_mode}": "0",
                },
                transaction=31,
            ),
            connection,
        )
        game = games.list()[0]
        self.assertTrue(game.is_ranked)
        self.assertTrue(game.server_hosted)
        self.assertEqual(game.properties["B-U-game_type"], "0")
        self.assertEqual(game.properties["B-U-game_mode"], "0")
        self.assertEqual(game.row()["HN"], "nfsdevserver")
        self.assertEqual(game.row()["HU"], "1")
        self.assertEqual(game.row()["AP"], "0")
        self.assertEqual(game.row()["JP"], "0")
        self.assertEqual(game.row()["QP"], "0")
        self.assertNotIn(connection.identity.user_id, game.participants)
        self.assertNotIn("B-U-location", game.row())
        self.assertEqual(game.row()["B-U-race_type_sprint"], "ct.4.2")
        self.assertEqual(game.row()["B-U-race_type_circuit"], "ex.5.1")
        self.assertEqual(game.row()["B-U-skill"], "500")
        self.assertNotIn("INT-IP", game.row())
        self.assertNotIn("INT-PORT", game.row())
        participant = games.enter(
            game.gid,
            connection.identity,
            internal_ip="192.168.1.9",
            internal_port=1042,
        )
        self.assertIsNotNone(participant)
        self.assertEqual(participant.player_id, 1)
        self.assertEqual(game.row()["AP"], "1")
        self.assertEqual(game.row()["JP"], "0")
        self.assertEqual(game.row()["QP"], "0")

    def test_ranked_find_server_reuses_the_existing_dedicated_room(self) -> None:
        identities = IdentityStore(token_factory=lambda: "ranked-search-key.")
        first, _ = identities.login("RankedOne")
        second, _ = identities.login("RankedTwo")
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        created = games.match_or_create(
            first,
            {
                "players.0.props.{sessionType}": "resetServer",
                "players.0.props.{filter-matchmaking_state}": "1",
                "players.0.props.{filter-game_type}": "0",
                "players.0.props.{pref-game_mode}": "0",
            },
        )
        self.assertIsNotNone(created)
        assert created is not None
        found = games.match_or_create(
            second,
            {
                "players.0.props.{sessionType}": "findServer",
                "players.0.props.{filter-matchmaking_state}": "1",
                "players.0.props.{filter-game_type}": "0|2",
                "players.0.props.{pref-game_mode}": "0",
            },
        )
        self.assertIs(found, created)
        self.assertEqual(len(games.list()), 1)

    def test_ranked_find_does_not_rejoin_the_callers_stale_room(self) -> None:
        identities = IdentityStore(token_factory=lambda: "ranked-reset-key.")
        host, _ = identities.login("RankedHost")
        games = CarbonGameDirectory(Endpoint("127.0.0.1", 19118))
        ranked_fields = {
            "players.0.props.{filter-matchmaking_state}": "1",
            "players.0.props.{filter-game_type}": "0",
            "players.0.props.{pref-game_mode}": "0",
        }
        old_game = games.match_or_create(
            host,
            {**ranked_fields, "players.0.props.{sessionType}": "resetServer"},
        )
        self.assertIsNotNone(old_game)
        assert old_game is not None
        self.assertIsNotNone(games.enter(old_game.gid, host, internal_port=1042))

        found = games.match_or_create(
            host,
            {
                **ranked_fields,
                "players.0.props.{sessionType}": "findServer",
                "players.0.props.{filter-game_type}": "0|2",
            },
        )
        self.assertIsNone(found)

        replacement = games.match_or_create(
            host,
            {**ranked_fields, "players.0.props.{sessionType}": "resetServer"},
        )
        self.assertIsNotNone(replacement)
        assert replacement is not None
        self.assertNotEqual(replacement.gid, old_game.gid)
        self.assertIsNotNone(games.enter(replacement.gid, host, internal_port=1042))
        self.assertIsNone(games.get(old_game.gid))
        self.assertEqual([game.gid for game in games.list()], [replacement.gid])
