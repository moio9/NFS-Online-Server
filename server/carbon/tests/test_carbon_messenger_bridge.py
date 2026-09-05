"""Cross-process Carbon Messenger IPC publisher tests."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from carbon.accounts.identity import IdentityStore, MAX_CARBON_WIRE_PLAYER_ID
from carbon.core.config import Endpoint, ServerSettings
from carbon.messenger_ipc import CarbonMessengerIPCPublisher
from carbon.theater.directory import CarbonGameDirectory


class CarbonMessengerIPCPublisherTests(unittest.TestCase):
    def test_snapshot_contains_live_lkey_wire_id_and_host_room(self) -> None:
        identities = IdentityStore(token_factory=lambda: "driver-key.")
        identity, token = identities.login("driver", "Driver")
        games = CarbonGameDirectory(
            Endpoint("127.0.0.1", 19118),
            player_id_resolver=identities.wire_player_id,
        )
        games.create(identity, {"MAX-PLAYERS": "8"})
        publisher = CarbonMessengerIPCPublisher(
            Endpoint("127.0.0.1", 13506),
            secret="test-secret",
            identities=identities,
            games=games,
            known_identities=lambda: (identity,),
            poll_interval=1,
            heartbeat_interval=1,
        )
        payload = publisher.snapshot()
        self.assertEqual(payload["game"], "carbon")
        self.assertEqual(payload["sessions"][token]["persona"], "Driver")
        self.assertEqual(
            payload["sessions"][token]["wire_player_id"],
            identities.wire_player_id(identity),
        )
        self.assertTrue(payload["rooms"]["driver"]["inviteable"])
        self.assertEqual(payload["rooms"]["driver"]["details"]["MP"], "8")

    def test_snapshot_publishes_duplicate_token_for_native_dupl_notice(self) -> None:
        tokens = iter(("old-key.", "new-key."))
        identities = IdentityStore(token_factory=lambda: next(tokens))
        old_identity, _ = identities.login("driver", "Driver")
        identities.login("driver", "Driver", forced_logoff_reason="DUPL")
        publisher = CarbonMessengerIPCPublisher(
            Endpoint("127.0.0.1", 13506),
            secret="test-secret",
            identities=identities,
            games=CarbonGameDirectory(
                Endpoint("127.0.0.1", 19118),
                player_id_resolver=identities.wire_player_id,
            ),
        )

        payload = publisher.snapshot()

        self.assertNotIn("old-key.", payload["sessions"])
        self.assertIn("new-key.", payload["sessions"])
        self.assertEqual(payload["forced_logoffs"]["old-key."]["reason"], "DUPL")
        self.assertEqual(
            payload["forced_logoffs"]["old-key."]["persona"],
            old_identity.persona,
        )
        self.assertFalse(
            payload["forced_logoffs"]["old-key."]["theater_ready"]
        )
        self.assertTrue(
            identities.mark_forced_logoff_theater_ready("old-key.")
        )
        self.assertTrue(
            publisher.snapshot()["forced_logoffs"]["old-key."]["theater_ready"]
        )

    def test_shared_messenger_config_uses_loopback_ipc(self) -> None:
        with TemporaryDirectory() as temporary:
            config = Path(temporary) / "server.cfg"
            config.write_text(
                "\n".join(
                    (
                        "GAME=carbon",
                        "MESSENGER_IPC=127.0.0.1:14506",
                        "MESSENGER_IPC_SECRET=test-secret",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            settings = ServerSettings.load(config)
            self.assertEqual(settings.messenger_ipc_endpoint, Endpoint("127.0.0.1", 14506))
            self.assertEqual(settings.messenger_ipc_secret, "test-secret")

    def test_shared_messenger_config_rejects_non_loopback_ipc(self) -> None:
        with TemporaryDirectory() as temporary:
            config = Path(temporary) / "server.cfg"
            config.write_text(
                "\n".join(
                    (
                        "GAME=carbon",
                        "MESSENGER_IPC=0.0.0.0:13506",
                        "MESSENGER_IPC_SECRET=test-secret",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "loopback"):
                ServerSettings.load(config)

    def test_legacy_internal_messenger_mode_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            config = Path(temporary) / "server.cfg"
            config.write_text(
                "GAME=carbon\nMESSENGER_MODE=internal\n"
                "MESSENGER_IPC_SECRET=test-secret\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "shared EA Messenger"):
                ServerSettings.load(config)

    def test_wire_player_ids_are_stable_positive_15_bit_values(self) -> None:
        identities = IdentityStore()
        driver, _ = identities.login("driver", "Driver")
        other, _ = identities.login("other", "OtherDriver")
        first = identities.wire_player_id(driver)
        self.assertEqual(first, identities.wire_player_id("driver"))
        self.assertNotEqual(first, identities.wire_player_id(other))
        self.assertTrue(0 < first <= MAX_CARBON_WIRE_PLAYER_ID)

    def test_wire_player_id_preserves_confirmed_decompilation_vectors(self) -> None:
        identities = IdentityStore()
        self.assertEqual(identities.wire_player_id("test"), 0x4622)
        self.assertEqual(identities.wire_player_id("player"), 0x7976)
        self.assertEqual(identities.wire_player_id("testdriver"), 0x0C7B)


if __name__ == "__main__":
    unittest.main()
