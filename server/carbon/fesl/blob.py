"""Small persistent EA Blob service backing Carbon's uploaded race shadows."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import json
from pathlib import Path
from threading import RLock
from typing import Mapping


class CarbonBlobQuotaError(ValueError):
    """Raised when an owner attempts to exceed the configured blob quota."""


@dataclass
class CarbonBlob:
    blob_id: int
    owner_id: int
    owner_type: int
    blob_type: int
    format_type: int = 0
    icon_id: int = 0
    create_date: str = ""
    update_date: str = ""
    creator: str = ""
    name: str = ""
    download_count: int = 0
    rating: float = 0.0
    review_count: int = 0
    version: str = ""
    short_description: str = ""
    long_description: str = ""
    locale: str = ""
    content: str = ""
    attributes: list[tuple[str, int, str]] = field(default_factory=list)

    @property
    def encoded_size(self) -> int:
        return len(self.content.encode("latin-1", errors="replace"))

    @property
    def unencoded_size(self) -> int:
        encoded = self.content.encode("latin-1", errors="replace")
        try:
            return len(base64.b64decode(encoded, validate=True))
        except (binascii.Error, ValueError):
            # Preserve compatibility with old/non-Base64 records while making
            # valid race-shadow metadata match the retail Blob contract.
            return len(encoded)

    def to_json(self) -> dict[str, object]:
        return {
            "blob_id": self.blob_id,
            "owner_id": self.owner_id,
            "owner_type": self.owner_type,
            "type": self.blob_type,
            "format_type": self.format_type,
            "icon_id": self.icon_id,
            "create_date": self.create_date,
            "update_date": self.update_date,
            "creator": self.creator,
            "name": self.name,
            "download_count": self.download_count,
            "rating": self.rating,
            "review_count": self.review_count,
            "version": self.version,
            "short_description": self.short_description,
            "long_description": self.long_description,
            "locale": self.locale,
            "content": self.content,
            "attributes": [
                {"name": name, "type": kind, "value": value}
                for name, kind, value in self.attributes
            ],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> "CarbonBlob":
        attributes: list[tuple[str, int, str]] = []
        raw_attributes = value.get("attributes", [])
        if isinstance(raw_attributes, list):
            for item in raw_attributes:
                if not isinstance(item, dict):
                    continue
                attributes.append(
                    (
                        str(item.get("name", "") or ""),
                        _safe_int(item.get("type", 0)),
                        str(item.get("value", "") or ""),
                    )
                )
        return cls(
            blob_id=_safe_int(value.get("blob_id")),
            owner_id=_safe_int(value.get("owner_id")),
            owner_type=_safe_int(value.get("owner_type"), 1),
            blob_type=_safe_int(value.get("type")),
            format_type=_safe_int(value.get("format_type")),
            icon_id=_safe_int(value.get("icon_id")),
            create_date=str(value.get("create_date", "") or ""),
            update_date=str(value.get("update_date", "") or ""),
            creator=str(value.get("creator", "") or ""),
            name=str(value.get("name", "") or ""),
            download_count=_safe_int(value.get("download_count")),
            rating=_safe_float(value.get("rating")),
            review_count=_safe_int(value.get("review_count")),
            version=str(value.get("version", "") or ""),
            short_description=str(value.get("short_description", "") or ""),
            long_description=str(value.get("long_description", "") or ""),
            locale=str(value.get("locale", "") or ""),
            content=str(value.get("content", "") or ""),
            attributes=attributes,
        )


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value if value is not None else default))
    except (TypeError, ValueError):
        return int(default)


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value if value is not None else default))
    except (TypeError, ValueError):
        return float(default)


class CarbonBlobStore:
    """Thread-safe blob metadata/content store with optional JSON persistence."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = RLock()
        self._blobs: dict[int, CarbonBlob] = {}
        self._next_blob_id = 1
        if self.path is not None:
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid Carbon blob data {self.path}: {exc}") from exc
        entries = raw.get("blobs", []) if isinstance(raw, dict) else []
        if not isinstance(entries, list):
            raise ValueError(f"invalid Carbon blob data {self.path}: blobs must be an array")
        if isinstance(raw, dict):
            self._next_blob_id = max(
                self._next_blob_id,
                _safe_int(raw.get("next_blob_id"), 1),
            )
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            blob = CarbonBlob.from_json(entry)
            if blob.blob_id <= 0:
                continue
            self._blobs[blob.blob_id] = blob
            self._next_blob_id = max(self._next_blob_id, blob.blob_id + 1)

    def _save_locked(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "next_blob_id": self._next_blob_id,
            "blobs": [
                blob.to_json()
                for _blob_id, blob in sorted(self._blobs.items())
            ],
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def add(
        self,
        blob: CarbonBlob,
        *,
        max_owner_blobs: int | None = None,
    ) -> CarbonBlob:
        with self._lock:
            if max_owner_blobs is not None:
                limit = max(0, int(max_owner_blobs))
                owner_count = sum(
                    candidate.owner_id == blob.owner_id
                    for candidate in self._blobs.values()
                )
                if owner_count >= limit:
                    raise CarbonBlobQuotaError(
                        f"owner {blob.owner_id} reached blob quota {limit}"
                    )
            blob.blob_id = self._next_blob_id
            self._next_blob_id += 1
            self._blobs[blob.blob_id] = blob
            self._save_locked()
            return blob

    def get(self, blob_id: int) -> CarbonBlob | None:
        with self._lock:
            return self._blobs.get(int(blob_id))

    def remove(self, blob_id: int) -> bool:
        with self._lock:
            removed = self._blobs.pop(int(blob_id), None) is not None
            if removed:
                self._save_locked()
            return removed

    def remove_owned(self, blob_id: int, owner_id: int) -> bool:
        """Remove a blob only when it still belongs to the authenticated owner."""
        with self._lock:
            blob = self._blobs.get(int(blob_id))
            if blob is None or blob.owner_id != int(owner_id):
                return False
            del self._blobs[int(blob_id)]
            self._save_locked()
            return True

    def update(self, blob_id: int, **changes: object) -> CarbonBlob | None:
        with self._lock:
            blob = self._blobs.get(int(blob_id))
            if blob is None:
                return None
            for name, value in changes.items():
                if hasattr(blob, name):
                    setattr(blob, name, value)
            self._save_locked()
            return blob

    def update_owned(
        self,
        blob_id: int,
        owner_id: int,
        **changes: object,
    ) -> CarbonBlob | None:
        """Update a blob only when it still belongs to the authenticated owner."""
        with self._lock:
            blob = self._blobs.get(int(blob_id))
            if blob is None or blob.owner_id != int(owner_id):
                return None
            for name, value in changes.items():
                if hasattr(blob, name):
                    setattr(blob, name, value)
            self._save_locked()
            return blob

    def search(
        self,
        *,
        owner_id: int | None = None,
        owner_type: int | None = None,
        blob_type: int | None = None,
        name: str = "",
        name_case_sensitive: bool = False,
        name_wildcard: bool = False,
        max_records: int = 20_000,
        attributes: tuple[tuple[str, str], ...] = (),
    ) -> list[CarbonBlob]:
        with self._lock:
            rows = list(self._blobs.values())
        if owner_id is not None:
            rows = [blob for blob in rows if blob.owner_id == int(owner_id)]
        if owner_type is not None:
            rows = [blob for blob in rows if blob.owner_type == int(owner_type)]
        if blob_type is not None:
            rows = [blob for blob in rows if blob.blob_type == int(blob_type)]
        if name:
            requested = name if name_case_sensitive else name.casefold()

            def matches_name(blob: CarbonBlob) -> bool:
                candidate = blob.name if name_case_sensitive else blob.name.casefold()
                return (
                    fnmatchcase(candidate, requested)
                    if name_wildcard
                    else candidate == requested
                )

            rows = [blob for blob in rows if matches_name(blob)]
        for attribute_name, attribute_value in attributes:
            rows = [
                blob
                for blob in rows
                if any(
                    name == attribute_name and value == attribute_value
                    for name, _kind, value in blob.attributes
                )
            ]
        rows.sort(key=lambda blob: blob.blob_id)
        return rows[: max(0, int(max_records))]
