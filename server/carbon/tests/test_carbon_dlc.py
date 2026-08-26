"""Catalog, assignment and DOBJ serialization tests for Carbon DLC."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from carbon.accounts.identity import Identity
from carbon.dlc import (
    CarbonDLCAssignments,
    CarbonDLCCatalog,
    CarbonDLCConfigError,
    CarbonDLCInventory,
)
from carbon.fesl.frame import FESLFrame, packetize_frame


class CarbonDLCCatalogTests(unittest.TestCase):
    def test_repository_catalog_contains_every_upstream_example(self) -> None:
        catalog = CarbonDLCCatalog.from_path(
            "../../data/carbon/dlc_catalog.json"
        )
        self.assertEqual(len(catalog.groups), 51)
        self.assertEqual(len(catalog.all_tokens()), 2793)
        self.assertEqual(
            catalog.groups["virus_vinyls"].tokens,
            (
                "VIRUS_KNOCKOUT_FEVER",
                "VIRUS_CANYON_CRAZE",
                "VIRUS_PURSUIT_PANDEMIC",
                "VIRUS_CARBON_PLAGUE",
            ),
        )
        self.assertEqual(
            catalog.groups["2006_infiniti_g35"].tokens,
            ("g35",),
        )
        self.assertIn(
            "DLC_CE_TRACKS",
            catalog.groups["collector_s_edition_upgrade"].tokens,
        )

    def test_group_preset_raw_token_and_exclusion_selectors(self) -> None:
        catalog = CarbonDLCCatalog.from_path(
            "../../data/carbon/dlc_catalog.json"
        )
        tokens = catalog.expand(
            (
                "default_dlc",
                "token:EXPERIMENTAL_UNLOCK",
                "-virus_vinyls",
            )
        )
        self.assertIn("997tt", tokens)
        self.assertIn("EXPERIMENTAL_UNLOCK", tokens)
        self.assertNotIn("VIRUS_PURSUIT_PANDEMIC", tokens)
        self.assertEqual(len(tokens), len(set(tokens)))

    def test_unknown_assignment_selector_is_rejected_at_startup(self) -> None:
        catalog = CarbonDLCCatalog.from_path(
            "../../data/carbon/dlc_catalog.json"
        )
        assignments = CarbonDLCAssignments(
            default=("not_a_real_dlc",),
            accounts={},
            personas={},
        )
        inventory = CarbonDLCInventory(catalog, assignments)
        with self.assertRaisesRegex(CarbonDLCConfigError, "unknown"):
            inventory.validate_assignments()

    def test_preset_cycles_are_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "groups": {
                            "one": {
                                "label": "One",
                                "category": "test",
                                "tokens": ["ONE"],
                            }
                        },
                        "presets": {"a": ["b"], "b": ["a"]},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CarbonDLCConfigError, "cycle"):
                CarbonDLCCatalog.from_path(path)


class CarbonDLCInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inventory = CarbonDLCInventory.from_paths(
            "../../data/carbon/dlc_catalog.json",
            "../../data/carbon/dlc_assignments.json",
        )

    def test_account_override_and_default_selection(self) -> None:
        moio = Identity("moio", "moio", 1, 1)
        other = Identity("Driver", "Driver", 2, 2)
        self.assertEqual(len(self.inventory.tokens_for(moio)), 6)
        self.assertEqual(len(self.inventory.tokens_for(other)), 6)
        self.assertEqual(len(self.inventory.tokens_for(None)), 6)
        self.assertFalse(
            any(token.startswith("VIRUS_") for token in self.inventory.tokens_for(other))
        )

    def test_inventory_fields_have_stable_unique_entitlement_ids(self) -> None:
        identity = Identity("Driver", "Driver", 2, 2)
        first = self.inventory.fields_for(identity)
        second = self.inventory.fields_for(identity)
        self.assertEqual(first, second)
        count = int(first["entitlements.[]"])
        self.assertEqual(count, 6)
        ids = [first[f"entitlements.{index}.entitleId"] for index in range(count)]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(value.isdigit() and int(value) > 0 for value in ids))
        self.assertTrue(
            all(
                first[f"entitlements.{index}.dateEntitled"]
                == "Jan-1-2007 0:00:00 UTC"
                for index in range(count)
            )
        )

    def test_persistent_viral_tokens_merge_without_duplicates(self) -> None:
        identity = Identity("Driver", "Driver", 2, 2)
        tokens = self.inventory.tokens_for(
            identity,
            (
                "VIRUS_KNOCKOUT_FEVER",
                "VIRUS_KNOCKOUT_FEVER",
                "VIRUS_CANYON_CRAZE",
            ),
        )
        self.assertEqual(len(tokens), 8)
        self.assertEqual(tokens.count("VIRUS_KNOCKOUT_FEVER"), 1)
        fields = self.inventory.fields_for(identity, tokens[-2:])
        self.assertEqual(fields["entitlements.[]"], "8")

    def test_all_inventory_is_fragmentable_with_retail_envelope(self) -> None:
        identity = Identity("moio", "moio", 1, 1)
        inventory = CarbonDLCInventory(
            self.inventory.catalog,
            CarbonDLCAssignments(
                default=("default_dlc",),
                accounts={"moio": ("all",)},
                personas={},
            ),
        )
        frame = FESLFrame.from_fields(
            "dobj",
            inventory.fields_for(identity),
            transaction=0x80000004,
        )
        fragments = packetize_frame(frame)
        self.assertGreater(len(fragments), 1)
        self.assertTrue(all(len(fragment.encode()) < 8192 for fragment in fragments))
        self.assertTrue(all(fragment.command == "dobj" for fragment in fragments))

    def test_missing_assignment_file_is_initialized_without_account_data(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path = root / "catalog.json"
            assignments_path = root / "assignments.json"
            catalog_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "groups": {
                            "base": {
                                "label": "Base",
                                "category": "cars",
                                "tokens": ["BASE"],
                            }
                        },
                        "presets": {"default_dlc": ["base"]},
                    }
                ),
                encoding="utf-8",
            )

            inventory = CarbonDLCInventory.from_paths(catalog_path, assignments_path)

            self.assertEqual(inventory.tokens_for(None), ("BASE",))
            document = json.loads(assignments_path.read_text(encoding="utf-8"))
            self.assertEqual(document["default"], ["default_dlc"])
            self.assertEqual(document["accounts"], {})
            self.assertEqual(document["personas"], {})

    def test_assignment_store_writes_atomically_and_hot_reloads_other_inventory(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path = root / "catalog.json"
            assignments_path = root / "assignments.json"
            catalog_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "groups": {
                            "base": {"label": "Base", "category": "cars", "tokens": ["BASE"]},
                            "bonus": {"label": "Bonus", "category": "cars", "tokens": ["BONUS"]},
                        },
                        "presets": {"default_dlc": ["base"]},
                    }
                ),
                encoding="utf-8",
            )
            assignments_path.write_text(
                json.dumps({"version": 1, "default": ["default_dlc"], "accounts": {}, "personas": {}}),
                encoding="utf-8",
            )
            writer = CarbonDLCInventory.from_paths(catalog_path, assignments_path)
            reader = CarbonDLCInventory.from_paths(catalog_path, assignments_path)
            assert writer.assignment_store is not None

            identity = Identity("Driver", "Driver", 2, 2)
            self.assertEqual(reader.tokens_for(identity), ("BASE",))
            writer.assignment_store.set_account("Driver", ("bonus",))
            self.assertEqual(reader.tokens_for(identity), ("BONUS",))
            writer.assignment_store.set_account("Driver", ("all",))
            self.assertEqual(reader.tokens_for(identity), ("BASE", "BONUS"))
            writer.assignment_store.reset_account("Driver")
            self.assertEqual(reader.tokens_for(identity), ("BASE",))
            self.assertFalse((assignments_path.with_name(assignments_path.name + ".lock")).exists())

    def test_account_assignment_can_explicitly_disable_inventory(self) -> None:
        assignments = CarbonDLCAssignments(
            default=("default_dlc",),
            accounts={"driver": ("none",)},
            personas={},
        )
        inventory = CarbonDLCInventory(self.inventory.catalog, assignments)
        fields = inventory.fields_for(Identity("Driver", "Driver", 2, 2))
        self.assertEqual(fields, {"TXN": "GetObjectInventory", "entitlements.[]": "0"})


if __name__ == "__main__":
    unittest.main()
