"""Small clean Carbon FESL login/system service.

This is intentionally limited to proven stateless/login transactions. Theater,
game browser and GameManager are separate services.
"""

from __future__ import annotations

import base64
import binascii
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import quote

from common.legal import TERMS_OF_SERVICE_TEXT, TERMS_OF_SERVICE_VERSION

from carbon.accounts.credentials import (
    AuthenticationResult,
    CredentialAccountExistsError,
    CredentialStore,
)
from carbon.accounts.identity import Identity, IdentityStore
from carbon.fesl.blob import (
    CarbonBlob,
    CarbonBlobQuotaError,
    CarbonBlobStore,
)
from carbon.dlc import CarbonDLCInventory
from carbon.fesl.frame import FESLFrame, decode_fields
from carbon.theater.directory import CarbonGameDirectory
from carbon.theater.matchmaking import parse_request, preference_key
from carbon.progression import CarbonProgressionStore


log = logging.getLogger(__name__)

_MAX_ACTIVE_CHUNK_BUFFERS = 8
_MAX_CHUNKED_PAYLOAD_ENCODED_SIZE = 2 * 1024 * 1024
_MAX_BLOB_CONTENT_ENCODED_SIZE = 1024 * 1024
_MAX_BLOBS_PER_OWNER = 100
_MAX_BLOB_ATTRIBUTES = 64
_MAX_BLOB_SEARCH_RECORDS = 500
_BLOB_TEXT_LIMITS = {
    "creator": 64,
    "name": 128,
    "version": 64,
    "shortDescription": 1024,
    "longDescription": 4096,
    "locale": 32,
}
_MAX_BLOB_ATTRIBUTE_NAME = 64
_MAX_BLOB_ATTRIBUTE_VALUE = 2048
_LOGIN_ERROR_CODES = {
    # Confirmed against the NFSC PC client's built-in login error dispatcher.
    # In particular, 103 maps to its internal ACCOUNT_BANNED state, while 121
    # maps to TOO_MANY_ATTEMPTS; localizedMessage does not override that state.
    "unknown_account": 101,
    # Confirmed live against NFSC PC: ACCOUNT_PENDING, displayed by the client
    # as "The provided account is currently pending. Contact Customer Support."
    "pending": 105,
    # Confirmed live against NFSC PC: ACCOUNT_DISABLED.
    "disabled": 102,
    "banned": 103,
    "missing_name": 101,
    "bad_password": 122,
    "missing_password": 122,
    "locked": 102,
}

# NFSC has no dedicated ``acct/Login`` error state for an existing session.
# Retail delivers that condition asynchronously through Messenger as
# ``ADMN/TYPE=DUPL``; the client translates it to its internal error -204.
_DUPLICATE_LOGIN_REASONS = frozenset({"account_in_use", "persona_in_use"})
_DUPLICATE_LOGIN_ADMIN_TYPE = "DUPL"

_LOGIN_ERROR_MESSAGES = {
    "unknown_account": "The name you provided cannot be found",
    "pending": "The provided account is currently pending. Contact Customer Support.",
    "disabled": "The account has been disabled. Contact Customer Support.",
    "banned": "This account has been banned. Contact Customer Support.",
    "missing_name": "The name you provided cannot be found",
    "bad_password": "The password does not match the account",
    "missing_password": "The password does not match the account",
    "locked": "The account has been disabled. Contact Customer Support.",
}

_LOGIN_ERROR_FIELDS = {
    "missing_name": "name",
    "missing_password": "password",
    "bad_password": "password",
}


_RANKING_STAT_ALIASES = {
    # The client-facing stats manager uses this name while authoritative race
    # progression stores the accumulated value under the rebroadcaster name.
    "Online_Rep_Level": "Online_Rep",
    # Older retail menus ask for the aggregate instead of the explicit
    # started/finished counters.
    "Total_Online_Games": "Total_Online_Games_Started",
}


@dataclass(frozen=True)
class CarbonEndpoints:
    messenger_host: str
    messenger_port: int
    theater_host: str
    theater_port: int = 18215


@dataclass
class FESLConnection:
    connection_id: str = ""
    identity: Identity | None = None
    session_key: str = ""
    dedicated_server: bool = False
    chunk_buffers: dict[tuple[str, int], tuple[str, int]] | None = None
    replied_transactions: set[tuple[str, int]] | None = None
    play_now_session_id: int = 2549
    ping_responses: int = 0
    registered_game: str = ""
    registered_platform: str = ""
    close_requested: bool = False
    close_reason: str = ""

    def __post_init__(self) -> None:
        if self.chunk_buffers is None:
            self.chunk_buffers = {}
        if self.replied_transactions is None:
            self.replied_transactions = set()


@dataclass(frozen=True)
class _BlobRequest:
    fields: dict[str, str]
    transaction: int
    reply_transaction: int
    connection: FESLConnection
    operation: str
    persona: str
    current_owner: int
    blob_id: int
    blob: CarbonBlob | None


class CarbonFESLService:
    def __init__(
        self,
        endpoints: CarbonEndpoints,
        identities: IdentityStore,
        games: CarbonGameDirectory | None = None,
        progression: CarbonProgressionStore | None = None,
        blobs: CarbonBlobStore | None = None,
        *,
        credentials: CredentialStore | None = None,
        authentication_mode: str = "password",
        clock: Callable[[], datetime] | None = None,
        activity_timeout_seconds: int = 0,
        login_error_probe_code: int | None = None,
        dlc_inventory: CarbonDLCInventory | None = None,
        active_sessions: object | None = None,
    ) -> None:
        if int(activity_timeout_seconds) < 0:
            raise ValueError("activity_timeout_seconds must be zero or positive")
        if (
            login_error_probe_code is not None
            and not 0 <= int(login_error_probe_code) <= 65_535
        ):
            raise ValueError(
                "login_error_probe_code must be None or between 0 and 65535"
            )
        mode = str(authentication_mode or "password").strip().casefold()
        if mode not in {"open", "password"}:
            raise ValueError("authentication_mode must be open or password")
        self.endpoints = endpoints
        self.identities = identities
        self.credentials = credentials
        self.authentication_mode = mode
        self.games = games
        self.progression = progression or CarbonProgressionStore()
        blob_path = (
            self.progression.path.with_name("carbon_blobs.json")
            if self.progression.path is not None
            else None
        )
        self.blobs = blobs or CarbonBlobStore(blob_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # Retail Carbon advertises zero, disabling the FESL idle deadline.
        # Keep it configurable for deployments that explicitly want a finite
        # client-visible session timeout.
        self.activity_timeout_seconds = int(activity_timeout_seconds)
        self.login_error_probe_code = (
            int(login_error_probe_code)
            if login_error_probe_code is not None
            else None
        )
        self.dlc_inventory = dlc_inventory or CarbonDLCInventory.compatibility_default()
        self.active_sessions = active_sessions

    def dispatch(self, frame: FESLFrame, connection: FESLConnection) -> list[FESLFrame]:
        if connection.identity is not None and self.active_sessions is not None:
            touch = getattr(self.active_sessions, "touch", None)
            if touch is not None:
                touch(connection.connection_id)
        fields = self._complete_fields(frame, connection)
        if fields is None:
            return []
        if frame.command.upper() == "ECHO" or fields.get("TXN", "").upper() == "ECHO":
            reply = {
                "TID": fields.get("TID", "1"),
                "TXN": "ECHO",
                "ERR": "0",
                "TYPE": fields.get("TYPE", "1"),
            }
            if fields.get("UID", ""):
                reply["UID"] = fields["UID"]
            return [FESLFrame.from_fields("ECHO", reply, transaction=0)]
        if frame.command == "fsys":
            return self._system(fields, frame.transaction, connection)
        if frame.command == "acct":
            return self._account(fields, frame.transaction, connection)
        if frame.command == "asso" and fields.get("TXN") == "GetAssociations":
            return [self._reply("asso", {"TXN": "GetAssociations", "members.[]": "0"}, frame.transaction)]
        if frame.command == "dobj" and fields.get("TXN") == "GetObjectInventory":
            identity = connection.identity
            viral_tokens = (
                self.progression.viral_tokens_for_profile(identity.profile_id)
                if identity is not None
                else ()
            )
            reply_fields = self.dlc_inventory.fields_for(identity, viral_tokens)
            log.info(
                "Carbon DOBJ inventory: account=%s persona=%s entitlements=%s viral=%s",
                identity.account_name if identity is not None else "<unauthenticated>",
                identity.persona if identity is not None else "<unauthenticated>",
                reply_fields["entitlements.[]"],
                ",".join(viral_tokens) or "none",
            )
            return [self._reply("dobj", reply_fields, frame.transaction)]
        if frame.command == "mtrx":
            if fields.get("TXN") != "ReportMetrics":
                persona = (
                    connection.identity.persona
                    if connection.identity is not None
                    else "<unauthenticated>"
                )
                log.warning(
                    "Carbon Metrics unhandled operation: persona=%s txn=0x%08x "
                    "operation=%s fields=%s",
                    persona,
                    int(frame.transaction) & 0xFFFFFFFF,
                    fields.get("TXN", "<missing>"),
                    ",".join(sorted(fields)) or "<none>",
                )
                return []
            if not self._claim_transaction(frame.command, frame.transaction, connection):
                return []
            return [
                self._reply(
                    "mtrx",
                    {"TXN": "ReportMetrics"},
                    self._reply_transaction(frame.transaction),
                )
            ]
        if frame.command == "rank":
            if not self._claim_transaction(frame.command, frame.transaction, connection):
                return []
            reply = self._rank(fields, frame.transaction, connection)
            return [reply] if reply is not None else []
        if frame.command == "blob":
            if not self._claim_transaction(frame.command, frame.transaction, connection):
                return []
            reply = self._blob(fields, frame.transaction, connection)
            return [reply] if reply is not None else []
        if frame.command == "pnow":
            if not self._claim_transaction(frame.command, frame.transaction, connection):
                return []
            return self._play_now(fields, frame.transaction, connection)
        persona = connection.identity.persona if connection.identity is not None else "<unauthenticated>"
        log.warning(
            "Carbon FESL unhandled request: persona=%s command=%s txn=0x%08x operation=%s fields=%s",
            persona,
            frame.command,
            int(frame.transaction) & 0xFFFFFFFF,
            fields.get("TXN", "<missing>"),
            ",".join(sorted(fields)) or "<none>",
        )
        return []

    @staticmethod
    def _reply_transaction(transaction: int) -> int:
        token = int(transaction) & 0xFFFFFFFF
        if token & 0x30000000:
            token &= 0x8FFFFFFF
        return token or 0x80000000

    @staticmethod
    def _claim_transaction(
        command: str,
        transaction: int,
        connection: FESLConnection,
    ) -> bool:
        key = (command, int(transaction) & 0xFFFFFFFF)
        assert connection.replied_transactions is not None
        if key in connection.replied_transactions:
            return False
        connection.replied_transactions.add(key)
        return True

    @staticmethod
    def _complete_fields(
        frame: FESLFrame,
        connection: FESLConnection,
    ) -> dict[str, str] | None:
        fields = frame.fields
        if "data" not in fields or "size" not in fields:
            return fields
        key = (frame.command, int(frame.transaction) & 0xFFFFFFFF)
        assert connection.chunk_buffers is not None
        old_data, old_size = connection.chunk_buffers.get(key, ("", 0))
        try:
            expected_size = int(fields.get("size", "") or "0")
        except (TypeError, ValueError):
            connection.chunk_buffers.pop(key, None)
            log.warning(
                "Carbon FESL fragmented request rejected: command=%s "
                "txn=0x%08x reason=invalid-size",
                frame.command,
                int(frame.transaction) & 0xFFFFFFFF,
            )
            return None
        if (
            expected_size <= 0
            or expected_size > _MAX_CHUNKED_PAYLOAD_ENCODED_SIZE
            or (old_size and old_size != expected_size)
        ):
            connection.chunk_buffers.pop(key, None)
            log.warning(
                "Carbon FESL fragmented request rejected: command=%s "
                "txn=0x%08x reason=size-limit size=%d",
                frame.command,
                int(frame.transaction) & 0xFFFFFFFF,
                expected_size,
            )
            return None
        if (
            key not in connection.chunk_buffers
            and len(connection.chunk_buffers) >= _MAX_ACTIVE_CHUNK_BUFFERS
        ):
            log.warning(
                "Carbon FESL fragmented request rejected: command=%s "
                "txn=0x%08x reason=too-many-active buffers=%d",
                frame.command,
                int(frame.transaction) & 0xFFFFFFFF,
                len(connection.chunk_buffers),
            )
            return None
        chunk = fields.get("data", "").replace("%3d", "=").replace("%3D", "=")
        try:
            chunk.encode("ascii")
        except UnicodeError:
            connection.chunk_buffers.pop(key, None)
            log.warning(
                "Carbon FESL fragmented request rejected: command=%s "
                "txn=0x%08x reason=non-ascii-data",
                frame.command,
                int(frame.transaction) & 0xFFFFFFFF,
            )
            return None
        encoded = old_data + chunk
        if len(encoded) > expected_size:
            connection.chunk_buffers.pop(key, None)
            log.warning(
                "Carbon FESL fragmented request rejected: command=%s "
                "txn=0x%08x reason=encoded-size-mismatch expected=%d actual=%d",
                frame.command,
                int(frame.transaction) & 0xFFFFFFFF,
                expected_size,
                len(encoded),
            )
            return None
        if len(encoded) < expected_size:
            connection.chunk_buffers[key] = (encoded, expected_size)
            return None
        connection.chunk_buffers.pop(key, None)
        try:
            payload = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (binascii.Error, ValueError, UnicodeError):
            log.warning(
                "Carbon FESL fragmented request rejected: command=%s "
                "txn=0x%08x reason=invalid-base64",
                frame.command,
                int(frame.transaction) & 0xFFFFFFFF,
            )
            return None
        if "decodedSize" in fields:
            try:
                decoded_size = int(fields["decodedSize"])
            except (TypeError, ValueError):
                decoded_size = -1
            if decoded_size != len(payload):
                log.warning(
                    "Carbon FESL fragmented request rejected: command=%s "
                    "txn=0x%08x reason=decoded-size-mismatch expected=%d actual=%d",
                    frame.command,
                    int(frame.transaction) & 0xFFFFFFFF,
                    decoded_size,
                    len(payload),
                )
                return None
        return decode_fields(payload + b"\x00")

    @staticmethod
    def _requested_keys(fields: dict[str, str]) -> list[str]:
        try:
            key_count = min(
                256,
                max(0, int(fields.get("keys.[]", "0") or "0")),
            )
        except ValueError:
            key_count = 0
        keys = [fields.get(f"keys.{index}", "") for index in range(key_count)]
        return [key for key in keys if key]

    @staticmethod
    def _safe_int(value: object, default: int = 0) -> int:
        try:
            return int(str(value or default))
        except (TypeError, ValueError):
            return int(default)

    @classmethod
    def _owner_id(cls, fields: dict[str, str], connection: FESLConnection) -> int:
        owner_text = fields.get("owner", "Current")
        if owner_text.casefold() == "current" and connection.identity is not None:
            return connection.identity.profile_id
        fallback = connection.identity.profile_id if connection.identity is not None else 0
        return cls._safe_int(owner_text, fallback)

    @classmethod
    def _requested_owners(cls, fields: dict[str, str]) -> list[tuple[int, str]]:
        owner_count = max(0, cls._safe_int(fields.get("owners.[]", "0")))
        return [
            (
                cls._safe_int(fields.get(f"owners.{index}.ownerId", "0")),
                fields.get(f"owners.{index}.ownerType", "1"),
            )
            for index in range(owner_count)
        ]

    def _ranking_stat_value(self, profile_id: int, stat: str) -> float:
        stored_stat = _RANKING_STAT_ALIASES.get(str(stat), str(stat))
        return self.progression.stat_for_profile(profile_id, stored_stat)

    def _ranking_stat_text(self, profile_id: int, stat: str) -> str:
        stored_stat = _RANKING_STAT_ALIASES.get(str(stat), str(stat))
        return self.progression.stat_text_for_profile(profile_id, stored_stat)

    @staticmethod
    def _lower_value_is_better(stat: str) -> bool:
        name = str(stat).casefold()
        return (
            "best_time" in name
            or "best_lap" in name
            or name.startswith("evt_bst_")
            or "fewest" in name
        )

    def _leaderboard(self, stat: str) -> list[tuple[int, Identity, float]]:
        stored_stat = _RANKING_STAT_ALIASES.get(str(stat), str(stat))
        rows = [
            (identity, self._ranking_stat_value(identity.profile_id, stat))
            for identity in self.progression.known_identities()
            if self.progression.has_stat_for_profile(identity.profile_id, stored_stat)
        ]
        if self._lower_value_is_better(stat):
            rows.sort(key=lambda item: (item[1], item[0].persona.casefold(), item[0].profile_id))
        else:
            rows.sort(key=lambda item: (-item[1], item[0].persona.casefold(), item[0].profile_id))
        return [
            (rank, identity, value)
            for rank, (identity, value) in enumerate(rows, start=1)
        ]

    def _rank_for_profile(self, profile_id: int, stat: str) -> int:
        for rank, identity, _value in self._leaderboard(stat):
            if identity.profile_id == int(profile_id):
                return rank
        return 0

    @classmethod
    def _rank_window(cls, fields: dict[str, str]) -> tuple[int, int]:
        minimum = max(1, cls._safe_int(fields.get("minRank", "1"), 1))
        maximum = max(minimum, cls._safe_int(fields.get("maxRank", "100"), 100))
        return minimum, min(maximum, minimum + 999)

    def _top_n_reply(
        self,
        fields: dict[str, str],
        transaction_name: str,
        reply_transaction: int,
        *,
        include_additional_stats: bool,
    ) -> FESLFrame:
        # Retail Carbon's Ranking SDK serializes GetTopN's ranking name as
        # ``key`` (NFSC.exe DAT_00a18e00), unlike some other EA titles which
        # use ``name``.  Keep ``name`` as a compatibility fallback.
        ranking_name = (
            fields.get("key", "").strip()
            or fields.get("name", "").strip()
            or "Skill_Level"
        )
        minimum, maximum = self._rank_window(fields)
        rows = [
            row
            for row in self._leaderboard(ranking_name)
            if minimum <= row[0] <= maximum
        ]
        additional_keys = self._requested_keys(fields) if include_additional_stats else []
        reply: dict[str, object] = {
            "TXN": transaction_name,
            "key": ranking_name,
            "name": ranking_name,
            "ownerType": fields.get("ownerType", "1"),
            "stats.[]": str(len(rows)),
        }
        for index, (rank, identity, value) in enumerate(rows):
            prefix = f"stats.{index}"
            reply[f"{prefix}.owner"] = str(identity.profile_id)
            reply[f"{prefix}.ownerType"] = fields.get("ownerType", "1")
            reply[f"{prefix}.name"] = identity.persona
            reply[f"{prefix}.key"] = ranking_name
            reply[f"{prefix}.value"] = f"{value:.4f}"
            reply[f"{prefix}.rank"] = str(rank)
            reply[f"{prefix}.text"] = self._ranking_stat_text(
                identity.profile_id,
                ranking_name,
            )
            if additional_keys:
                reply[f"{prefix}.addStats.[]"] = str(len(additional_keys))
                for stat_index, key in enumerate(additional_keys):
                    stat_prefix = f"{prefix}.addStats.{stat_index}"
                    reply[f"{stat_prefix}.key"] = key
                    reply[f"{stat_prefix}.value"] = (
                        f"{self._ranking_stat_value(identity.profile_id, key):.4f}"
                    )
                    reply[f"{stat_prefix}.text"] = self._ranking_stat_text(
                        identity.profile_id,
                        key,
                    )
        log.info(
            "Carbon Ranking leaderboard: operation=%s stat=%s min_rank=%d max_rank=%d rows=%d add_stats=%d",
            transaction_name,
            ranking_name,
            minimum,
            maximum,
            len(rows),
            len(additional_keys),
        )
        return self._reply("rank", reply, reply_transaction)

    def _rank(
        self,
        fields: dict[str, str],
        transaction: int,
        connection: FESLConnection,
    ) -> FESLFrame | None:
        transaction_name = fields.get("TXN", "")
        reply_transaction = self._reply_transaction(transaction)
        persona = connection.identity.persona if connection.identity is not None else "<unauthenticated>"
        log.info(
            "Carbon Ranking request: persona=%s txn=0x%08x operation=%s key=%s name=%s owners=%s keys=%s "
            "min_rank=%s max_rank=%s",
            persona,
            int(transaction) & 0xFFFFFFFF,
            transaction_name,
            fields.get("key", "<none>"),
            fields.get("name", "<none>"),
            fields.get("owners.[]", "0"),
            fields.get("keys.[]", "0"),
            fields.get("minRank", "<none>"),
            fields.get("maxRank", "<none>"),
        )
        if transaction_name == "GetStats":
            keys = self._requested_keys(fields) or [
                "Skill_Level",
                "DNF",
                "Total_Online_Games_Started",
                "Total_Online_Games_Finished",
            ]
            owner_id = self._owner_id(fields, connection)
            reply: dict[str, object] = {
                "TXN": "GetStats",
                "ownerId": str(owner_id),
                "ownerType": fields.get("ownerType", "1"),
                "stats.[]": str(len(keys)),
            }
            for index, key in enumerate(keys):
                reply[f"stats.{index}.key"] = key
                reply[f"stats.{index}.value"] = f"{self._ranking_stat_value(owner_id, key):.4f}"
                reply[f"stats.{index}.rank"] = str(self._rank_for_profile(owner_id, key))
                reply[f"stats.{index}.text"] = self._ranking_stat_text(owner_id, key)
            return self._reply("rank", reply, reply_transaction)

        if transaction_name == "GetStatsForOwners":
            keys = self._requested_keys(fields) or ["Skill_Level", "Virus1", "Virus2", "Virus3", "Moderator"]
            owners = self._requested_owners(fields)
            reply = {"TXN": "GetStatsForOwners", "stats.[]": str(len(owners))}
            for owner_index, (owner_id, owner_type) in enumerate(owners):
                reply[f"stats.{owner_index}.ownerId"] = str(owner_id)
                reply[f"stats.{owner_index}.ownerType"] = owner_type
                reply[f"stats.{owner_index}.stats.[]"] = str(len(keys))
                for stat_index, key in enumerate(keys):
                    prefix = f"stats.{owner_index}.stats.{stat_index}"
                    reply[f"{prefix}.key"] = key
                    reply[f"{prefix}.value"] = f"{self._ranking_stat_value(owner_id, key):.4f}"
                    reply[f"{prefix}.text"] = self._ranking_stat_text(owner_id, key)
            return self._reply("rank", reply, reply_transaction)

        if transaction_name == "GetRankedStats":
            keys = self._requested_keys(fields) or ["Skill_Level"]
            owner_id = self._owner_id(fields, connection)
            reply = {
                "TXN": "GetRankedStats",
                "ownerId": str(owner_id),
                "ownerType": fields.get("ownerType", "1"),
                "stats.[]": str(len(keys)),
            }
            for index, key in enumerate(keys):
                prefix = f"stats.{index}"
                reply[f"{prefix}.key"] = key
                reply[f"{prefix}.value"] = f"{self._ranking_stat_value(owner_id, key):.4f}"
                reply[f"{prefix}.rank"] = str(self._rank_for_profile(owner_id, key))
                reply[f"{prefix}.text"] = self._ranking_stat_text(owner_id, key)
            return self._reply("rank", reply, reply_transaction)

        if transaction_name == "GetRankedStatsForOwners":
            keys = self._requested_keys(fields) or ["Skill_Level"]
            owners = self._requested_owners(fields)
            reply = {
                "TXN": "GetRankedStatsForOwners",
                "rankedStats.[]": str(len(owners)),
            }
            for owner_index, (owner_id, owner_type) in enumerate(owners):
                prefix = f"rankedStats.{owner_index}"
                reply[f"{prefix}.ownerId"] = str(owner_id)
                reply[f"{prefix}.ownerType"] = owner_type
                reply[f"{prefix}.rankedStats.[]"] = str(len(keys))
                for stat_index, key in enumerate(keys):
                    stat_prefix = f"{prefix}.rankedStats.{stat_index}"
                    reply[f"{stat_prefix}.value"] = (
                        f"{self._ranking_stat_value(owner_id, key):.4f}"
                    )
                    reply[f"{stat_prefix}.rank"] = str(self._rank_for_profile(owner_id, key))
                    reply[f"{stat_prefix}.key"] = key
                    reply[f"{stat_prefix}.text"] = self._ranking_stat_text(owner_id, key)
            return self._reply("rank", reply, reply_transaction)

        if transaction_name == "GetTopN":
            return self._top_n_reply(
                fields,
                transaction_name,
                reply_transaction,
                include_additional_stats=False,
            )

        if transaction_name == "GetTopNAndStats":
            return self._top_n_reply(
                fields,
                transaction_name,
                reply_transaction,
                include_additional_stats=True,
            )

        if transaction_name == "GetDateRange":
            now = self.clock().astimezone(timezone.utc)
            return self._reply(
                "rank",
                {
                    "TXN": "GetDateRange",
                    "startDate": "Jan-01-2000 00:00:00 UTC",
                    "endDate": now.strftime("%b-%d-%Y %H:%M:%S UTC"),
                },
                reply_transaction,
            )

        if transaction_name == "UpdateStats":
            try:
                user_count = max(0, int(fields.get("u.[]", "0") or "0"))
            except ValueError:
                user_count = 0
            event_updates: list[str] = []
            for user_index in range(user_count):
                try:
                    owner_id = int(fields.get(f"u.{user_index}.o", "0") or "0")
                    stat_count = max(0, int(fields.get(f"u.{user_index}.s.[]", "0") or "0"))
                except ValueError:
                    continue
                for stat_index in range(stat_count):
                    prefix = f"u.{user_index}.s.{stat_index}"
                    key = fields.get(f"{prefix}.k", "")
                    try:
                        value = float(fields.get(f"{prefix}.v", "0") or "0")
                        update_type = int(fields.get(f"{prefix}.ut", "0") or "0")
                    except ValueError:
                        continue
                    text_value = fields.get(f"{prefix}.t", "")
                    changed = self.progression.apply_rank_update(
                        owner_id,
                        key,
                        value,
                        update_type=update_type,
                        trusted_server=connection.dedicated_server,
                        text=text_value,
                    )
                    if key.casefold().startswith("evt_bst_"):
                        event_updates.append(
                            f"{key}:owner={owner_id}:value={value:.4f}:"
                            f"ut={update_type}:text={int(bool(text_value))}:changed={int(changed)}"
                        )
            if event_updates:
                log.info(
                    "Carbon Ranking event updates: persona=%s count=%d entries=%s",
                    persona,
                    len(event_updates),
                    ",".join(event_updates),
                )
            return self._reply("rank", {"TXN": "UpdateStats"}, reply_transaction)

        log.warning(
            "Carbon Ranking unhandled operation: persona=%s txn=0x%08x operation=%s fields=%s",
            persona,
            int(transaction) & 0xFFFFFFFF,
            transaction_name or "<missing>",
            ",".join(sorted(fields)) or "<none>",
        )
        return None

    @staticmethod
    def _blob_boolean(value: object) -> bool:
        return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}

    @classmethod
    def _blob_attributes(
        cls,
        fields: dict[str, str],
        root: str,
    ) -> list[tuple[str, int, str]]:
        count = min(
            _MAX_BLOB_ATTRIBUTES,
            max(0, cls._safe_int(fields.get(f"{root}.[]", "0"))),
        )
        attributes: list[tuple[str, int, str]] = []
        for index in range(count):
            prefix = f"{root}.{index}"
            name = fields.get(f"{prefix}.name", "")
            if not name:
                continue
            attributes.append(
                (
                    name,
                    cls._safe_int(fields.get(f"{prefix}.type", "0")),
                    fields.get(f"{prefix}.value", ""),
                )
            )
        return attributes

    @staticmethod
    def _blob_date(now: datetime) -> str:
        return now.astimezone(timezone.utc).strftime("%b-%d-%Y %H:%M:%S UTC")

    @staticmethod
    def _blob_content(fields: dict[str, str]) -> str:
        """Restore Base64 padding escaped by the retail FESL field codec."""

        return (
            fields.get("content", "")
            .replace("%3d", "=")
            .replace("%3D", "=")
        )

    @staticmethod
    def _blob_metadata_fields(blob: CarbonBlob, prefix: str = "") -> dict[str, object]:
        root = f"{prefix}." if prefix else ""
        reply: dict[str, object] = {
            f"{root}blobId": str(blob.blob_id),
            f"{root}ownerId": str(blob.owner_id),
            f"{root}ownerType": str(blob.owner_type),
            f"{root}type": str(blob.blob_type),
            f"{root}formatType": str(blob.format_type),
            f"{root}iconId": str(blob.icon_id),
            f"{root}createDate": blob.create_date,
            f"{root}updateDate": blob.update_date,
            f"{root}creator": blob.creator,
            f"{root}name": blob.name,
            f"{root}downloadCount": str(blob.download_count),
            f"{root}rating": f"{blob.rating:.4f}",
            f"{root}reviewCount": str(blob.review_count),
            f"{root}version": blob.version,
            f"{root}shortDescription": blob.short_description,
            f"{root}longDescription": blob.long_description,
            f"{root}locale": blob.locale,
            f"{root}attributes.[]": str(len(blob.attributes)),
        }
        for index, (name, kind, value) in enumerate(blob.attributes):
            attribute = f"{root}attributes.{index}"
            reply[f"{attribute}.name"] = name
            reply[f"{attribute}.type"] = str(kind)
            reply[f"{attribute}.value"] = value
        return reply

    @classmethod
    def _blob_write_validation_error(
        cls,
        fields: dict[str, str],
        *,
        validate_content: bool,
        validate_metadata: bool,
    ) -> str:
        if validate_content:
            content = cls._blob_content(fields)
            try:
                encoded_content = content.encode("ascii")
            except UnicodeError:
                return "content-not-ascii"
            if len(encoded_content) > _MAX_BLOB_CONTENT_ENCODED_SIZE:
                return "content-too-large"
            try:
                base64.b64decode(encoded_content, validate=True)
            except (binascii.Error, ValueError):
                return "content-not-base64"
        if not validate_metadata:
            return ""
        for field_name, limit in _BLOB_TEXT_LIMITS.items():
            if len(fields.get(field_name, "")) > limit:
                return f"{field_name}-too-long"
        attribute_count = max(
            0,
            cls._safe_int(fields.get("attributes.[]", "0")),
        )
        if attribute_count > _MAX_BLOB_ATTRIBUTES:
            return "too-many-attributes"
        for name, _kind, value in cls._blob_attributes(fields, "attributes"):
            if len(name) > _MAX_BLOB_ATTRIBUTE_NAME:
                return "attribute-name-too-long"
            if len(value) > _MAX_BLOB_ATTRIBUTE_VALUE:
                return "attribute-value-too-long"
        remove_count = max(
            0,
            cls._safe_int(fields.get("removeAttributes.[]", "0")),
        )
        if remove_count > _MAX_BLOB_ATTRIBUTES:
            return "too-many-removed-attributes"
        return ""

    def _blob_rejected(
        self,
        *,
        operation: str,
        transaction: int,
        persona: str,
        reason: str,
        blob_id: int = 0,
    ) -> FESLFrame:
        log.warning(
            "Carbon Blob mutation rejected: persona=%s txn=0x%08x "
            "operation=%s blob_id=%s reason=%s",
            persona,
            int(transaction) & 0xFFFFFFFF,
            operation,
            blob_id if blob_id else "<none>",
            reason,
        )
        fields: dict[str, object] = {
            "TXN": operation,
            # 120 is Carbon's retail-compatible "not entitled" response.
            # The detailed reason remains server-side to avoid exposing policy.
            "errorCode": "120",
            "localizedMessage": '"Blob request rejected"',
        }
        if blob_id:
            fields["blobId"] = str(blob_id)
        return self._reply(
            "blob",
            fields,
            self._reply_transaction(transaction),
        )

    def _blob(
        self,
        fields: dict[str, str],
        transaction: int,
        connection: FESLConnection,
    ) -> FESLFrame | None:
        operation = fields.get("TXN", "")
        reply_transaction = self._reply_transaction(transaction)
        persona = (
            connection.identity.persona
            if connection.identity is not None
            else "<unauthenticated>"
        )
        current_owner = (
            connection.identity.profile_id
            if connection.identity is not None
            else 0
        )
        log.info(
            "Carbon Blob request: persona=%s txn=0x%08x operation=%s blob_id=%s "
            "owner=%s type=%s name=%s content=%d",
            persona,
            int(transaction) & 0xFFFFFFFF,
            operation,
            fields.get("blobId", "<none>"),
            fields.get("ownerId", str(current_owner) if current_owner else "<none>"),
            fields.get("type", "<none>"),
            fields.get("name", "<none>"),
            len(fields.get("content", "").encode("latin-1", errors="replace")),
        )
        blob_id = self._safe_int(fields.get("blobId"))
        request = _BlobRequest(
            fields=fields,
            transaction=transaction,
            reply_transaction=reply_transaction,
            connection=connection,
            operation=operation,
            persona=persona,
            current_owner=current_owner,
            blob_id=blob_id,
            blob=self.blobs.get(blob_id),
        )
        handlers = {
            "ListBlobInfo": self._blob_list,
            "TopNBlobDownloads": self._blob_list,
            "TopNBlobRatings": self._blob_list,
            "AddBlob": self._blob_add,
            "GetBlobInfo": self._blob_get_info,
            "GetBlobContent": self._blob_get_content,
            "RemoveBlob": self._blob_remove,
            "UpdateBlobContent": self._blob_update_content,
            "UpdateBlobRating": self._blob_update_rating,
            "UpdateBlobInfo": self._blob_update_info,
        }
        handler = handlers.get(operation)
        if handler is None:
            log.warning(
                "Carbon Blob unhandled operation: persona=%s txn=0x%08x operation=%s fields=%s",
                persona,
                int(transaction) & 0xFFFFFFFF,
                operation or "<missing>",
                ",".join(sorted(fields)) or "<none>",
            )
            return None
        return handler(request)

    def _blob_list(self, request: _BlobRequest) -> FESLFrame:
        fields = request.fields
        operation = request.operation
        reply_transaction = request.reply_transaction
        blob = request.blob
        owner_id = (
            self._safe_int(fields.get("ownerId"))
            if fields.get("ownerId", "") not in {"", "-1"}
            else None
        )
        owner_type = (
            self._safe_int(fields.get("ownerType"), 1)
            if fields.get("ownerType", "") not in {"", "-1"}
            else None
        )
        blob_type = (
            self._safe_int(fields.get("type"))
            if fields.get("type", "") not in {"", "-1"}
            else None
        )
        search_attributes = tuple(
            (name, value)
            for name, _kind, value in self._blob_attributes(
                fields,
                "searchAttributes",
            )
        )
        rows = self.blobs.search(
            owner_id=owner_id,
            owner_type=owner_type,
            blob_type=blob_type,
            name=fields.get("name", ""),
            name_case_sensitive=self._blob_boolean(
                fields.get("nameCaseSensitive", "0")
            ),
            name_wildcard=self._blob_boolean(
                fields.get("nameWildcardMatch", "0")
            ),
            max_records=max(
                0,
                min(
                    _MAX_BLOB_SEARCH_RECORDS,
                    self._safe_int(
                        fields.get("maxRecords", "20000"),
                        20_000,
                    ),
                ),
            ),
            attributes=search_attributes,
        )
        if operation == "TopNBlobDownloads":
            rows.sort(key=lambda blob: (-blob.download_count, blob.blob_id))
        elif operation == "TopNBlobRatings":
            rows.sort(key=lambda blob: (-blob.rating, blob.blob_id))
        reply: dict[str, object] = {
            "TXN": operation,
            "nextChunkFlag": "0",
            "blobs.[]": str(len(rows)),
            # Retail's list-result parser reads this one field without the
            # ``blobs.N`` prefix even though all other metadata is scoped.
            "ownerType": str(owner_type if owner_type is not None else 1),
        }
        for index, blob in enumerate(rows):
            reply.update(self._blob_metadata_fields(blob, f"blobs.{index}"))
        log.info(
            "Carbon Blob list: operation=%s owner=%s type=%s name=%s rows=%d",
            operation,
            owner_id if owner_id is not None else "any",
            blob_type if blob_type is not None else "any",
            fields.get("name", "<any>") or "<any>",
            len(rows),
        )
        return self._reply("blob", reply, reply_transaction)

    def _blob_add(self, request: _BlobRequest) -> FESLFrame:
        fields = request.fields
        transaction = request.transaction
        connection = request.connection
        operation = request.operation
        reply_transaction = request.reply_transaction
        persona = request.persona
        current_owner = request.current_owner
        blob = request.blob
        if connection.identity is None:
            return self._blob_rejected(
                operation=operation,
                transaction=transaction,
                persona=persona,
                reason="authentication-required",
            )
        validation_error = self._blob_write_validation_error(
            fields,
            validate_content=True,
            validate_metadata=True,
        )
        if validation_error:
            return self._blob_rejected(
                operation=operation,
                transaction=transaction,
                persona=persona,
                reason=validation_error,
            )
        now = self._blob_date(self.clock())
        try:
            blob = self.blobs.add(
                CarbonBlob(
                    blob_id=0,
                    # Never trust client-supplied ownership metadata.
                    owner_id=current_owner,
                    owner_type=1,
                    blob_type=self._safe_int(fields.get("type")),
                    format_type=self._safe_int(fields.get("formatType")),
                    icon_id=self._safe_int(fields.get("iconId")),
                    create_date=now,
                    update_date=now,
                    creator=persona,
                    name=fields.get("name", ""),
                    version=fields.get("version", ""),
                    short_description=fields.get("shortDescription", ""),
                    long_description=fields.get("longDescription", ""),
                    locale=fields.get("locale", ""),
                    content=self._blob_content(fields),
                    attributes=self._blob_attributes(fields, "attributes"),
                ),
                max_owner_blobs=_MAX_BLOBS_PER_OWNER,
            )
        except CarbonBlobQuotaError:
            return self._blob_rejected(
                operation=operation,
                transaction=transaction,
                persona=persona,
                reason="owner-quota-exceeded",
            )
        log.info(
            "Carbon Blob stored: operation=AddBlob blob_id=%d owner=%d type=%d "
            "name=%s bytes=%d",
            blob.blob_id,
            blob.owner_id,
            blob.blob_type,
            blob.name or "<none>",
            blob.unencoded_size,
        )
        return self._reply(
            "blob",
            {"TXN": "AddBlob", "blobId": str(blob.blob_id)},
            reply_transaction,
        )

    def _blob_get_info(self, request: _BlobRequest) -> FESLFrame:
        operation = request.operation
        reply_transaction = request.reply_transaction
        blob = request.blob
        reply = {"TXN": "GetBlobInfo"}
        if blob is not None:
            reply.update(self._blob_metadata_fields(blob))
        return self._reply("blob", reply, reply_transaction)

    def _blob_get_content(self, request: _BlobRequest) -> FESLFrame:
        connection = request.connection
        operation = request.operation
        reply_transaction = request.reply_transaction
        persona = request.persona
        blob_id = request.blob_id
        blob = request.blob
        reply = {
            "TXN": "GetBlobContent",
            "blobId": str(blob_id),
            "nextChunkFlag": "0",
            # NFSC's BlobGetBlobContentResult parser reads ``size`` first
            # and uses it to allocate/decode ``content``.  Omitting it
            # makes a valid large shadow look empty to the client.
            "size": str(blob.encoded_size if blob is not None else 0),
            "unencodedSize": str(blob.unencoded_size if blob is not None else 0),
            "content": blob.content if blob is not None else "",
        }
        log.info(
            "Carbon Blob content reply: persona=%s blob_id=%d found=%d "
            "encoded=%d unencoded=%d",
            persona,
            blob_id,
            int(blob is not None),
            blob.encoded_size if blob is not None else 0,
            blob.unencoded_size if blob is not None else 0,
        )
        if blob is not None and connection.identity is not None:
            self.blobs.update(
                blob_id,
                download_count=blob.download_count + 1,
            )
        return self._reply("blob", reply, reply_transaction)

    def _blob_remove(self, request: _BlobRequest) -> FESLFrame:
        transaction = request.transaction
        connection = request.connection
        operation = request.operation
        reply_transaction = request.reply_transaction
        persona = request.persona
        current_owner = request.current_owner
        blob_id = request.blob_id
        blob = request.blob
        if (
            connection.identity is None
            or blob is None
            or blob.owner_id != current_owner
        ):
            return self._blob_rejected(
                operation=operation,
                transaction=transaction,
                persona=persona,
                reason="owner-required",
                blob_id=blob_id,
            )
        removed = self.blobs.remove_owned(blob_id, current_owner)
        log.info(
            "Carbon Blob removed: blob_id=%d removed=%d",
            blob_id,
            int(removed),
        )
        return self._reply(
            "blob",
            {"TXN": "RemoveBlob", "blobId": str(blob_id)},
            reply_transaction,
        )

    def _blob_update_content(self, request: _BlobRequest) -> FESLFrame:
        fields = request.fields
        transaction = request.transaction
        connection = request.connection
        operation = request.operation
        reply_transaction = request.reply_transaction
        persona = request.persona
        current_owner = request.current_owner
        blob_id = request.blob_id
        blob = request.blob
        if (
            connection.identity is None
            or blob is None
            or blob.owner_id != current_owner
        ):
            return self._blob_rejected(
                operation=operation,
                transaction=transaction,
                persona=persona,
                reason="owner-required",
                blob_id=blob_id,
            )
        validation_error = self._blob_write_validation_error(
            fields,
            validate_content=True,
            validate_metadata=True,
        )
        if validation_error:
            return self._blob_rejected(
                operation=operation,
                transaction=transaction,
                persona=persona,
                reason=validation_error,
                blob_id=blob_id,
            )
        changes: dict[str, object] = {
            "content": self._blob_content(fields),
            "update_date": self._blob_date(self.clock()),
        }
        if "version" in fields:
            changes["version"] = fields["version"]
        updated = self.blobs.update_owned(
            blob_id,
            current_owner,
            **changes,
        )
        log.info(
            "Carbon Blob stored: operation=UpdateBlobContent blob_id=%d found=%d bytes=%d",
            blob_id,
            int(updated is not None),
            updated.unencoded_size if updated is not None else 0,
        )
        return self._reply(
            "blob",
            {"TXN": "UpdateBlobContent", "blobId": str(blob_id)},
            reply_transaction,
        )

    def _blob_update_rating(self, request: _BlobRequest) -> FESLFrame:
        fields = request.fields
        transaction = request.transaction
        connection = request.connection
        operation = request.operation
        reply_transaction = request.reply_transaction
        persona = request.persona
        blob_id = request.blob_id
        blob = request.blob
        if connection.identity is None or blob is None:
            return self._blob_rejected(
                operation=operation,
                transaction=transaction,
                persona=persona,
                reason="authentication-required",
                blob_id=blob_id,
            )
        try:
            rating = float(fields.get("rating", "0") or "0")
        except ValueError:
            rating = math.nan
        if not math.isfinite(rating) or not 0.0 <= rating <= 5.0:
            return self._blob_rejected(
                operation=operation,
                transaction=transaction,
                persona=persona,
                reason="invalid-rating",
                blob_id=blob_id,
            )
        review_count = blob.review_count + 1
        self.blobs.update(
            blob_id,
            rating=rating,
            review_count=review_count,
            update_date=self._blob_date(self.clock()),
        )
        return self._reply(
            "blob",
            {"TXN": "UpdateBlobRating", "blobId": str(blob_id)},
            reply_transaction,
        )

    def _blob_update_info(self, request: _BlobRequest) -> FESLFrame:
        fields = request.fields
        transaction = request.transaction
        connection = request.connection
        operation = request.operation
        reply_transaction = request.reply_transaction
        persona = request.persona
        current_owner = request.current_owner
        blob_id = request.blob_id
        blob = request.blob
        if (
            connection.identity is None
            or blob is None
            or blob.owner_id != current_owner
        ):
            return self._blob_rejected(
                operation=operation,
                transaction=transaction,
                persona=persona,
                reason="owner-required",
                blob_id=blob_id,
            )
        validation_error = self._blob_write_validation_error(
            fields,
            validate_content=False,
            validate_metadata=True,
        )
        if validation_error:
            return self._blob_rejected(
                operation=operation,
                transaction=transaction,
                persona=persona,
                reason=validation_error,
                blob_id=blob_id,
            )
        changes = {"update_date": self._blob_date(self.clock())}
        metadata_fields = {
            "formatType": ("format_type", self._safe_int),
            "iconId": ("icon_id", self._safe_int),
            "creator": ("creator", str),
            "name": ("name", str),
            "version": ("version", str),
            "shortDescription": ("short_description", str),
            "longDescription": ("long_description", str),
            "locale": ("locale", str),
        }
        for field_name, (attribute_name, converter) in metadata_fields.items():
            if field_name in fields:
                changes[attribute_name] = converter(fields[field_name])
        if "attributes.[]" in fields:
            changes["attributes"] = self._blob_attributes(fields, "attributes")
        updated = self.blobs.update_owned(
            blob_id,
            current_owner,
            **changes,
        )
        if updated is not None:
            remove_count = min(
                _MAX_BLOB_ATTRIBUTES,
                max(
                    0,
                    self._safe_int(
                        fields.get("removeAttributes.[]", "0")
                    ),
                ),
            )
            removed_names = {
                fields.get(f"removeAttributes.{index}", "")
                for index in range(remove_count)
            }
            if removed_names:
                self.blobs.update_owned(
                    blob_id,
                    current_owner,
                    attributes=[
                        item
                        for item in updated.attributes
                        if item[0] not in removed_names
                    ],
                )
        return self._reply(
            "blob",
            {"TXN": "UpdateBlobInfo", "blobId": str(blob_id)},
            reply_transaction,
        )

    def _play_now(
        self,
        fields: dict[str, str],
        transaction: int,
        connection: FESLConnection,
    ) -> list[FESLFrame]:
        connection.play_now_session_id += 1
        if connection.play_now_session_id > 2_147_483_647:
            connection.play_now_session_id = 2550
        session_id = str(connection.play_now_session_id)
        partition = fields.get("partition.partition", "/eagames/NFS-2007") or "/eagames/NFS-2007"
        if partition.casefold() == "/eagames/nfs-2007":
            partition = "/eagames/NFS-2007"
        start = {
            "TXN": fields.get("TXN", "Start"),
            "id.id": session_id,
            "id.partition": partition,
        }
        request = parse_request(fields)
        persona = connection.identity.persona if connection.identity is not None else "<unauthenticated>"
        help_type = fields.get(preference_key("pref", "help_type"), fields.get(preference_key("filter", "help_type"), ""))
        game_mode = fields.get(preference_key("pref", "game_mode"), fields.get(preference_key("filter", "game_mode"), ""))
        log.info(
            "Carbon PlayNow request: persona=%s txn=0x%08x session_id=%s session_type=%s "
            "allowed_types=%s concrete_type=%s states=%s version=%s help_type=%s game_mode=%s threshold=%.4f",
            persona,
            int(transaction) & 0xFFFFFFFF,
            session_id,
            request.session_type or "<missing>",
            "|".join(sorted(request.allowed_game_types)) or "<any>",
            request.concrete_game_type or "<none>",
            "|".join(sorted(request.matchmaking_states)) or "<any>",
            request.version or "<any>",
            help_type or "<missing>",
            game_mode or "<missing>",
            request.fit_threshold,
        )
        resolution = None
        if self.games is not None and connection.identity is not None:
            resolution = self.games.resolve_play_now(connection.identity, fields)
        elif connection.identity is None:
            log.warning("Carbon PlayNow rejected before matchmaking: session_id=%s reason=NOT_AUTHENTICATED", session_id)
        status = {
            "sessionState": "COMPLETE",
            "TXN": "Status",
            "props.{}": "2",
            "props.{games}.[]": "0",
            "props.{resultType}": "NOSERVER",
            "id.id": session_id,
            "id.partition": partition,
        }
        if resolution is not None:
            game = resolution.game
            log.info(
                "Carbon PlayNow result: persona=%s session_id=%s result=JOIN gid=%s created=%s fit=%.4f "
                "game_type=%s matchmaking_state=%s help_type=%s host=%s participants=%d/%d",
                persona,
                session_id,
                game.gid,
                int(resolution.created),
                resolution.avg_fit,
                game.properties.get("B-U-game_type", "?"),
                game.properties.get("B-U-matchmaking_state", "?"),
                game.properties.get("B-U-help_type", "?"),
                game.host.persona,
                len(game.participants),
                game.session.capacity,
            )
            status = {
                "sessionState": "COMPLETE",
                "props.{avgFit}": f"{resolution.avg_fit:.12g}",
                "props.{games}.[]": "1",
                "props.{games}.0.gid": game.gid,
                "TXN": "Status",
                "props.{}": "3",
                "props.{games}.0.lid": game.lobby_id,
                "props.{resultType}": "JOIN",
                "id.id": session_id,
                "id.partition": partition,
            }
        if resolution is None:
            log.warning(
                "Carbon PlayNow result: persona=%s session_id=%s result=NOSERVER session_type=%s "
                "allowed_types=%s concrete_type=%s",
                persona,
                session_id,
                request.session_type or "<missing>",
                "|".join(sorted(request.allowed_game_types)) or "<any>",
                request.concrete_game_type or "<none>",
            )
        return [
            self._reply("pnow", start, self._reply_transaction(transaction)),
            self._reply("pnow", status, 0x80000000),
        ]

    @staticmethod
    def _reply(command: str, fields: dict[str, object], transaction: int) -> FESLFrame:
        return FESLFrame.from_fields(command, fields, transaction=transaction)

    @staticmethod
    def ping_frame() -> FESLFrame:
        """Build the asynchronous FESL Ping seen every 30 s in retail traffic."""
        return FESLFrame.from_fields("fsys", {"TXN": "Ping"}, transaction=0)

    def _memcheck_challenge(self) -> dict[str, object]:
        now = self.clock().astimezone(timezone.utc)
        return {
            "TXN": "MemCheck",
            "memcheck.[]": "0",
            "type": "0",
            "salt": str(int(now.timestamp()) & 0x7FFFFFFF),
        }

    def _system(
        self,
        fields: dict[str, str],
        transaction: int,
        connection: FESLConnection,
    ) -> list[FESLFrame]:
        transaction_name = fields.get("TXN", "")
        if transaction_name == "Ping":
            # This is the client's response to the asynchronous server Ping,
            # normally carried with response transaction 0x80000000.  It
            # closes the liveness transaction and must not be answered again.
            connection.ping_responses += 1
            return []
        if transaction_name == "Hello":
            connection.dedicated_server = fields.get("clientType", "").strip().casefold() == "server"
            now = self.clock().astimezone(timezone.utc)
            hello = {
                "domainPartition.domain": "eagames",
                "messengerIp": self.endpoints.messenger_host,
                "messengerPort": str(self.endpoints.messenger_port),
                "domainPartition.subDomain": "NFS-2007",
                "TXN": "Hello",
                "activityTimeoutSecs": str(self.activity_timeout_seconds),
                "curTime": now.strftime('"%b-%d-%Y %H%%3a%M%%3a%S UTC"'),
                "theaterIp": self.endpoints.theater_host,
                "theaterPort": str(self.endpoints.theater_port),
            }
            return [
                self._reply("fsys", hello, transaction),
                self._reply("fsys", self._memcheck_challenge(), 0x80000000),
            ]
        if transaction_name == "GetPingSites":
            return [
                self._reply(
                    "fsys",
                    {"pingSite.[]": "0", "minPingSitesToPing": "0", "TXN": "GetPingSites"},
                    transaction,
                )
            ]
        if transaction_name == "MemCheck":
            # Retail answers the asynchronous challenge with ``result=`` and
            # receives no second application reply.  A request without result
            # is a challenge request, not a reason to emit a placeholder salt.
            if "result" in fields:
                return []
            return [
                self._reply(
                    "fsys",
                    self._memcheck_challenge(),
                    transaction,
                )
            ]
        persona = (
            connection.identity.persona
            if connection.identity is not None
            else "<unauthenticated>"
        )
        log.warning(
            "Carbon FESL unhandled system transaction: persona=%s txn=0x%08x "
            "operation=%s fields=%s",
            persona,
            int(transaction) & 0xFFFFFFFF,
            transaction_name or "<missing>",
            ",".join(sorted(fields)) or "<none>",
        )
        return []

    def account_policy_frame(
        self,
        action: str,
        *,
        transaction: int = 0x80000000,
    ) -> FESLFrame:
        """Return Carbon's native restrictive-policy Login response."""

        policy = str(action or "").strip().casefold()
        if policy not in {"ban", "disable", "kick"}:
            raise ValueError(f"unsupported restrictive account policy: {action!r}")
        return self._login_error(
            AuthenticationResult(
                False,
                "banned" if policy == "ban" else "disabled",
            ),
            int(transaction) & 0xFFFFFFFF,
        )

    def _login_error(
        self,
        result: AuthenticationResult,
        transaction: int,
    ) -> FESLFrame:
        reason = result.reason if result.reason in _LOGIN_ERROR_CODES else "unknown_account"
        code = _LOGIN_ERROR_CODES[reason]
        fields: dict[str, object] = {
            "TXN": "Login",
            "localizedMessage": f'"{_LOGIN_ERROR_MESSAGES[reason]}"',
            "errorCode": str(code),
        }
        field_name = _LOGIN_ERROR_FIELDS.get(reason, "")
        if field_name:
            fields.update(
                {
                    "errorContainer.[]": "1",
                    "errorContainer.0.fieldName": field_name,
                    "errorContainer.0.fieldError": str(code),
                }
            )
        if result.retry_after_seconds > 0:
            fields["retryAfterSecs"] = str(result.retry_after_seconds)
        return self._reply("acct", fields, transaction)

    @staticmethod
    def _clean_screen_name_seed(value: object) -> str:
        raw = str(value or "").strip()
        cleaned = "".join(
            character for character in raw
            if character.isalnum() or character in {"_", "-"}
        )
        return (cleaned or "Driver")[:32]

    def _screen_name_available(self, value: str) -> bool:
        if self.credentials is None:
            return True
        checker = getattr(self.credentials, "screen_name_available", None)
        if callable(checker):
            return bool(checker(value))
        key = str(value or "").strip().casefold()
        for account in self.credentials.accounts():
            if key in {
                account.account_name.casefold(),
                account.persona.casefold(),
                account.email.casefold(),
            }:
                return False
        return True

    def _suggest_screen_names(self, fields: dict[str, str]) -> tuple[str, ...]:
        try:
            requested = int(str(fields.get("maxSuggestions", "5") or "5"))
        except (TypeError, ValueError):
            requested = 5
        maximum = max(1, min(requested, 20))
        base = self._clean_screen_name_seed(fields.get("name", ""))
        keyword_values = [
            self._clean_screen_name_seed(value)
            for key, value in sorted(fields.items())
            if key.startswith("keywords.") and key != "keywords.[]" and str(value or "").strip()
        ]
        seeds = []
        for value in (base, *keyword_values):
            if value.casefold() not in {item.casefold() for item in seeds}:
                seeds.append(value)

        candidates: list[str] = []
        seen: set[str] = {base.casefold()}
        for number in range(1, 10_000):
            for seed in seeds:
                suffix = str(number)
                candidate = f"{seed[:32-len(suffix)]}{suffix}"
                key = candidate.casefold()
                if len(candidate) < 3 or key in seen:
                    continue
                seen.add(key)
                if self._screen_name_available(candidate):
                    candidates.append(candidate)
                    if len(candidates) >= maximum:
                        return tuple(candidates)
        return tuple(candidates)

    def _account(
        self,
        fields: dict[str, str],
        transaction: int,
        connection: FESLConnection,
    ) -> list[FESLFrame]:
        transaction_name = fields.get("TXN", "")
        handlers = {
            "SuggestScreenNames": self._account_suggest_names,
            "RegisterGame": self._account_register_game,
            "AddAccount": self._account_add,
            "GetTos": self._account_terms,
            "GetCountryList": self._account_countries,
            "Login": self._account_login,
            "NuGetPersonas": self._account_personas,
            "NuLoginPersona": self._account_login_persona,
        }
        handler = handlers.get(transaction_name)
        identity_required = transaction_name in {
            "NuGetPersonas",
            "NuLoginPersona",
        }
        if handler is not None and (
            not identity_required or connection.identity is not None
        ):
            return handler(fields, transaction, connection)

        persona = (
            connection.identity.persona
            if connection.identity is not None
            else "<unauthenticated>"
        )
        log.warning(
            "Carbon FESL unhandled account transaction: persona=%s txn=0x%08x "
            "operation=%s fields=%s",
            persona,
            int(transaction) & 0xFFFFFFFF,
            transaction_name or "<missing>",
            ",".join(sorted(fields)) or "<none>",
        )
        return []

    def _account_suggest_names(
        self,
        fields: dict[str, str],
        transaction: int,
        connection: FESLConnection,
    ) -> list[FESLFrame]:
        suggestions = self._suggest_screen_names(fields)
        reply: dict[str, object] = {
            "TXN": "SuggestScreenNames",
            "names.[]": str(len(suggestions)),
        }
        for index, suggestion in enumerate(suggestions):
            reply[f"names.{index}"] = suggestion
        log.info(
            "Carbon FESL screen-name suggestions: requested=%r returned=%s",
            str(fields.get("name", "") or "").strip(),
            len(suggestions),
        )
        return [self._reply("acct", reply, transaction)]

    def _account_register_game(
        self,
        fields: dict[str, str],
        transaction: int,
        connection: FESLConnection,
    ) -> list[FESLFrame]:
        connection.registered_game = str(fields.get("game", "") or "").strip()
        connection.registered_platform = str(fields.get("platform", "") or "").strip()
        log.info(
            "Carbon FESL game registration accepted: game=%s platform=%s "
            "account_hint=%r code_present=%s policy=compatibility-only",
            connection.registered_game or "<none>",
            connection.registered_platform or "<none>",
            str(fields.get("name", "") or "").strip(),
            bool(str(fields.get("code", "") or "").strip()),
        )
        return [
            self._reply(
                "acct",
                {"TXN": "RegisterGame"},
                transaction,
            )
        ]

    def _account_add(
        self,
        fields: dict[str, str],
        transaction: int,
        connection: FESLConnection,
    ) -> list[FESLFrame]:
        account_name = str(fields.get("name", "") or "").strip()
        password = str(fields.get("password", "") or "")
        invalid_field = ""
        field_error = 0
        field_value = ""
        if len(account_name) < 3:
            invalid_field, field_error, field_value = (
                "displayName",
                2,
                "TOO_SHORT",
            )
        elif len(account_name) > 32:
            invalid_field, field_error, field_value = (
                "displayName",
                3,
                "TOO_LONG",
            )
        elif not password:
            invalid_field, field_error, field_value = (
                "password",
                2,
                "TOO_SHORT",
            )
        elif len(password) > 16:
            invalid_field, field_error, field_value = (
                "password",
                3,
                "TOO_LONG",
            )
        if invalid_field:
            return [
                self._reply(
                    "acct",
                    {
                        "TXN": "NuAddAccount",
                        "localizedMessage": (
                            '"The required parameters for this call are '
                            'missing or invalid"'
                        ),
                        "errorCode": "21",
                        "errorContainer.[]": "1",
                        "errorContainer.0.fieldName": invalid_field,
                        "errorContainer.0.fieldError": str(field_error),
                        "errorContainer.0.value": field_value,
                    },
                    transaction,
                )
            ]
        if self.credentials is None:
            log.error(
                "Carbon FESL account creation rejected: account=%r "
                "reason=credential-store-unavailable",
                account_name,
            )
            return [
                self._reply(
                    "acct",
                    {
                        "TXN": "NuAddAccount",
                        "errorCode": "120",
                        "errorContainer.[]": "0",
                    },
                    transaction,
                )
            ]
        try:
            self.credentials.create_account(
                account_name,
                password,
                persona=account_name,
                email=fields.get("email", ""),
                dob_day=fields.get("DOBDay", ""),
                dob_month=fields.get("DOBMonth", ""),
                dob_year=fields.get("DOBYear", ""),
                country_code=fields.get("countryCode", ""),
                zip_code=fields.get("zipCode", ""),
                ea_mail_flag=fields.get("eaMailFlag", ""),
                third_party_mail_flag=fields.get(
                    "thirdPartyMailFlag",
                    "",
                ),
            )
        except CredentialAccountExistsError:
            completion = getattr(
                self.credentials,
                "complete_missing_profile",
                None,
            )
            completion_result = None
            if callable(completion):
                try:
                    completion_result = completion(
                        account_name,
                        password,
                        email=fields.get("email", ""),
                        dob_day=fields.get("DOBDay", ""),
                        dob_month=fields.get("DOBMonth", ""),
                        dob_year=fields.get("DOBYear", ""),
                        country_code=fields.get("countryCode", ""),
                        zip_code=fields.get("zipCode", ""),
                        ea_mail_flag=fields.get("eaMailFlag", ""),
                        third_party_mail_flag=fields.get(
                            "thirdPartyMailFlag",
                            "",
                        ),
                    )
                except CredentialAccountExistsError:
                    completion_result = None
            if completion_result is not None and completion_result.accepted:
                log.info(
                    "Carbon FESL existing account completed: account=%r "
                    "metadata=%s",
                    completion_result.account_name or account_name,
                    completion_result.reason,
                )
                return [
                    self._reply(
                        "acct",
                        {"TXN": "NuAddAccount"},
                        transaction,
                    )
                ]
            log.warning(
                "Carbon FESL account creation rejected: account=%r "
                "reason=already-exists completion=%s",
                account_name,
                (
                    completion_result.reason
                    if completion_result is not None
                    else "unavailable"
                ),
            )
            return [
                self._reply(
                    "acct",
                    {
                        "TXN": "NuAddAccount",
                        "localizedMessage": (
                            '"That account name is already taken"'
                        ),
                        "errorCode": "160",
                        "errorContainer.[]": "0",
                    },
                    transaction,
                )
            ]
        log.info(
            "Carbon FESL account created: account=%r country=%s "
            "credential=pbkdf2",
            account_name,
            fields.get("countryCode", "") or "<none>",
        )
        # Retail's request is AddAccount, while the completion transaction
        # is named NuAddAccount in the earlier working Carbon service.
        # Login follows separately and is the only step that creates a
        # session/lkey.
        return [
            self._reply(
                "acct",
                {"TXN": "NuAddAccount"},
                transaction,
            )
        ]

    def _account_terms(
        self,
        fields: dict[str, str],
        transaction: int,
        connection: FESLConnection,
    ) -> list[FESLFrame]:
        terms = quote(
            TERMS_OF_SERVICE_TEXT,
            safe=" ,.'&/()?;[]",
        ).replace("%3A", "%3a").replace("%0A", "%0a")
        return [
            self._reply(
                "acct",
                {
                    "TXN": "GetTos",
                    "version": TERMS_OF_SERVICE_VERSION,
                    "tos": terms,
                },
                transaction,
            )
        ]

    def _account_countries(
        self,
        fields: dict[str, str],
        transaction: int,
        connection: FESLConnection,
    ) -> list[FESLFrame]:
        countries = (
            ("US", "United States"),
            ("RO", "Romania"),
            ("GB", "United Kingdom"),
            ("DE", "Germany"),
            ("FR", "France"),
            ("IT", "Italy"),
            ("ES", "Spain"),
            ("NL", "Netherlands"),
            ("CA", "Canada"),
            ("AU", "Australia"),
        )
        reply: dict[str, object] = {
            "TXN": "GetCountryList",
            "countryList.[]": str(len(countries)),
        }
        for index, (iso_code, description) in enumerate(countries):
            reply[f"countryList.{index}.ISOCode"] = iso_code
            reply[f"countryList.{index}.description"] = description
        return [self._reply("acct", reply, transaction)]

    def _account_login(
        self,
        fields: dict[str, str],
        transaction: int,
        connection: FESLConnection,
    ) -> list[FESLFrame]:
        if self.login_error_probe_code is not None:
            log.warning(
                "Carbon FESL login error probe: account=%r code=%d "
                "action=reject-without-localized-message",
                fields.get("name", ""),
                self.login_error_probe_code,
            )
            return [
                self._reply(
                    "acct",
                    {
                        "TXN": "Login",
                        "errorCode": str(self.login_error_probe_code),
                        "errorContainer.[]": "0",
                    },
                    transaction,
                )
            ]
        requested_name = fields.get("name", "")
        account_name = requested_name
        persona = requested_name
        if self.authentication_mode == "password":
            if self.credentials is None:
                result = AuthenticationResult(False, "unknown_account")
            else:
                result = self.credentials.authenticate(
                    requested_name,
                    fields.get("password", ""),
                )
            if not result.accepted:
                log.warning(
                    "Carbon FESL login rejected: account=%r reason=%s retry_after=%d",
                    requested_name,
                    result.reason,
                    result.retry_after_seconds,
                )
                return [self._login_error(result, transaction)]
            account_name = result.account_name
            persona = result.persona
        else:
            account_name = str(requested_name or "Player").strip() or "Player"
            persona = account_name
        forced_logoff_reason = ""
        if self.active_sessions is not None:
            conflict = self.active_sessions.claim(
                connection.connection_id,
                account_name,
                persona,
            )
            if conflict is not None:
                if conflict in _DUPLICATE_LOGIN_REASONS:
                    # The established lease belongs to the first client.  Give
                    # only the newcomer a temporary LKEY so Messenger can send
                    # the retail ADMN/DUPL notification to that exact client.
                    forced_logoff_reason = _DUPLICATE_LOGIN_ADMIN_TYPE
                    log.warning(
                        "Carbon FESL duplicate login staged: account=%r persona=%r "
                        "reason=%s action=temporary-lkey-for-native-dupl",
                        account_name,
                        persona,
                        conflict,
                    )
                else:
                    log.warning(
                        "Carbon FESL login rejected: account=%r persona=%r "
                        "reason=%s error_code=%d",
                        account_name,
                        persona,
                        conflict,
                        _LOGIN_ERROR_CODES.get(conflict, 101),
                    )
                    return [
                        self._login_error(
                            AuthenticationResult(False, conflict),
                            transaction,
                        )
                    ]
        old_session_key = connection.session_key
        try:
            identity, session_key = self.identities.login(
                account_name,
                persona,
                forced_logoff_reason=forced_logoff_reason,
            )
        except Exception:
            if self.active_sessions is not None:
                self.active_sessions.release(connection.connection_id)
            raise
        if old_session_key:
            self.identities.revoke_session(old_session_key)
        connection.identity = identity
        connection.session_key = session_key
        if forced_logoff_reason:
            log.warning(
                "Carbon FESL duplicate login temporary session issued: "
                "account=%r persona=%r action=await-messenger-dupl",
                account_name,
                persona,
            )
        else:
            log.info(
                "Carbon FESL login accepted: account=%r persona=%r mode=%s result=ok",
                account_name,
                persona,
                self.authentication_mode,
            )
            self.progression.bind_identity(identity)
            imported_viruses = (
                self.progression.import_viral_tokens(
                    identity,
                    self.dlc_inventory.tokens_for(identity),
                )
                if self.dlc_inventory.seed_viral_carriers
                else ()
            )
            if imported_viruses:
                log.info(
                    "Carbon trusted DLC assignment seeded viral carrier: account=%s persona=%s stats=%s",
                    identity.account_name,
                    identity.persona,
                    ",".join(imported_viruses),
                )
        reply = {
            "lkey": session_key,
            "name": identity.persona,
            "profileId": str(identity.profile_id),
            "TXN": "Login",
            "userId": str(identity.user_id),
            "displayName": identity.persona,
        }
        return [self._reply("acct", reply, transaction)]

    def _account_personas(
        self,
        fields: dict[str, str],
        transaction: int,
        connection: FESLConnection,
    ) -> list[FESLFrame]:
        return [
            self._reply(
                "acct",
                {"TXN": "NuGetPersonas", "personas.[]": "1", "personas.0": connection.identity.persona},
                transaction,
            )
        ]

    def _account_login_persona(
        self,
        fields: dict[str, str],
        transaction: int,
        connection: FESLConnection,
    ) -> list[FESLFrame]:
        identity = connection.identity
        return [
            self._reply(
                "acct",
                {
                    "TXN": "NuLoginPersona",
                    "lkey": connection.session_key,
                    "profileId": str(identity.profile_id),
                    "userId": str(identity.user_id),
                },
                transaction,
            )
        ]

    def touch(self, connection: FESLConnection) -> bool:
        if connection.identity is None or self.active_sessions is None:
            return False
        touch = getattr(self.active_sessions, "touch", None)
        return bool(touch and touch(connection.connection_id))

    def disconnect(self, connection: FESLConnection) -> None:
        if self.active_sessions is not None:
            release = getattr(self.active_sessions, "release", None)
            if release is not None:
                release(connection.connection_id)
        if connection.session_key:
            self.identities.revoke_session(connection.session_key)
        connection.identity = None
        connection.session_key = ""
