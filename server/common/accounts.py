"""Process-safe shared SQLite account storage for the NFS online services.

The module intentionally has no dependency on either the Classic or Carbon
wire adapters.  Each server wraps the records in its existing dataclasses, so
protocol code can remain stable while both processes share one durable source
of truth.
"""

from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Callable, Iterable, Iterator
from uuid import uuid4

from common.enforcement import append_account_policy_event


PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 210_000
SCHEMA_VERSION = 4
MAX_CARBON_WIRE_PLAYER_ID = 0x7FFF


class SharedAccountError(ValueError):
    """Base class for durable account-store errors."""


class SharedAccountExistsError(SharedAccountError):
    """Raised when an account identifier is already owned."""


class SharedPersonaExistsError(SharedAccountError):
    """Raised when a globally unique persona name is already owned."""


@dataclass(frozen=True)
class SharedAccountRecord:
    account_id: int
    account_uuid: str
    account_name: str
    primary_persona: str
    password_hash: str
    enabled: bool
    banned: bool
    email: str
    aliases: tuple[str, ...]
    personas: tuple[str, ...]
    dob_day: str = ""
    dob_month: str = ""
    dob_year: str = ""
    country_code: str = ""
    zip_code: str = ""
    ea_mail_flag: str = ""
    third_party_mail_flag: str = ""


@dataclass(frozen=True)
class SharedIdentityRecord:
    account_id: int
    account_uuid: str
    account_name: str
    persona_id: int
    persona_uuid: str
    persona: str
    profile_id: int
    user_id: int
    carbon_wire_player_id: int | None = None


@dataclass(frozen=True)
class SharedAuthenticationResult:
    accepted: bool
    reason: str
    account_name: str = ""
    persona: str = ""
    retry_after_seconds: int = 0


@dataclass(frozen=True)
class SharedSessionRecord:
    account_name: str
    persona: str
    game: str
    connection_id: str
    server_id: str
    heartbeat_at: float
    expires_at: float


class SQLiteAccountDatabase:
    """One process-safe account database shared by U2, MW and Carbon.

    A fresh SQLite connection is opened for each public operation.  This keeps
    usage safe across both threads and the separate Classic/Carbon processes.
    WAL permits concurrent readers while writes remain short transactions.
    """

    def __init__(
        self,
        path: str | Path,
        user_root: str | Path,
        *,
        auto_enroll: bool = False,
        failure_limit: int = 5,
        lockout_seconds: float = 300.0,
        busy_timeout_ms: int = 5_000,
        clock: Callable[[], float] | None = None,
        salt_factory: Callable[[int], bytes] | None = None,
    ) -> None:
        if int(failure_limit) < 0:
            raise ValueError("failure_limit must be zero or positive")
        if float(lockout_seconds) < 0:
            raise ValueError("lockout_seconds must be zero or positive")
        if int(busy_timeout_ms) < 0:
            raise ValueError("busy_timeout_ms must be zero or positive")
        self.path = Path(path)
        self.user_root = Path(user_root)
        self.auto_enroll = bool(auto_enroll)
        self.failure_limit = int(failure_limit)
        self.lockout_seconds = float(lockout_seconds)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._clock = clock or time.time
        self._salt_factory = salt_factory or os.urandom
        self._dummy_password_hash = self.encode_password(
            "invalid-account-password",
            salt=b"\x00" * 16,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.user_root.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @staticmethod
    def normalize(value: object) -> str:
        return str(value or "").strip().casefold()

    @staticmethod
    def clean(value: object) -> str:
        return str(value or "").strip()

    @classmethod
    def clean_values(cls, values: object) -> tuple[str, ...]:
        if values is None:
            return ()
        if isinstance(values, str):
            source: Iterable[object] = values.replace(";", ",").split(",")
        elif isinstance(values, (tuple, list, set, frozenset)):
            source = values
        else:
            source = (values,)
        result: list[str] = []
        seen: set[str] = set()
        for raw in source:
            value = cls.clean(raw)
            key = cls.normalize(value)
            if value and key not in seen:
                seen.add(key)
                result.append(value)
        return tuple(result)

    @classmethod
    def encode_password(
        cls,
        password: str,
        *,
        iterations: int = PBKDF2_ITERATIONS,
        salt: bytes | None = None,
        salt_factory: Callable[[int], bytes] | None = None,
    ) -> str:
        if int(iterations) <= 0:
            raise ValueError("PBKDF2 iterations must be positive")
        actual_salt = salt if salt is not None else (salt_factory or os.urandom)(16)
        if not actual_salt:
            raise ValueError("password salt must not be empty")
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            str(password or "").encode("utf-8"),
            actual_salt,
            int(iterations),
        )
        return "$".join(
            (
                PBKDF2_ALGORITHM,
                str(int(iterations)),
                base64.b64encode(actual_salt).decode("ascii"),
                base64.b64encode(digest).decode("ascii"),
            )
        )

    @staticmethod
    def verify_password(password: str, encoded: str) -> bool:
        try:
            algorithm, iteration_text, salt_text, digest_text = str(encoded or "").split("$", 3)
            if algorithm != PBKDF2_ALGORITHM:
                return False
            iterations = int(iteration_text)
            if iterations <= 0 or iterations > 10_000_000:
                return False
            salt = base64.b64decode(salt_text.encode("ascii"), validate=True)
            expected = base64.b64decode(digest_text.encode("ascii"), validate=True)
            actual = hashlib.pbkdf2_hmac(
                "sha256",
                str(password or "").encode("utf-8"),
                salt,
                iterations,
            )
            return len(actual) == len(expected) and hmac.compare_digest(actual, expected)
        except (TypeError, ValueError, UnicodeError, binascii.Error):
            return False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=max(0.0, self.busy_timeout_ms / 1000.0),
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _enable_wal(connection: sqlite3.Connection) -> None:
        current = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        if current.casefold() == "wal":
            return
        for attempt in range(20):
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).casefold() or attempt == 19:
                    raise
                time.sleep(min(0.01 * (attempt + 1), 0.1))

    def _initialize(self) -> None:
        with self.connect() as connection:
            self._enable_wal(connection)
            connection.execute("PRAGMA synchronous=NORMAL")
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise ValueError(
                    f"account database schema {current} is newer than supported {SCHEMA_VERSION}"
                )
            connection.executescript(
                """
                BEGIN IMMEDIATE;

                CREATE TABLE IF NOT EXISTS accounts (
                    account_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_uuid TEXT NOT NULL UNIQUE,
                    account_name TEXT NOT NULL,
                    account_name_key TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL DEFAULT '',
                    email_key TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                    banned INTEGER NOT NULL DEFAULT 0 CHECK(banned IN (0, 1)),
                    dob_day TEXT NOT NULL DEFAULT '',
                    dob_month TEXT NOT NULL DEFAULT '',
                    dob_year TEXT NOT NULL DEFAULT '',
                    country_code TEXT NOT NULL DEFAULT '',
                    zip_code TEXT NOT NULL DEFAULT '',
                    ea_mail_flag TEXT NOT NULL DEFAULT '',
                    third_party_mail_flag TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS accounts_email_key_unique
                    ON accounts(email_key) WHERE email_key <> '';

                CREATE TABLE IF NOT EXISTS login_aliases (
                    alias_key TEXT PRIMARY KEY,
                    alias TEXT NOT NULL,
                    account_id INTEGER NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS personas (
                    persona_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    persona_uuid TEXT NOT NULL UNIQUE,
                    account_id INTEGER NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                    display_name TEXT NOT NULL,
                    display_name_key TEXT NOT NULL UNIQUE,
                    profile_id INTEGER NOT NULL UNIQUE,
                    carbon_wire_player_id INTEGER UNIQUE,
                    is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0, 1)),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS personas_one_primary
                    ON personas(account_id) WHERE is_primary = 1;
                CREATE INDEX IF NOT EXISTS personas_account_idx ON personas(account_id);

                CREATE TABLE IF NOT EXISTS active_sessions (
                    account_id INTEGER PRIMARY KEY REFERENCES accounts(account_id) ON DELETE CASCADE,
                    persona_id INTEGER NOT NULL UNIQUE REFERENCES personas(persona_id) ON DELETE CASCADE,
                    game TEXT NOT NULL,
                    connection_id TEXT NOT NULL UNIQUE,
                    session_token TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    connected_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS active_sessions_expiry_idx ON active_sessions(expires_at);

                CREATE TABLE IF NOT EXISTS account_policy_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL
                        REFERENCES accounts(account_id) ON DELETE CASCADE,
                    account_name TEXT NOT NULL,
                    action TEXT NOT NULL
                        CHECK(action IN ('ban', 'unban', 'disable', 'enable', 'kick')),
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS account_policy_events_account_idx
                    ON account_policy_events(account_id, event_id);

                CREATE TABLE IF NOT EXISTS auth_failures (
                    identifier_key TEXT PRIMARY KEY,
                    failures INTEGER NOT NULL DEFAULT 0,
                    locked_until REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS blocked_emails (
                    email_key TEXT PRIMARY KEY,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS game_entitlements (
                    persona_id INTEGER NOT NULL REFERENCES personas(persona_id) ON DELETE CASCADE,
                    game TEXT NOT NULL,
                    token TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'server',
                    granted_at REAL NOT NULL,
                    PRIMARY KEY(persona_id, game, token)
                );

                CREATE TABLE IF NOT EXISTS social_relations (
                    source_persona_id INTEGER NOT NULL REFERENCES personas(persona_id) ON DELETE CASCADE,
                    target_persona_id INTEGER NOT NULL REFERENCES personas(persona_id) ON DELETE CASCADE,
                    relation TEXT NOT NULL CHECK(relation IN ('friend', 'pending', 'blocked')),
                    created_at REAL NOT NULL,
                    PRIMARY KEY(source_persona_id, target_persona_id, relation),
                    CHECK(source_persona_id <> target_persona_id)
                );

                CREATE TABLE IF NOT EXISTS assets (
                    asset_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_uuid TEXT NOT NULL UNIQUE,
                    persona_id INTEGER NOT NULL REFERENCES personas(persona_id) ON DELETE CASCADE,
                    game TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    wire_id INTEGER,
                    relative_path TEXT NOT NULL UNIQUE,
                    byte_size INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(game, wire_id)
                );

                CREATE TABLE IF NOT EXISTS game_player_stats (
                    persona_id INTEGER NOT NULL
                        REFERENCES personas(persona_id) ON DELETE CASCADE,
                    game TEXT NOT NULL,
                    category INTEGER NOT NULL CHECK(category BETWEEN 0 AND 31),
                    wins INTEGER NOT NULL DEFAULT 0 CHECK(wins >= 0),
                    losses INTEGER NOT NULL DEFAULT 0 CHECK(losses >= 0),
                    disconnects INTEGER NOT NULL DEFAULT 0 CHECK(disconnects >= 0),
                    skill INTEGER NOT NULL DEFAULT 100 CHECK(skill >= 0),
                    opponents_skill INTEGER NOT NULL DEFAULT 101
                        CHECK(opponents_skill >= 0),
                    opponents_rank INTEGER NOT NULL DEFAULT 101
                        CHECK(opponents_rank >= 0),
                    metric_total REAL NOT NULL DEFAULT 0 CHECK(metric_total >= 0),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(persona_id, game, category)
                );
                CREATE INDEX IF NOT EXISTS game_player_stats_board_idx
                    ON game_player_stats(
                        game, category, skill DESC, wins DESC,
                        losses ASC, disconnects ASC, persona_id
                    );

                CREATE TABLE IF NOT EXISTS game_leaderboard_visibility (
                    persona_id INTEGER NOT NULL
                        REFERENCES personas(persona_id) ON DELETE CASCADE,
                    game TEXT NOT NULL,
                    hidden INTEGER NOT NULL DEFAULT 0 CHECK(hidden IN (0, 1)),
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(persona_id, game)
                );

                CREATE TABLE IF NOT EXISTS game_races (
                    race_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game TEXT NOT NULL,
                    server_run_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    category INTEGER NOT NULL DEFAULT 0,
                    ranked INTEGER NOT NULL DEFAULT 1 CHECK(ranked IN (0, 1)),
                    track TEXT NOT NULL DEFAULT '',
                    direction INTEGER NOT NULL DEFAULT 0,
                    laps INTEGER NOT NULL DEFAULT 0 CHECK(laps >= 0),
                    status TEXT NOT NULL DEFAULT 'reported',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(game, server_run_id, session_id)
                );
                CREATE INDEX IF NOT EXISTS game_races_lookup_idx
                    ON game_races(game, created_at DESC);

                CREATE TABLE IF NOT EXISTS game_race_results (
                    race_id INTEGER NOT NULL
                        REFERENCES game_races(race_id) ON DELETE CASCADE,
                    persona_id INTEGER NOT NULL
                        REFERENCES personas(persona_id) ON DELETE CASCADE,
                    reporter_key TEXT NOT NULL,
                    category INTEGER NOT NULL DEFAULT 0
                        CHECK(category BETWEEN 0 AND 31),
                    outcome TEXT NOT NULL,
                    place INTEGER NOT NULL DEFAULT 0 CHECK(place >= 0),
                    disconnected INTEGER NOT NULL DEFAULT 0
                        CHECK(disconnected IN (0, 1)),
                    elapsed_ms INTEGER NOT NULL DEFAULT 0 CHECK(elapsed_ms >= 0),
                    best_lap_ms INTEGER NOT NULL DEFAULT 0 CHECK(best_lap_ms >= 0),
                    best_drift INTEGER NOT NULL DEFAULT 0 CHECK(best_drift >= 0),
                    nos_used REAL NOT NULL DEFAULT 0 CHECK(nos_used >= 0),
                    source TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    aggregate_applied INTEGER NOT NULL DEFAULT 0
                        CHECK(aggregate_applied IN (0, 1)),
                    reported_at REAL NOT NULL,
                    PRIMARY KEY(race_id, persona_id)
                );
                CREATE INDEX IF NOT EXISTS game_race_results_persona_idx
                    ON game_race_results(persona_id, reported_at DESC);
                CREATE TABLE IF NOT EXISTS legacy_imports (
                    source_path TEXT PRIMARY KEY,
                    source_sha256 TEXT NOT NULL,
                    rows_imported INTEGER NOT NULL DEFAULT 0
                        CHECK(rows_imported >= 0),
                    imported_at REAL NOT NULL
                );
                """
            )
            result_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(game_race_results)"
                ).fetchall()
            }
            if "reporter_key" not in result_columns:
                connection.execute(
                    """
                    ALTER TABLE game_race_results
                    ADD COLUMN reporter_key TEXT NOT NULL DEFAULT ''
                    """
                )
                connection.execute(
                    """
                    UPDATE game_race_results
                       SET reporter_key=CAST(persona_id AS TEXT)
                     WHERE reporter_key=''
                    """
                )
            if "category" not in result_columns:
                connection.execute(
                    """
                    ALTER TABLE game_race_results
                    ADD COLUMN category INTEGER NOT NULL DEFAULT 0
                        CHECK(category BETWEEN 0 AND 31)
                    """
                )
                connection.execute(
                    """
                    UPDATE game_race_results
                       SET category=COALESCE(
                           (
                               SELECT r.category
                                 FROM game_races AS r
                                WHERE r.race_id=game_race_results.race_id
                           ),
                           0
                       )
                    """
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS game_race_results_reporter_idx
                    ON game_race_results(race_id, reporter_key)
                """
            )
            if current < 4:
                policy_schema = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='account_policy_events'"
                ).fetchone()
                policy_sql = str(policy_schema["sql"] if policy_schema is not None else "")
                if "'kick'" not in policy_sql.casefold():
                    connection.execute(
                        "DROP INDEX IF EXISTS account_policy_events_account_idx"
                    )
                    connection.execute(
                        "ALTER TABLE account_policy_events RENAME TO account_policy_events_v3"
                    )
                    connection.execute(
                        """
                        CREATE TABLE account_policy_events (
                            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            account_id INTEGER NOT NULL
                                REFERENCES accounts(account_id) ON DELETE CASCADE,
                            account_name TEXT NOT NULL,
                            action TEXT NOT NULL
                                CHECK(action IN ('ban', 'unban', 'disable', 'enable', 'kick')),
                            created_at REAL NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO account_policy_events(
                            event_id, account_id, account_name, action, created_at
                        )
                        SELECT event_id, account_id, account_name, action, created_at
                          FROM account_policy_events_v3
                         ORDER BY event_id
                        """
                    )
                    connection.execute("DROP TABLE account_policy_events_v3")
                    connection.execute(
                        """
                        CREATE INDEX account_policy_events_account_idx
                            ON account_policy_events(account_id, event_id)
                        """
                    )
            if current < SCHEMA_VERSION:
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()

    @staticmethod
    def _profile_candidate(persona: str) -> int:
        normalized = str(persona or "Player").strip().casefold()
        raw = int.from_bytes(hashlib.md5(normalized.encode("utf-8")).digest()[:4], "big")
        return 100_000_000 + (raw % 900_000_000)

    @staticmethod
    def _wire_candidate(persona: str) -> int:
        key = str(persona or "Player").strip().casefold() or "player"
        raw = hashlib.md5(key.encode("utf-8")).digest()
        return 1 + (int.from_bytes(raw[4:6], "big") % MAX_CARBON_WIRE_PLAYER_ID)

    def _allocate_profile_id(self, connection: sqlite3.Connection, persona: str) -> int:
        candidate = self._profile_candidate(persona)
        for _ in range(900_000_000):
            owner = connection.execute(
                "SELECT persona_id FROM personas WHERE profile_id=?",
                (candidate,),
            ).fetchone()
            if owner is None:
                return candidate
            candidate = 100_000_000 if candidate >= 999_999_999 else candidate + 1
        raise RuntimeError("profile id namespace exhausted")

    def _allocate_wire_id(self, connection: sqlite3.Connection, persona: str) -> int:
        candidate = self._wire_candidate(persona)
        for _ in range(MAX_CARBON_WIRE_PLAYER_ID):
            owner = connection.execute(
                "SELECT persona_id FROM personas WHERE carbon_wire_player_id=?",
                (candidate,),
            ).fetchone()
            if owner is None:
                return candidate
            candidate = 1 if candidate >= MAX_CARBON_WIRE_PLAYER_ID else candidate + 1
        raise RuntimeError("Carbon wire player id namespace exhausted")

    def _account_row(
        self,
        connection: sqlite3.Connection,
        identifier: object,
    ) -> sqlite3.Row | None:
        key = self.normalize(identifier)
        if not key:
            return None
        return connection.execute(
            """
            SELECT DISTINCT a.*
              FROM accounts AS a
              LEFT JOIN login_aliases AS l ON l.account_id = a.account_id
             WHERE a.account_name_key=? OR a.email_key=? OR l.alias_key=?
             LIMIT 1
            """,
            (key, key, key),
        ).fetchone()

    def _primary_persona_row(
        self,
        connection: sqlite3.Connection,
        account_id: int,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM personas
             WHERE account_id=?
             ORDER BY is_primary DESC, persona_id ASC
             LIMIT 1
            """,
            (int(account_id),),
        ).fetchone()
        if row is None:
            raise ValueError(f"account {account_id} has no persona")
        return row

    def _record(self, connection: sqlite3.Connection, row: sqlite3.Row) -> SharedAccountRecord:
        account_id = int(row["account_id"])
        persona_rows = connection.execute(
            """
            SELECT display_name, is_primary FROM personas
             WHERE account_id=?
             ORDER BY is_primary DESC, persona_id ASC
            """,
            (account_id,),
        ).fetchall()
        personas = tuple(str(item["display_name"]) for item in persona_rows)
        if not personas:
            raise ValueError(f"account {account_id} has no persona")
        primary = next(
            (str(item["display_name"]) for item in persona_rows if int(item["is_primary"])),
            personas[0],
        )
        aliases = tuple(
            str(item["alias"])
            for item in connection.execute(
                "SELECT alias FROM login_aliases WHERE account_id=? ORDER BY alias_key",
                (account_id,),
            ).fetchall()
        )
        return SharedAccountRecord(
            account_id=account_id,
            account_uuid=str(row["account_uuid"]),
            account_name=str(row["account_name"]),
            primary_persona=primary,
            password_hash=str(row["password_hash"]),
            enabled=bool(row["enabled"]),
            banned=bool(row["banned"]),
            email=str(row["email"]),
            aliases=aliases,
            personas=personas,
            dob_day=str(row["dob_day"]),
            dob_month=str(row["dob_month"]),
            dob_year=str(row["dob_year"]),
            country_code=str(row["country_code"]),
            zip_code=str(row["zip_code"]),
            ea_mail_flag=str(row["ea_mail_flag"]),
            third_party_mail_flag=str(row["third_party_mail_flag"]),
        )

    def _ensure_identifier_available(
        self,
        connection: sqlite3.Connection,
        value: object,
        *,
        allowed_account_id: int | None = None,
    ) -> None:
        key = self.normalize(value)
        if not key:
            return
        row = self._account_row(connection, value)
        if row is not None and int(row["account_id"]) != int(allowed_account_id or -1):
            raise SharedAccountExistsError(f"account identifier already exists: {value}")

    def _ensure_persona_available(
        self,
        connection: sqlite3.Connection,
        value: object,
        *,
        allowed_account_id: int | None = None,
    ) -> None:
        key = self.normalize(value)
        if not key:
            raise ValueError("persona is required")
        row = connection.execute(
            "SELECT account_id FROM personas WHERE display_name_key=?",
            (key,),
        ).fetchone()
        if row is not None and int(row["account_id"]) != int(allowed_account_id or -1):
            raise SharedPersonaExistsError(f"persona already exists: {value}")

    def create_account(
        self,
        account_name: str,
        password: str,
        *,
        persona: str | None = None,
        email: str = "",
        aliases: Iterable[str] = (),
        personas: Iterable[str] = (),
        replace: bool = False,
        dob_day: str = "",
        dob_month: str = "",
        dob_year: str = "",
        country_code: str = "",
        zip_code: str = "",
        ea_mail_flag: str = "",
        third_party_mail_flag: str = "",
    ) -> SharedAccountRecord:
        name = self.clean(account_name)
        secret = str(password or "")
        if not name:
            raise ValueError("account name is required")
        if not secret:
            raise ValueError("password is required")
        primary = self.clean(persona) or name
        persona_values = self.clean_values((primary, *tuple(personas)))
        alias_values = self.clean_values(aliases)
        email_value = self.clean(email)
        now = float(self._clock())
        encoded = self.encode_password(secret, salt_factory=self._salt_factory)

        with self.transaction() as connection:
            existing = self._account_row(connection, name)
            if existing is not None and not replace:
                raise SharedAccountExistsError(f"account already exists: {name}")
            account_id = int(existing["account_id"]) if existing is not None else None
            self._ensure_identifier_available(
                connection,
                email_value,
                allowed_account_id=account_id,
            )
            for alias in alias_values:
                self._ensure_identifier_available(
                    connection,
                    alias,
                    allowed_account_id=account_id,
                )
            for value in persona_values:
                self._ensure_persona_available(
                    connection,
                    value,
                    allowed_account_id=account_id,
                )

            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO accounts(
                        account_uuid, account_name, account_name_key,
                        email, email_key, password_hash, enabled, banned,
                        dob_day, dob_month, dob_year, country_code, zip_code,
                        ea_mail_flag, third_party_mail_flag, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid4().hex,
                        name,
                        self.normalize(name),
                        email_value,
                        self.normalize(email_value),
                        encoded,
                        self.clean(dob_day),
                        self.clean(dob_month),
                        self.clean(dob_year),
                        self.clean(country_code),
                        self.clean(zip_code),
                        self.clean(ea_mail_flag),
                        self.clean(third_party_mail_flag),
                        now,
                        now,
                    ),
                )
                account_id = int(cursor.lastrowid)
            else:
                assert account_id is not None
                connection.execute(
                    """
                    UPDATE accounts
                       SET account_name=?, account_name_key=?, email=?, email_key=?,
                           password_hash=?, enabled=1,
                           dob_day=?, dob_month=?, dob_year=?, country_code=?,
                           zip_code=?, ea_mail_flag=?, third_party_mail_flag=?,
                           updated_at=?
                     WHERE account_id=?
                    """,
                    (
                        name,
                        self.normalize(name),
                        email_value,
                        self.normalize(email_value),
                        encoded,
                        self.clean(dob_day),
                        self.clean(dob_month),
                        self.clean(dob_year),
                        self.clean(country_code),
                        self.clean(zip_code),
                        self.clean(ea_mail_flag),
                        self.clean(third_party_mail_flag),
                        now,
                        account_id,
                    ),
                )
                connection.execute("DELETE FROM login_aliases WHERE account_id=?", (account_id,))

            assert account_id is not None
            existing_personas = {
                str(row["display_name_key"]): row
                for row in connection.execute(
                    "SELECT * FROM personas WHERE account_id=?",
                    (account_id,),
                ).fetchall()
            }
            connection.execute("UPDATE personas SET is_primary=0 WHERE account_id=?", (account_id,))
            for index, display in enumerate(persona_values):
                key = self.normalize(display)
                current = existing_personas.get(key)
                if current is None:
                    connection.execute(
                        """
                        INSERT INTO personas(
                            persona_uuid, account_id, display_name, display_name_key,
                            profile_id, carbon_wire_player_id, is_primary,
                            created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, NULL, ?, ?, ?)
                        """,
                        (
                            uuid4().hex,
                            account_id,
                            display,
                            key,
                            self._allocate_profile_id(connection, display),
                            1 if index == 0 else 0,
                            now,
                            now,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE personas SET display_name=?, is_primary=?, updated_at=?
                         WHERE persona_id=?
                        """,
                        (display, 1 if index == 0 else 0, now, int(current["persona_id"])),
                    )
            if replace:
                keep = tuple(self.normalize(item) for item in persona_values)
                placeholders = ",".join("?" for _ in keep)
                if keep:
                    connection.execute(
                        f"DELETE FROM personas WHERE account_id=? AND display_name_key NOT IN ({placeholders})",
                        (account_id, *keep),
                    )

            for alias in alias_values:
                key = self.normalize(alias)
                if key in {self.normalize(name), self.normalize(email_value)}:
                    continue
                connection.execute(
                    "INSERT INTO login_aliases(alias_key, alias, account_id, created_at) VALUES(?, ?, ?, ?)",
                    (key, alias, account_id, now),
                )
            row = connection.execute(
                "SELECT * FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            assert row is not None
            record = self._record(connection, row)
            connection.execute(
                "DELETE FROM auth_failures WHERE identifier_key IN (?, ?, ?)",
                (
                    self.normalize(name),
                    self.normalize(email_value),
                    self.normalize(primary),
                ),
            )
        self.ensure_directories(record)
        return record

    def screen_name_available(self, value: object) -> bool:
        key = self.normalize(value)
        if not key:
            return False
        with self.connect() as connection:
            if self._account_row(connection, value) is not None:
                return False
            row = connection.execute(
                "SELECT 1 FROM personas WHERE display_name_key=? LIMIT 1",
                (key,),
            ).fetchone()
            return row is None

    def complete_missing_profile(
        self,
        identifier: str,
        password: str,
        *,
        email: str = "",
        dob_day: str = "",
        dob_month: str = "",
        dob_year: str = "",
        country_code: str = "",
        zip_code: str = "",
        ea_mail_flag: str = "",
        third_party_mail_flag: str = "",
    ) -> SharedAuthenticationResult:
        name = self.clean(identifier)
        secret = str(password or "")
        if not name:
            return SharedAuthenticationResult(False, "missing_name")
        if not secret:
            return SharedAuthenticationResult(False, "missing_password")
        key = self.normalize(name)
        now = float(self._clock())
        with self.transaction() as connection:
            locked = self._failure_state(connection, key, now)
            if locked is not None:
                return locked
            row = self._account_row(connection, name)
            if row is None:
                self.verify_password(secret, self._dummy_password_hash)
                result = self._note_failure(connection, key, now)
                return result or SharedAuthenticationResult(False, "unknown_account")
            email_blocked = bool(row["email_key"]) and connection.execute(
                "SELECT 1 FROM blocked_emails WHERE email_key=?",
                (str(row["email_key"]),),
            ).fetchone() is not None
            if bool(row["banned"]) or email_blocked:
                return SharedAuthenticationResult(False, "banned")
            if not bool(row["enabled"]):
                return SharedAuthenticationResult(False, "disabled")
            if not self.verify_password(secret, str(row["password_hash"])):
                result = self._note_failure(connection, key, now)
                return result or SharedAuthenticationResult(False, "bad_password")

            account_id = int(row["account_id"])
            incoming = {
                "email": self.clean(email),
                "dob_day": self.clean(dob_day),
                "dob_month": self.clean(dob_month),
                "dob_year": self.clean(dob_year),
                "country_code": self.clean(country_code),
                "zip_code": self.clean(zip_code),
                "ea_mail_flag": self.clean(ea_mail_flag),
                "third_party_mail_flag": self.clean(third_party_mail_flag),
            }
            updates: dict[str, str | float] = {}
            email_value = incoming.pop("email")
            if email_value and not str(row["email"]):
                self._ensure_identifier_available(
                    connection,
                    email_value,
                    allowed_account_id=account_id,
                )
                updates["email"] = email_value
                updates["email_key"] = self.normalize(email_value)
            for column, value in incoming.items():
                if value and not str(row[column]):
                    updates[column] = value
            if updates:
                updates["updated_at"] = now
                assignments = ", ".join(f"{column}=?" for column in updates)
                connection.execute(
                    f"UPDATE accounts SET {assignments} WHERE account_id=?",
                    (*updates.values(), account_id),
                )
            connection.execute("DELETE FROM auth_failures WHERE identifier_key=?", (key,))
            current = connection.execute(
                "SELECT * FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            assert current is not None
            record = self._record(connection, current)
            return SharedAuthenticationResult(
                True,
                "profile_completed" if updates else "ok",
                record.account_name,
                record.primary_persona,
            )

    def resolve_account(self, identifier: object) -> SharedAccountRecord | None:
        with self.connect() as connection:
            row = self._account_row(connection, identifier)
            return self._record(connection, row) if row is not None else None

    def accounts(self) -> tuple[SharedAccountRecord, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM accounts ORDER BY account_name_key"
            ).fetchall()
            return tuple(self._record(connection, row) for row in rows)

    def _require_account(
        self,
        connection: sqlite3.Connection,
        identifier: object,
    ) -> sqlite3.Row:
        row = self._account_row(connection, identifier)
        if row is None:
            raise KeyError(str(identifier))
        return row

    def set_password(self, identifier: str, password: str) -> SharedAccountRecord:
        if not str(password or ""):
            raise ValueError("password is required")
        with self.transaction() as connection:
            row = self._require_account(connection, identifier)
            connection.execute(
                "UPDATE accounts SET password_hash=?, updated_at=? WHERE account_id=?",
                (
                    self.encode_password(str(password), salt_factory=self._salt_factory),
                    float(self._clock()),
                    int(row["account_id"]),
                ),
            )
            connection.execute(
                "DELETE FROM auth_failures WHERE identifier_key=?",
                (self.normalize(identifier),),
            )
            updated = connection.execute(
                "SELECT * FROM accounts WHERE account_id=?",
                (int(row["account_id"]),),
            ).fetchone()
            assert updated is not None
            return self._record(connection, updated)

    def set_enabled(self, identifier: str, enabled: bool) -> SharedAccountRecord:
        desired = bool(enabled)
        with self.transaction() as connection:
            row = self._require_account(connection, identifier)
            account_id = int(row["account_id"])
            changed = bool(row["enabled"]) != desired
            if changed:
                now = float(self._clock())
                connection.execute(
                    "UPDATE accounts SET enabled=?, updated_at=? WHERE account_id=?",
                    (1 if desired else 0, now, account_id),
                )
                append_account_policy_event(
                    connection,
                    account_id=account_id,
                    account_name=str(row["account_name"]),
                    action="enable" if desired else "disable",
                    created_at=now,
                )
            if not desired:
                connection.execute(
                    "DELETE FROM active_sessions WHERE account_id=?",
                    (account_id,),
                )
            updated = connection.execute(
                "SELECT * FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            assert updated is not None
            return self._record(connection, updated)

    def set_banned(self, identifier: str, banned: bool) -> SharedAccountRecord:
        desired = bool(banned)
        with self.transaction() as connection:
            row = self._require_account(connection, identifier)
            account_id = int(row["account_id"])
            changed = bool(row["banned"]) != desired
            if changed:
                now = float(self._clock())
                connection.execute(
                    "UPDATE accounts SET banned=?, updated_at=? WHERE account_id=?",
                    (1 if desired else 0, now, account_id),
                )
                append_account_policy_event(
                    connection,
                    account_id=account_id,
                    account_name=str(row["account_name"]),
                    action="ban" if desired else "unban",
                    created_at=now,
                )
            if desired:
                connection.execute(
                    "DELETE FROM active_sessions WHERE account_id=?",
                    (account_id,),
                )
            updated = connection.execute(
                "SELECT * FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            assert updated is not None
            return self._record(connection, updated)

    def kick(self, identifier: str) -> SharedAccountRecord:
        """Disconnect an account now without changing its persistent policy."""

        with self.transaction() as connection:
            row = self._require_account(connection, identifier)
            account_id = int(row["account_id"])
            now = float(self._clock())
            connection.execute(
                "DELETE FROM active_sessions WHERE account_id=?",
                (account_id,),
            )
            append_account_policy_event(
                connection,
                account_id=account_id,
                account_name=str(row["account_name"]),
                action="kick",
                created_at=now,
            )
            current = connection.execute(
                "SELECT * FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            assert current is not None
            return self._record(connection, current)

    def add_alias(self, identifier: str, alias: str) -> SharedAccountRecord:
        value = self.clean(alias)
        if not value:
            raise ValueError("alias is required")
        with self.transaction() as connection:
            row = self._require_account(connection, identifier)
            account_id = int(row["account_id"])
            self._ensure_identifier_available(connection, value, allowed_account_id=account_id)
            connection.execute(
                "INSERT OR REPLACE INTO login_aliases(alias_key, alias, account_id, created_at) VALUES(?, ?, ?, ?)",
                (self.normalize(value), value, account_id, float(self._clock())),
            )
            return self._record(connection, row)

    def add_persona(self, identifier: str, persona: str) -> SharedAccountRecord:
        value = self.clean(persona)
        if not value:
            raise ValueError("persona is required")
        with self.transaction() as connection:
            row = self._require_account(connection, identifier)
            account_id = int(row["account_id"])
            self._ensure_persona_available(connection, value, allowed_account_id=account_id)
            existing = connection.execute(
                "SELECT persona_id FROM personas WHERE account_id=? AND display_name_key=?",
                (account_id, self.normalize(value)),
            ).fetchone()
            if existing is None:
                now = float(self._clock())
                connection.execute(
                    """
                    INSERT INTO personas(
                        persona_uuid, account_id, display_name, display_name_key,
                        profile_id, carbon_wire_player_id, is_primary,
                        created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, NULL, 0, ?, ?)
                    """,
                    (
                        uuid4().hex,
                        account_id,
                        value,
                        self.normalize(value),
                        self._allocate_profile_id(connection, value),
                        now,
                        now,
                    ),
                )
            updated = connection.execute(
                "SELECT * FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            assert updated is not None
            record = self._record(connection, updated)
        self.ensure_directories(record)
        return record

    def remove_persona(self, identifier: str, persona: str) -> SharedAccountRecord:
        key = self.normalize(persona)
        if not key:
            raise ValueError("persona is required")
        with self.transaction() as connection:
            row = self._require_account(connection, identifier)
            account_id = int(row["account_id"])
            personas = connection.execute(
                "SELECT * FROM personas WHERE account_id=? ORDER BY is_primary DESC, persona_id",
                (account_id,),
            ).fetchall()
            target = next((item for item in personas if str(item["display_name_key"]) == key), None)
            if target is None:
                raise KeyError(str(persona))
            if len(personas) <= 1:
                raise ValueError("an account must retain at least one persona")
            was_primary = bool(target["is_primary"])
            connection.execute("DELETE FROM personas WHERE persona_id=?", (int(target["persona_id"]),))
            if was_primary:
                replacement = connection.execute(
                    "SELECT persona_id FROM personas WHERE account_id=? ORDER BY persona_id LIMIT 1",
                    (account_id,),
                ).fetchone()
                assert replacement is not None
                connection.execute(
                    "UPDATE personas SET is_primary=1, updated_at=? WHERE persona_id=?",
                    (float(self._clock()), int(replacement["persona_id"])),
                )
            updated = connection.execute(
                "SELECT * FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            assert updated is not None
            return self._record(connection, updated)

    def set_email_blocked(self, email: str, blocked: bool) -> str:
        key = self.normalize(email)
        if not key or "@" not in key:
            raise ValueError("valid email address is required")
        with self.transaction() as connection:
            if blocked:
                connection.execute(
                    "INSERT OR IGNORE INTO blocked_emails(email_key, created_at) VALUES(?, ?)",
                    (key, float(self._clock())),
                )
                connection.execute(
                    "DELETE FROM active_sessions WHERE account_id IN (SELECT account_id FROM accounts WHERE email_key=?)",
                    (key,),
                )
            else:
                connection.execute("DELETE FROM blocked_emails WHERE email_key=?", (key,))
        return key

    def is_email_blocked(self, email: str) -> bool:
        key = self.normalize(email)
        if not key:
            return False
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM blocked_emails WHERE email_key=?",
                (key,),
            ).fetchone() is not None

    def blocked_emails(self) -> tuple[str, ...]:
        with self.connect() as connection:
            return tuple(
                str(row["email_key"])
                for row in connection.execute(
                    "SELECT email_key FROM blocked_emails ORDER BY email_key"
                ).fetchall()
            )

    def _failure_state(
        self,
        connection: sqlite3.Connection,
        key: str,
        now: float,
    ) -> SharedAuthenticationResult | None:
        row = connection.execute(
            "SELECT failures, locked_until FROM auth_failures WHERE identifier_key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        locked_until = float(row["locked_until"])
        if locked_until <= now:
            if locked_until > 0:
                connection.execute("DELETE FROM auth_failures WHERE identifier_key=?", (key,))
            return None
        return SharedAuthenticationResult(
            False,
            "locked",
            retry_after_seconds=max(1, int(locked_until - now + 0.999)),
        )

    def _note_failure(
        self,
        connection: sqlite3.Connection,
        key: str,
        now: float,
    ) -> SharedAuthenticationResult | None:
        if self.failure_limit <= 0 or self.lockout_seconds <= 0:
            return None
        row = connection.execute(
            "SELECT failures FROM auth_failures WHERE identifier_key=?",
            (key,),
        ).fetchone()
        failures = (int(row["failures"]) if row is not None else 0) + 1
        if failures >= self.failure_limit:
            locked_until = now + self.lockout_seconds
            connection.execute(
                """
                INSERT INTO auth_failures(identifier_key, failures, locked_until, updated_at)
                VALUES(?, 0, ?, ?)
                ON CONFLICT(identifier_key) DO UPDATE SET
                    failures=0, locked_until=excluded.locked_until, updated_at=excluded.updated_at
                """,
                (key, locked_until, now),
            )
            return SharedAuthenticationResult(
                False,
                "locked",
                retry_after_seconds=max(1, int(self.lockout_seconds + 0.999)),
            )
        connection.execute(
            """
            INSERT INTO auth_failures(identifier_key, failures, locked_until, updated_at)
            VALUES(?, ?, 0, ?)
            ON CONFLICT(identifier_key) DO UPDATE SET
                failures=excluded.failures, locked_until=0, updated_at=excluded.updated_at
            """,
            (key, failures, now),
        )
        return None

    def authenticate_candidates(
        self,
        identifier: str,
        passwords: Iterable[str],
        *,
        allow_passwordless: bool = False,
    ) -> SharedAuthenticationResult:
        name = self.clean(identifier)
        if not name:
            return SharedAuthenticationResult(False, "missing_name")
        candidates: list[str] = []
        for raw in passwords:
            value = str(raw or "")
            if value and value not in candidates:
                candidates.append(value)
        if not candidates and not allow_passwordless:
            return SharedAuthenticationResult(False, "missing_password")
        key = self.normalize(name)
        now = float(self._clock())
        with self.transaction() as connection:
            locked = self._failure_state(connection, key, now)
            if locked is not None:
                return locked
            row = self._account_row(connection, name)
            if row is None:
                if self.auto_enroll and candidates:
                    # Commit this transaction first, then use the normal creator
                    # outside it to avoid nesting BEGIN IMMEDIATE.
                    pass
                else:
                    self.verify_password(candidates[0] if candidates else "", self._dummy_password_hash)
                    result = self._note_failure(connection, key, now)
                    return result or SharedAuthenticationResult(False, "unknown_account")
            else:
                email_blocked = bool(row["email_key"]) and connection.execute(
                    "SELECT 1 FROM blocked_emails WHERE email_key=?",
                    (str(row["email_key"]),),
                ).fetchone() is not None
                if bool(row["banned"]) or email_blocked:
                    return SharedAuthenticationResult(False, "banned")
                if not bool(row["enabled"]):
                    return SharedAuthenticationResult(False, "disabled")
                valid = allow_passwordless or any(
                    self.verify_password(candidate, str(row["password_hash"]))
                    for candidate in candidates
                )
                if not valid:
                    result = self._note_failure(connection, key, now)
                    return result or SharedAuthenticationResult(False, "bad_password")
                connection.execute("DELETE FROM auth_failures WHERE identifier_key=?", (key,))
                record = self._record(connection, row)
                return SharedAuthenticationResult(
                    True,
                    "ok",
                    record.account_name,
                    record.primary_persona,
                )
        # Auto-enrolment runs after the read/write transaction above closes.
        try:
            account = self.create_account(name, candidates[0], persona=name)
        except SharedAccountExistsError:
            return self.authenticate_candidates(name, candidates, allow_passwordless=allow_passwordless)
        return SharedAuthenticationResult(True, "enrolled", account.account_name, account.primary_persona)

    def authenticate(self, identifier: str, password: str) -> SharedAuthenticationResult:
        return self.authenticate_candidates(identifier, (str(password or ""),))

    def identity(
        self,
        account_identifier: str,
        persona: str | None = None,
        *,
        require_carbon_wire_id: bool = False,
    ) -> SharedIdentityRecord:
        with self.transaction() as connection:
            account = self._require_account(connection, account_identifier)
            account_id = int(account["account_id"])
            requested = self.normalize(persona)
            if requested:
                persona_row = connection.execute(
                    "SELECT * FROM personas WHERE account_id=? AND display_name_key=?",
                    (account_id, requested),
                ).fetchone()
            else:
                persona_row = self._primary_persona_row(connection, account_id)
            if persona_row is None:
                raise KeyError(str(persona))
            wire_id = persona_row["carbon_wire_player_id"]
            if require_carbon_wire_id and wire_id is None:
                wire_id = self._allocate_wire_id(connection, str(persona_row["display_name"]))
                connection.execute(
                    "UPDATE personas SET carbon_wire_player_id=?, updated_at=? WHERE persona_id=?",
                    (wire_id, float(self._clock()), int(persona_row["persona_id"])),
                )
            return SharedIdentityRecord(
                account_id=account_id,
                account_uuid=str(account["account_uuid"]),
                account_name=str(account["account_name"]),
                persona_id=int(persona_row["persona_id"]),
                persona_uuid=str(persona_row["persona_uuid"]),
                persona=str(persona_row["display_name"]),
                profile_id=int(persona_row["profile_id"]),
                user_id=int(persona_row["profile_id"]),
                carbon_wire_player_id=(int(wire_id) if wire_id is not None else None),
            )

    def identity_for_persona(
        self,
        persona: object,
        *,
        require_carbon_wire_id: bool = False,
    ) -> SharedIdentityRecord | None:
        """Resolve a globally unique persona without knowing its account name."""

        key = self.normalize(persona)
        if not key:
            return None
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT p.*, a.account_uuid, a.account_name
                  FROM personas AS p JOIN accounts AS a ON a.account_id=p.account_id
                 WHERE p.display_name_key=?
                 LIMIT 1
                """,
                (key,),
            ).fetchone()
            if row is None:
                return None
            wire_id = row["carbon_wire_player_id"]
            if require_carbon_wire_id and wire_id is None:
                wire_id = self._allocate_wire_id(connection, str(row["display_name"]))
                connection.execute(
                    "UPDATE personas SET carbon_wire_player_id=?, updated_at=? WHERE persona_id=?",
                    (wire_id, float(self._clock()), int(row["persona_id"])),
                )
            return SharedIdentityRecord(
                account_id=int(row["account_id"]),
                account_uuid=str(row["account_uuid"]),
                account_name=str(row["account_name"]),
                persona_id=int(row["persona_id"]),
                persona_uuid=str(row["persona_uuid"]),
                persona=str(row["display_name"]),
                profile_id=int(row["profile_id"]),
                user_id=int(row["profile_id"]),
                carbon_wire_player_id=(int(wire_id) if wire_id is not None else None),
            )

    def personas(self) -> tuple[SharedIdentityRecord, ...]:
        """Return all globally registered personas in stable display-name order."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*, a.account_uuid, a.account_name
                  FROM personas AS p JOIN accounts AS a ON a.account_id=p.account_id
                 ORDER BY p.display_name_key, p.persona_id
                """
            ).fetchall()
        return tuple(
            SharedIdentityRecord(
                account_id=int(row["account_id"]),
                account_uuid=str(row["account_uuid"]),
                account_name=str(row["account_name"]),
                persona_id=int(row["persona_id"]),
                persona_uuid=str(row["persona_uuid"]),
                persona=str(row["display_name"]),
                profile_id=int(row["profile_id"]),
                user_id=int(row["profile_id"]),
                carbon_wire_player_id=(
                    int(row["carbon_wire_player_id"])
                    if row["carbon_wire_player_id"] is not None
                    else None
                ),
            )
            for row in rows
        )

    def identities_for_account(self, account_id: int) -> tuple[SharedIdentityRecord, ...]:
        """Return every persona owned by one account in stable order."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*, a.account_uuid, a.account_name
                  FROM personas AS p JOIN accounts AS a ON a.account_id=p.account_id
                 WHERE p.account_id=?
                 ORDER BY p.display_name_key, p.persona_id
                """,
                (int(account_id),),
            ).fetchall()
        return tuple(
            SharedIdentityRecord(
                account_id=int(row["account_id"]),
                account_uuid=str(row["account_uuid"]),
                account_name=str(row["account_name"]),
                persona_id=int(row["persona_id"]),
                persona_uuid=str(row["persona_uuid"]),
                persona=str(row["display_name"]),
                profile_id=int(row["profile_id"]),
                user_id=int(row["profile_id"]),
                carbon_wire_player_id=(
                    int(row["carbon_wire_player_id"])
                    if row["carbon_wire_player_id"] is not None
                    else None
                ),
            )
            for row in rows
        )

    def identity_for_profile(self, profile_id: int) -> SharedIdentityRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT p.*, a.account_uuid, a.account_name
                  FROM personas AS p JOIN accounts AS a ON a.account_id=p.account_id
                 WHERE p.profile_id=?
                """,
                (int(profile_id),),
            ).fetchone()
            if row is None:
                return None
            return SharedIdentityRecord(
                account_id=int(row["account_id"]),
                account_uuid=str(row["account_uuid"]),
                account_name=str(row["account_name"]),
                persona_id=int(row["persona_id"]),
                persona_uuid=str(row["persona_uuid"]),
                persona=str(row["display_name"]),
                profile_id=int(row["profile_id"]),
                user_id=int(row["profile_id"]),
                carbon_wire_player_id=(
                    int(row["carbon_wire_player_id"])
                    if row["carbon_wire_player_id"] is not None
                    else None
                ),
            )

    def account_directory(self, account_uuid: str) -> Path:
        token = self.clean(account_uuid)
        if not token or any(character not in "0123456789abcdefABCDEF" for character in token):
            raise ValueError("invalid account UUID")
        return self.user_root / token[:2].lower() / token.lower()

    def persona_directory(self, identity: SharedIdentityRecord, game: str) -> Path:
        safe_game = "".join(
            character for character in self.clean(game).casefold() if character.isalnum() or character in {"-", "_"}
        )
        if not safe_game:
            raise ValueError("game directory name is required")
        return (
            self.account_directory(identity.account_uuid)
            / "personas"
            / identity.persona_uuid.lower()
            / safe_game
        )

    def ensure_directories(self, account: SharedAccountRecord | str) -> Path:
        record = self.resolve_account(account) if isinstance(account, str) else account
        if record is None:
            raise KeyError(str(account))
        root = self.account_directory(record.account_uuid)
        (root / "common").mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            personas = connection.execute(
                "SELECT persona_uuid FROM personas WHERE account_id=?",
                (record.account_id,),
            ).fetchall()
        for persona in personas:
            persona_root = root / "personas" / str(persona["persona_uuid"]).lower()
            for game in ("underground2", "most_wanted", "carbon"):
                game_root = persona_root / game
                game_root.mkdir(parents=True, exist_ok=True)
                if game == "carbon":
                    for category in ("photos", "shadows", "blobs"):
                        (game_root / category).mkdir(parents=True, exist_ok=True)
        return root


class SQLiteSessionRegistry:
    """Atomic cross-process single-login lease registry."""

    def __init__(
        self,
        database: SQLiteAccountDatabase,
        *,
        game: str,
        server_id: str | None = None,
        lease_seconds: float = 120.0,
        clock: Callable[[], float] | None = None,
        server_owner_alive: Callable[[str], bool] | None = None,
    ) -> None:
        if float(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        self.database = database
        self.game = str(game or "unknown").strip().casefold() or "unknown"
        self.server_id = str(server_id or f"{self.game}:{os.getpid()}:{secrets.token_hex(6)}")
        self.lease_seconds = float(lease_seconds)
        self._clock = clock or time.time
        self._server_owner_alive = server_owner_alive or self._local_server_owner_alive

    @staticmethod
    def _local_server_owner_alive(server_id: str) -> bool:
        """Return False only for a lease that certainly belongs to a dead PID.

        Generated server IDs are ``game:pid:nonce``.  Custom/legacy IDs cannot
        be proved local, so they retain the normal timeout behaviour.
        """
        parts = str(server_id or "").split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            return True
        pid = int(parts[1])
        if pid <= 0:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    def _discard_dead_owner(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row | None,
    ) -> bool:
        if row is None:
            return False
        owner = str(row["server_id"] or "")
        try:
            alive = bool(self._server_owner_alive(owner))
        except Exception:
            # An uncertain liveness probe must never evict a valid client.
            alive = True
        if alive:
            return False
        connection.execute(
            "DELETE FROM active_sessions WHERE server_id=?",
            (owner,),
        )
        return True

    def _prune(self, connection: sqlite3.Connection, now: float) -> None:
        connection.execute("DELETE FROM active_sessions WHERE expires_at<=?", (now,))

    @staticmethod
    def _policy_reason(
        connection: sqlite3.Connection,
        account_id: int,
    ) -> str | None:
        account = connection.execute(
            "SELECT enabled, banned, email_key FROM accounts WHERE account_id=?",
            (int(account_id),),
        ).fetchone()
        if account is None:
            return "invalid_claim"
        if bool(account["banned"]):
            return "banned"
        if not bool(account["enabled"]):
            return "disabled"
        if bool(account["email_key"]) and connection.execute(
            "SELECT 1 FROM blocked_emails WHERE email_key=?",
            (str(account["email_key"]),),
        ).fetchone() is not None:
            return "banned"
        return None

    def claim(
        self,
        connection_id: str,
        account_name: str,
        persona: str,
        *,
        replace_same_game: bool = False,
    ) -> str | None:
        connection_token = str(connection_id or "").strip()
        if not connection_token:
            return "invalid_claim"
        try:
            identity = self.database.identity(account_name, persona)
        except KeyError:
            return "invalid_claim"
        now = float(self._clock())
        expires = now + self.lease_seconds
        with self.database.transaction() as connection:
            self._prune(connection, now)
            policy_reason = self._policy_reason(connection, identity.account_id)
            if policy_reason is not None:
                return policy_reason
            existing = connection.execute(
                "SELECT * FROM active_sessions WHERE account_id=?",
                (identity.account_id,),
            ).fetchone()
            if self._discard_dead_owner(connection, existing):
                existing = None
            if existing is not None and str(existing["connection_id"]) != connection_token:
                if not replace_same_game or str(existing["game"]) != self.game:
                    return "account_in_use"
            persona_owner = connection.execute(
                "SELECT connection_id, server_id FROM active_sessions WHERE persona_id=?",
                (identity.persona_id,),
            ).fetchone()
            if self._discard_dead_owner(connection, persona_owner):
                persona_owner = None
            if persona_owner is not None and str(persona_owner["connection_id"]) != connection_token:
                if not replace_same_game:
                    return "persona_in_use"
            connection.execute("DELETE FROM active_sessions WHERE connection_id=?", (connection_token,))
            connection.execute(
                """
                INSERT INTO active_sessions(
                    account_id, persona_id, game, connection_id, session_token,
                    server_id, connected_at, heartbeat_at, expires_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    persona_id=excluded.persona_id,
                    game=excluded.game,
                    connection_id=excluded.connection_id,
                    session_token=excluded.session_token,
                    server_id=excluded.server_id,
                    heartbeat_at=excluded.heartbeat_at,
                    expires_at=excluded.expires_at
                """,
                (
                    identity.account_id,
                    identity.persona_id,
                    self.game,
                    connection_token,
                    connection_token,
                    self.server_id,
                    now,
                    now,
                    expires,
                ),
            )
        return None

    def touch(self, connection_id: str) -> bool:
        token = str(connection_id or "").strip()
        if not token:
            return False
        now = float(self._clock())
        with self.database.transaction() as connection:
            self._prune(connection, now)
            current = connection.execute(
                "SELECT account_id FROM active_sessions WHERE connection_id=? AND server_id=?",
                (token, self.server_id),
            ).fetchone()
            if current is None:
                return False
            if self._policy_reason(connection, int(current["account_id"])) is not None:
                connection.execute(
                    "DELETE FROM active_sessions WHERE connection_id=? AND server_id=?",
                    (token, self.server_id),
                )
                return False
            cursor = connection.execute(
                """
                UPDATE active_sessions
                   SET heartbeat_at=?, expires_at=?
                 WHERE connection_id=? AND server_id=?
                """,
                (now, now + self.lease_seconds, token, self.server_id),
            )
            return cursor.rowcount > 0

    def switch_persona(self, connection_id: str, persona: str) -> str | None:
        token = str(connection_id or "").strip()
        if not token or not self.database.normalize(persona):
            return "invalid_claim"
        now = float(self._clock())
        with self.database.transaction() as connection:
            self._prune(connection, now)
            current = connection.execute(
                "SELECT * FROM active_sessions WHERE connection_id=? AND server_id=?",
                (token, self.server_id),
            ).fetchone()
            if current is None:
                return "not_authenticated"
            policy_reason = self._policy_reason(
                connection,
                int(current["account_id"]),
            )
            if policy_reason is not None:
                connection.execute(
                    "DELETE FROM active_sessions WHERE connection_id=? AND server_id=?",
                    (token, self.server_id),
                )
                return policy_reason
            persona_row = connection.execute(
                "SELECT persona_id FROM personas WHERE account_id=? AND display_name_key=?",
                (int(current["account_id"]), self.database.normalize(persona)),
            ).fetchone()
            if persona_row is None:
                return "persona_not_owned"
            owner = connection.execute(
                "SELECT connection_id FROM active_sessions WHERE persona_id=?",
                (int(persona_row["persona_id"]),),
            ).fetchone()
            if owner is not None and str(owner["connection_id"]) != token:
                return "persona_in_use"
            connection.execute(
                """
                UPDATE active_sessions
                   SET persona_id=?, heartbeat_at=?, expires_at=?
                 WHERE connection_id=? AND server_id=?
                """,
                (
                    int(persona_row["persona_id"]),
                    now,
                    now + self.lease_seconds,
                    token,
                    self.server_id,
                ),
            )
        return None

    def release(self, connection_id: str) -> None:
        token = str(connection_id or "").strip()
        if not token:
            return
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM active_sessions WHERE connection_id=? AND server_id=?",
                (token, self.server_id),
            )

    def release_all(self) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM active_sessions WHERE server_id=?",
                (self.server_id,),
            )
            return max(0, int(cursor.rowcount))

    def session_for(self, account_name: str) -> SharedSessionRecord | None:
        now = float(self._clock())
        with self.database.transaction() as connection:
            self._prune(connection, now)
            account = self.database._account_row(connection, account_name)
            if account is None:
                return None
            row = connection.execute(
                """
                SELECT s.*, p.display_name, a.account_name
                  FROM active_sessions AS s
                  JOIN personas AS p ON p.persona_id=s.persona_id
                  JOIN accounts AS a ON a.account_id=s.account_id
                 WHERE s.account_id=?
                """,
                (int(account["account_id"]),),
            ).fetchone()
            if row is None:
                return None
            return SharedSessionRecord(
                account_name=str(row["account_name"]),
                persona=str(row["display_name"]),
                game=str(row["game"]),
                connection_id=str(row["connection_id"]),
                server_id=str(row["server_id"]),
                heartbeat_at=float(row["heartbeat_at"]),
                expires_at=float(row["expires_at"]),
            )
