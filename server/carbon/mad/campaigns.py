"""Configurable Massive Ads campaigns and deterministic zone rotation."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
import json
from pathlib import Path
import re
from typing import Mapping


_LAYOUTS = ("horizontal", "vertical", "panoramic")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_EXPECTED_DDS = {
    "horizontal": (256, 128),
    "vertical": (128, 256),
    "panoramic": (512, 128),
}


class MADCampaignError(ValueError):
    """Raised when a MAD campaign file is invalid or unsafe."""


@dataclass(frozen=True)
class MADCreativeSet:
    campaign_id: str
    zone_patterns: tuple[str, ...]
    assets: Mapping[str, bytes]
    source_paths: Mapping[str, Path]

    def matches(self, zone: str) -> bool:
        normalized = zone.casefold()
        return any(fnmatchcase(normalized, pattern.casefold()) for pattern in self.zone_patterns)


@dataclass(frozen=True)
class MADCampaignSelection:
    campaign: MADCreativeSet
    rotation_slot: int


class MADCampaignCatalog:
    """Load and select validated DXT1 campaigns for a Carbon MAD zone."""

    def __init__(
        self,
        campaigns: tuple[MADCreativeSet, ...],
        *,
        rotation_seconds: int,
    ) -> None:
        if not campaigns:
            raise MADCampaignError("MAD campaign catalog is empty")
        if rotation_seconds < 0:
            raise MADCampaignError("MAD rotation_seconds must be zero or positive")
        identifiers = [campaign.campaign_id for campaign in campaigns]
        if len(set(identifiers)) != len(identifiers):
            raise MADCampaignError("MAD campaign ids contain duplicates")
        self.campaigns = campaigns
        self.rotation_seconds = rotation_seconds

    @classmethod
    def built_in(
        cls,
        assets: Mapping[str, bytes],
        source_paths: Mapping[str, Path],
        *,
        rotation_seconds: int = 300,
    ) -> "MADCampaignCatalog":
        _validate_asset_set(assets)
        return cls(
            (
                MADCreativeSet(
                    campaign_id="default",
                    zone_patterns=("*",),
                    assets=dict(assets),
                    source_paths=dict(source_paths),
                ),
            ),
            rotation_seconds=rotation_seconds,
        )

    @classmethod
    def load(
        cls,
        path: str | Path | None,
        *,
        fallback_assets: Mapping[str, bytes],
        fallback_paths: Mapping[str, Path],
        default_rotation_seconds: int = 300,
    ) -> "MADCampaignCatalog":
        if path is None:
            return cls.built_in(
                fallback_assets,
                fallback_paths,
                rotation_seconds=default_rotation_seconds,
            )
        config_path = Path(path)
        if not config_path.exists():
            return cls.built_in(
                fallback_assets,
                fallback_paths,
                rotation_seconds=default_rotation_seconds,
            )
        try:
            document = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MADCampaignError(f"cannot read MAD campaign file {config_path}: {exc}") from exc
        if not isinstance(document, dict):
            raise MADCampaignError("MAD campaign root must be an object")
        version = document.get("version", 1)
        if version != 1:
            raise MADCampaignError(f"unsupported MAD campaign version {version!r}")
        try:
            rotation_seconds = int(
                document.get("rotation_seconds", default_rotation_seconds)
            )
        except (TypeError, ValueError) as exc:
            raise MADCampaignError("MAD rotation_seconds must be an integer") from exc
        raw_campaigns = document.get("campaigns")
        if not isinstance(raw_campaigns, list) or not raw_campaigns:
            raise MADCampaignError("MAD campaigns must be a non-empty array")

        campaigns: list[MADCreativeSet] = []
        for index, raw in enumerate(raw_campaigns):
            if not isinstance(raw, dict):
                raise MADCampaignError(f"MAD campaign #{index} must be an object")
            if raw.get("enabled", True) is False:
                continue
            campaign_id = str(raw.get("id", "")).strip()
            if not _SAFE_ID.fullmatch(campaign_id):
                raise MADCampaignError(
                    f"MAD campaign #{index} has invalid id {campaign_id!r}"
                )
            raw_zones = raw.get("zones", ["*"])
            if not isinstance(raw_zones, list) or not raw_zones:
                raise MADCampaignError(
                    f"MAD campaign {campaign_id!r} zones must be a non-empty array"
                )
            zone_patterns = tuple(str(value).strip() for value in raw_zones)
            if any(not value or len(value) > 128 for value in zone_patterns):
                raise MADCampaignError(
                    f"MAD campaign {campaign_id!r} contains an invalid zone pattern"
                )
            raw_assets = raw.get("assets")
            if not isinstance(raw_assets, dict):
                raise MADCampaignError(
                    f"MAD campaign {campaign_id!r} assets must be an object"
                )
            assets: dict[str, bytes] = {}
            source_paths: dict[str, Path] = {}
            for layout in _LAYOUTS:
                raw_path = raw_assets.get(layout)
                if not isinstance(raw_path, str) or not raw_path.strip():
                    raise MADCampaignError(
                        f"MAD campaign {campaign_id!r} is missing {layout!r}"
                    )
                source = Path(raw_path)
                if not source.is_absolute():
                    source = (config_path.parent / source).resolve()
                try:
                    body = source.read_bytes()
                except OSError as exc:
                    raise MADCampaignError(
                        f"cannot read MAD {layout} asset {source}: {exc}"
                    ) from exc
                assets[layout] = body
                source_paths[layout] = source
            _validate_asset_set(assets)
            campaigns.append(
                MADCreativeSet(
                    campaign_id=campaign_id,
                    zone_patterns=zone_patterns,
                    assets=assets,
                    source_paths=source_paths,
                )
            )
        if not campaigns:
            raise MADCampaignError("all MAD campaigns are disabled")
        return cls(tuple(campaigns), rotation_seconds=rotation_seconds)

    def select(self, zone: str, *, unix_time: float) -> MADCampaignSelection:
        specific_matches = tuple(
            campaign
            for campaign in self.campaigns
            if campaign.matches(zone) and "*" not in campaign.zone_patterns
        )
        matches = specific_matches
        if not matches:
            matches = tuple(
                campaign for campaign in self.campaigns if "*" in campaign.zone_patterns
            )
        if not matches:
            matches = tuple(campaign for campaign in self.campaigns if campaign.matches(zone))
        if not matches:
            matches = (self.campaigns[0],)
        slot = 0
        if self.rotation_seconds > 0:
            slot = int(unix_time // self.rotation_seconds)
        # Offset each zone deterministically so simultaneous zones do not all rotate
        # to the same creative when multiple campaigns match.
        zone_offset = sum(zone.encode("utf-8", errors="ignore"))
        campaign = matches[(slot + zone_offset) % len(matches)]
        return MADCampaignSelection(campaign=campaign, rotation_slot=slot)


def _validate_asset_set(assets: Mapping[str, bytes]) -> None:
    for layout in _LAYOUTS:
        if layout not in assets:
            raise MADCampaignError(f"MAD asset set is missing {layout!r}")
        _validate_dds(assets[layout], layout=layout)


def _validate_dds(body: bytes, *, layout: str) -> None:
    if len(body) < 128 or body[:4] != b"DDS ":
        raise MADCampaignError(f"MAD {layout} creative is not a DDS file")
    height = int.from_bytes(body[12:16], "little")
    width = int.from_bytes(body[16:20], "little")
    expected_width, expected_height = _EXPECTED_DDS[layout]
    if (width, height) != (expected_width, expected_height):
        raise MADCampaignError(
            f"MAD {layout} creative must be {expected_width}x{expected_height}, "
            f"got {width}x{height}"
        )
    if body[84:88] != b"DXT1":
        raise MADCampaignError(f"MAD {layout} creative must use DXT1 compression")
