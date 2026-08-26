"""Filesystem-backed Carbon Blob store indexed by the shared account SQLite DB."""

from __future__ import annotations

import base64
import binascii
from fnmatch import fnmatchcase
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from common.accounts import SQLiteAccountDatabase, SharedIdentityRecord
log = logging.getLogger(__name__)

from carbon.fesl.blob import (
    CarbonBlob,
    CarbonBlobQuotaError,
)


class SQLiteCarbonBlobStore:
    """Store Blob metadata in SQLite and payload bytes under the owner folder.

    Valid Base64 payloads are decoded to their native bytes on disk.  The wire
    API still returns the original Base64 representation, so the retail client
    observes the same contract while backups no longer contain a huge JSON
    string for every shadow.
    """

    def __init__(self, database: SQLiteAccountDatabase) -> None:
        self.database = database
        self.path = database.path
        self._reconcile_filesystem()

    def _quarantine_orphan(self, path: Path) -> None:
        root = self.database.user_root.parent / "backups" / "orphaned-carbon-assets"
        root.mkdir(parents=True, exist_ok=True)
        destination = root / f"{path.stem}-{uuid4().hex}{path.suffix}"
        os.replace(path, destination)
        log.warning("quarantined unindexed Carbon Blob payload: %s", destination)

    def _reconcile_filesystem(self) -> None:
        """Repair crash residue between SQLite commits and filesystem renames."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT asset_id, relative_path, byte_size, sha256 "
                "FROM assets WHERE game='carbon'"
            ).fetchall()

        referenced: set[Path] = set()
        missing_ids: list[int] = []
        repaired: list[tuple[int, str, int]] = []
        for row in rows:
            path = self._safe_payload_path(str(row["relative_path"]))
            referenced.add(path.resolve())
            if not path.is_file():
                missing_ids.append(int(row["asset_id"]))
                continue
            payload = path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            if int(row["byte_size"]) != len(payload) or str(row["sha256"]) != digest:
                repaired.append((len(payload), digest, int(row["asset_id"])))

        if missing_ids or repaired:
            with self.database.transaction() as connection:
                if missing_ids:
                    connection.executemany(
                        "DELETE FROM assets WHERE asset_id=? AND game='carbon'",
                        ((asset_id,) for asset_id in missing_ids),
                    )
                if repaired:
                    connection.executemany(
                        "UPDATE assets SET byte_size=?, sha256=? WHERE asset_id=? AND game='carbon'",
                        repaired,
                    )
            if missing_ids:
                log.warning(
                    "removed %d Carbon Blob metadata rows with missing payloads",
                    len(missing_ids),
                )
            if repaired:
                log.info("reconciled %d Carbon Blob size/checksum rows", len(repaired))

        # Temp files are never authoritative; completed but unindexed payloads
        # are preserved under backups instead of being deleted.
        for temporary in self.database.user_root.glob(
            "*/*/personas/*/carbon/*/*.bin.tmp"
        ):
            temporary.unlink(missing_ok=True)
        for payload_path in self.database.user_root.glob(
            "*/*/personas/*/carbon/*/*.bin"
        ):
            if payload_path.resolve() not in referenced:
                self._quarantine_orphan(payload_path)

    @staticmethod
    def _category(blob: CarbonBlob) -> str:
        marker = " ".join(
            (
                blob.name,
                blob.short_description,
                blob.long_description,
                *(name for name, _kind, _value in blob.attributes),
            )
        ).casefold()
        if "shadow" in marker or "ghost" in marker:
            return "shadows"
        if "photo" in marker or "image" in marker or "screenshot" in marker:
            return "photos"
        return "blobs"

    @staticmethod
    def _encode_payload(content: str) -> tuple[bytes, str]:
        encoded = str(content or "").encode("latin-1", errors="replace")
        try:
            return base64.b64decode(encoded, validate=True), "base64"
        except (binascii.Error, ValueError):
            return encoded, "latin1"

    @staticmethod
    def _decode_payload(payload: bytes, encoding: str) -> str:
        if encoding == "base64":
            return base64.b64encode(payload).decode("ascii")
        return payload.decode("latin-1", errors="replace")

    @staticmethod
    def _metadata(blob: CarbonBlob, *, content_encoding: str) -> dict[str, object]:
        value = blob.to_json()
        value.pop("content", None)
        value["content_encoding"] = content_encoding
        return value

    def _safe_payload_path(self, relative_path: str) -> Path:
        root = self.database.user_root.resolve()
        path = (self.database.user_root / str(relative_path)).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("asset path escapes ACCOUNT_FILES") from exc
        return path

    def _identity_for_blob(self, blob: CarbonBlob) -> SharedIdentityRecord:
        identity = self.database.identity_for_profile(blob.owner_id)
        if identity is None:
            raise ValueError(f"unknown Blob owner profile {blob.owner_id}")
        return identity

    def _row(self, blob_id: int):
        with self.database.connect() as connection:
            return connection.execute(
                """
                SELECT a.*, p.profile_id
                  FROM assets AS a
                  JOIN personas AS p ON p.persona_id=a.persona_id
                 WHERE a.game='carbon' AND a.wire_id=?
                """,
                (int(blob_id),),
            ).fetchone()

    def _blob_from_row(self, row) -> CarbonBlob:
        metadata = json.loads(str(row["metadata_json"] or "{}"))
        if not isinstance(metadata, Mapping):
            metadata = {}
        blob = CarbonBlob.from_json(metadata)
        blob.blob_id = int(row["wire_id"])
        blob.owner_id = int(row["profile_id"])
        path = self._safe_payload_path(str(row["relative_path"]))
        payload = path.read_bytes() if path.exists() else b""
        blob.content = self._decode_payload(
            payload,
            str(metadata.get("content_encoding", "latin1")),
        )
        return blob

    def add(
        self,
        blob: CarbonBlob,
        *,
        max_owner_blobs: int | None = None,
    ) -> CarbonBlob:
        identity = self._identity_for_blob(blob)
        category = self._category(blob)
        payload, encoding = self._encode_payload(blob.content)
        now = float(self.database._clock())
        asset_uuid = uuid4().hex
        final_path: Path | None = None
        temporary_path: Path | None = None

        try:
            with self.database.transaction() as connection:
                if max_owner_blobs is not None:
                    count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM assets WHERE persona_id=? AND game='carbon'",
                            (identity.persona_id,),
                        ).fetchone()[0]
                    )
                    if count >= max(0, int(max_owner_blobs)):
                        raise CarbonBlobQuotaError(
                            f"owner {blob.owner_id} reached blob quota {max_owner_blobs}"
                        )
                cursor = connection.execute(
                    """
                    INSERT INTO assets(
                        asset_uuid, persona_id, game, kind, wire_id,
                        relative_path, byte_size, sha256, metadata_json,
                        created_at, updated_at
                    ) VALUES(?, ?, 'carbon', ?, NULL, ?, 0, '', '{}', ?, ?)
                    """,
                    (
                        asset_uuid,
                        identity.persona_id,
                        category[:-1] if category.endswith("s") else category,
                        f"pending/{asset_uuid}",
                        now,
                        now,
                    ),
                )
                blob_id = int(cursor.lastrowid)
                blob.blob_id = blob_id
                directory = self.database.persona_directory(identity, "carbon") / category
                directory.mkdir(parents=True, exist_ok=True)
                final_path = directory / f"{blob_id:08d}-{asset_uuid}.bin"
                temporary_path = final_path.with_suffix(final_path.suffix + ".tmp")
                temporary_path.write_bytes(payload)
                os.replace(temporary_path, final_path)
                relative_path = final_path.relative_to(self.database.user_root).as_posix()
                sha256 = hashlib.sha256(payload).hexdigest()
                metadata = json.dumps(
                    self._metadata(blob, content_encoding=encoding),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                connection.execute(
                    """
                    UPDATE assets
                       SET wire_id=?, relative_path=?, byte_size=?, sha256=?,
                           metadata_json=?, updated_at=?
                     WHERE asset_id=?
                    """,
                    (
                        blob_id,
                        relative_path,
                        len(payload),
                        sha256,
                        metadata,
                        now,
                        blob_id,
                    ),
                )
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            if final_path is not None:
                final_path.unlink(missing_ok=True)
            raise
        return blob

    def get(self, blob_id: int) -> CarbonBlob | None:
        row = self._row(blob_id)
        return self._blob_from_row(row) if row is not None else None

    def remove(self, blob_id: int) -> bool:
        row = self._row(blob_id)
        if row is None:
            return False
        path = self._safe_payload_path(str(row["relative_path"]))
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM assets WHERE game='carbon' AND wire_id=?",
                (int(blob_id),),
            )
        if cursor.rowcount > 0:
            path.unlink(missing_ok=True)
            return True
        return False

    def remove_owned(self, blob_id: int, owner_id: int) -> bool:
        row = self._row(blob_id)
        if row is None or int(row["profile_id"]) != int(owner_id):
            return False
        return self.remove(blob_id)

    def update(self, blob_id: int, **changes: object) -> CarbonBlob | None:
        row = self._row(blob_id)
        if row is None:
            return None
        blob = self._blob_from_row(row)
        for name, value in changes.items():
            if hasattr(blob, name):
                setattr(blob, name, value)

        path = self._safe_payload_path(str(row["relative_path"]))
        metadata_old = json.loads(str(row["metadata_json"] or "{}"))
        encoding = str(metadata_old.get("content_encoding", "latin1"))
        payload_changed = "content" in changes
        if payload_changed:
            payload, encoding = self._encode_payload(blob.content)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(payload)
            os.replace(temporary, path)
        else:
            payload = path.read_bytes() if path.exists() else b""

        now = float(self.database._clock())
        metadata = json.dumps(
            self._metadata(blob, content_encoding=encoding),
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE assets
                   SET byte_size=?, sha256=?, metadata_json=?, updated_at=?
                 WHERE game='carbon' AND wire_id=?
                """,
                (
                    len(payload),
                    hashlib.sha256(payload).hexdigest(),
                    metadata,
                    now,
                    int(blob_id),
                ),
            )
        return blob

    def update_owned(
        self,
        blob_id: int,
        owner_id: int,
        **changes: object,
    ) -> CarbonBlob | None:
        row = self._row(blob_id)
        if row is None or int(row["profile_id"]) != int(owner_id):
            return None
        return self.update(blob_id, **changes)

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
        clauses = ["a.game='carbon'"]
        parameters: list[object] = []
        if owner_id is not None:
            clauses.append("p.profile_id=?")
            parameters.append(int(owner_id))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT a.*, p.profile_id
                  FROM assets AS a
                  JOIN personas AS p ON p.persona_id=a.persona_id
                 WHERE {' AND '.join(clauses)}
                 ORDER BY a.wire_id
                """,
                tuple(parameters),
            ).fetchall()
        blobs = [self._blob_from_row(row) for row in rows]
        if owner_type is not None:
            blobs = [blob for blob in blobs if blob.owner_type == int(owner_type)]
        if blob_type is not None:
            blobs = [blob for blob in blobs if blob.blob_type == int(blob_type)]
        if name:
            requested = name if name_case_sensitive else name.casefold()

            def matches(candidate_blob: CarbonBlob) -> bool:
                candidate = candidate_blob.name if name_case_sensitive else candidate_blob.name.casefold()
                return fnmatchcase(candidate, requested) if name_wildcard else candidate == requested

            blobs = [blob for blob in blobs if matches(blob)]
        for attribute_name, attribute_value in attributes:
            blobs = [
                blob
                for blob in blobs
                if any(
                    item_name == attribute_name and item_value == attribute_value
                    for item_name, _kind, item_value in blob.attributes
                )
            ]
        return blobs[: max(0, int(max_records))]
