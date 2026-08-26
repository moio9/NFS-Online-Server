"""Durable account-policy events and live connection enforcement.

Account policy is persisted in SQLite by the administration command.  Every
running service follows the same append-only event stream and applies a
restrictive policy to its own in-memory protocol state.  This keeps the shared
account database authoritative without adding another deployment port or a
launcher-only control channel.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import sqlite3
import time
from threading import Event, RLock, Thread
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from common.accounts import SQLiteAccountDatabase


log = logging.getLogger(__name__)

ACCOUNT_POLICY_ACTIONS = frozenset({"ban", "unban", "disable", "enable", "kick"})
RESTRICTIVE_ACCOUNT_POLICY_ACTIONS = frozenset({"ban", "disable", "kick"})
ACCOUNT_POLICY_CLOSE_GRACE_SECONDS = 2.0


@dataclass(frozen=True)
class AccountPolicyEvent:
    """One committed account-policy transition from the shared database."""

    event_id: int
    account_id: int
    account_name: str
    action: str
    created_at: float

    def __post_init__(self) -> None:
        action = str(self.action or "").strip().casefold()
        if action not in ACCOUNT_POLICY_ACTIONS:
            raise ValueError(f"unsupported account policy action: {self.action!r}")
        object.__setattr__(self, "action", action)

    @property
    def restrictive(self) -> bool:
        return self.action in RESTRICTIVE_ACCOUNT_POLICY_ACTIONS

    @property
    def disconnect_reason(self) -> str:
        if self.action == "ban":
            return "account-banned"
        if self.action == "kick":
            return "account-kicked"
        return "account-disabled"


def append_account_policy_event(
    connection: sqlite3.Connection,
    *,
    account_id: int,
    account_name: str,
    action: str,
    created_at: float,
) -> int:
    """Append an event inside the caller's existing account transaction."""

    normalized_action = str(action or "").strip().casefold()
    if normalized_action not in ACCOUNT_POLICY_ACTIONS:
        raise ValueError(f"unsupported account policy action: {action!r}")
    cursor = connection.execute(
        """
        INSERT INTO account_policy_events(
            account_id, account_name, action, created_at
        ) VALUES(?, ?, ?, ?)
        """,
        (
            int(account_id),
            str(account_name),
            normalized_action,
            float(created_at),
        ),
    )
    event_id = int(cursor.lastrowid or 0)
    if event_id <= 0:
        raise RuntimeError("failed to allocate account policy event id")
    return event_id


class SQLiteAccountPolicyEventStore:
    """Read the append-only policy stream through the shared DB wrapper."""

    def __init__(
        self,
        database: "SQLiteAccountDatabase",
        *,
        batch_size: int = 100,
    ) -> None:
        if int(batch_size) <= 0:
            raise ValueError("account policy event batch size must be positive")
        self.database = database
        self.batch_size = int(batch_size)

    def latest_event_id(self) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(event_id), 0) AS event_id FROM account_policy_events"
            ).fetchone()
        return int(row["event_id"] if row is not None else 0)

    def events_after(self, event_id: int) -> tuple[AccountPolicyEvent, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, account_id, account_name, action, created_at
                  FROM account_policy_events
                 WHERE event_id > ?
                 ORDER BY event_id
                 LIMIT ?
                """,
                (max(0, int(event_id)), self.batch_size),
            ).fetchall()
        return tuple(
            AccountPolicyEvent(
                event_id=int(row["event_id"]),
                account_id=int(row["account_id"]),
                account_name=str(row["account_name"]),
                action=str(row["action"]),
                created_at=float(row["created_at"]),
            )
            for row in rows
        )


PolicyEventHandler = Callable[[AccountPolicyEvent], None]


class AccountPolicyMonitor:
    """Follow committed policy events with at-least-once in-process delivery."""

    def __init__(
        self,
        store: SQLiteAccountPolicyEventStore,
        handler: PolicyEventHandler,
        *,
        name: str,
        poll_interval: float = 0.1,
    ) -> None:
        if float(poll_interval) <= 0:
            raise ValueError("account policy poll interval must be positive")
        self.store = store
        self.handler = handler
        self.name = str(name or "account-policy-monitor")
        self.poll_interval = float(poll_interval)
        self._stop = Event()
        self._thread: Thread | None = None
        self._lifecycle_lock = RLock()
        self._cursor_lock = RLock()
        self._cursor = 0

    @property
    def cursor(self) -> int:
        with self._cursor_lock:
            return self._cursor

    def start(self) -> None:
        with self._lifecycle_lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                return
            self._thread = None
            # Existing events belong to earlier process lifetimes.  New
            # processes have no live sockets to evict, while the accounts
            # table itself still enforces persistent policy during login.
            with self._cursor_lock:
                self._cursor = self.store.latest_event_id()
            self._stop.clear()
            thread = Thread(target=self._run, name=self.name, daemon=True)
            self._thread = thread
            thread.start()
        log.info("%s started at event_id=%d", self.name, self.cursor)

    def poll_once(self) -> int:
        delivered = 0
        while True:
            events = self.store.events_after(self.cursor)
            if not events:
                return delivered
            for event in events:
                try:
                    self.handler(event)
                except Exception:
                    # Do not advance past a failed event.  A later poll retries
                    # it instead of silently leaving one service inconsistent.
                    log.exception(
                        "%s failed account policy event_id=%d account=%s action=%s",
                        self.name,
                        event.event_id,
                        event.account_name,
                        event.action,
                    )
                    return delivered
                with self._cursor_lock:
                    self._cursor = event.event_id
                delivered += 1
            if len(events) < self.store.batch_size:
                return delivered

    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                self.poll_once()
            except Exception:
                log.exception("%s failed while reading account policy events", self.name)

    def stop(self) -> None:
        with self._lifecycle_lock:
            thread = self._thread
            if thread is None:
                return
            self._stop.set()
        database_timeout = max(
            0.0,
            float(getattr(self.store.database, "busy_timeout_ms", 0)) / 1000.0,
        )
        thread.join(
            timeout=max(2.0, self.poll_interval * 4.0, database_timeout + 1.0)
        )
        if thread.is_alive():
            log.warning("%s did not stop before its database timeout", self.name)
            return
        with self._lifecycle_lock:
            if self._thread is thread:
                self._thread = None
        log.info("%s stopped at event_id=%d", self.name, self.cursor)


class PolicyCloseGate:
    """Thread-safe, non-blocking drain window for a policy-closed transport.

    Account state and room/race membership are revoked immediately, but the
    socket remains quiescent for a short bounded interval so a native protocol
    notification can reach and be processed by the retail client.  Callers
    poll :meth:`expired`; no enforcement thread sleeps or owns a timer thread.
    """

    def __init__(
        self,
        *,
        grace_seconds: float = ACCOUNT_POLICY_CLOSE_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if float(grace_seconds) < 0:
            raise ValueError("policy close grace must not be negative")
        self.grace_seconds = float(grace_seconds)
        self._clock = clock
        self._lock = RLock()
        self._deadline: float | None = None
        self._reason = ""

    def request(
        self,
        reason: str,
        *,
        grace_seconds: float | None = None,
    ) -> bool:
        """Quiesce the transport and schedule its bounded physical close.

        Repeated requests are idempotent.  A stricter request may shorten an
        existing deadline, but no later request can extend it.  The return
        value is true only for the first transition into the closing state.
        """

        grace = self.grace_seconds if grace_seconds is None else float(grace_seconds)
        if grace < 0:
            raise ValueError("policy close grace must not be negative")
        deadline = float(self._clock()) + grace
        normalized_reason = str(reason or "account-policy").strip() or "account-policy"
        with self._lock:
            first = self._deadline is None
            if self._deadline is None or deadline < self._deadline:
                self._deadline = deadline
            if not self._reason:
                self._reason = normalized_reason
            return first

    def force(self, reason: str) -> bool:
        """Request an immediate close while preserving the first close reason."""

        return self.request(reason, grace_seconds=0.0)

    @property
    def active(self) -> bool:
        with self._lock:
            return self._deadline is not None

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def expired(self, *, now: float | None = None) -> bool:
        with self._lock:
            deadline = self._deadline
        if deadline is None:
            return False
        current = float(self._clock()) if now is None else float(now)
        return current >= deadline

    def remaining(self, *, now: float | None = None) -> float | None:
        with self._lock:
            deadline = self._deadline
        if deadline is None:
            return None
        current = float(self._clock()) if now is None else float(now)
        return max(0.0, deadline - current)


AccountNameResolver = Callable[[], str]
PolicyCloseInitiator = Callable[[AccountPolicyEvent], bool]
PolicyCleanup = Callable[[], None]
PolicyCloseRequester = Callable[[str], None]


@dataclass(frozen=True)
class AccountEnforcementFailure:
    """One callback failure that requires the durable event to be retried."""

    phase: str
    connection_id: str
    protocol: str
    error: Exception


class AccountEnforcementError(RuntimeError):
    """Raised after all possible close work ran but one callback failed."""

    def __init__(
        self,
        registry_name: str,
        event: AccountPolicyEvent,
        failures: tuple[AccountEnforcementFailure, ...],
    ) -> None:
        if not failures:
            raise ValueError("account enforcement error requires at least one failure")
        self.registry_name = str(registry_name)
        self.event = event
        self.failures = failures
        summary = ", ".join(
            f"{failure.phase}:{failure.protocol}:{failure.connection_id}"
            for failure in failures
        )
        super().__init__(
            f"{self.registry_name} failed account policy event_id={event.event_id} "
            f"account={event.account_name!r} callbacks={summary}"
        )


@dataclass(frozen=True)
class LiveAccountConnection:
    """Transport hooks for one connection whose identity may appear later."""

    connection_id: str
    protocol: str
    account_name: AccountNameResolver
    request_close: PolicyCloseRequester
    begin_close: PolicyCloseInitiator | None = None


@dataclass(frozen=True)
class AccountEnforcementResult:
    matched: int = 0
    notified: int = 0
    closing: int = 0
    protocols: tuple[str, ...] = ()


class LiveAccountConnectionRegistry:
    """Process-local registry for notification and bounded live-policy closes."""

    def __init__(self, *, name: str) -> None:
        self.name = str(name or "account-connections")
        self._lock = RLock()
        self._connections: dict[str, LiveAccountConnection] = {}

    @staticmethod
    def _key(value: object) -> str:
        return str(value or "").strip().casefold()

    def register(self, connection: LiveAccountConnection) -> None:
        connection_id = str(connection.connection_id or "").strip()
        if not connection_id:
            raise ValueError("live account connection id must not be empty")
        with self._lock:
            if connection_id in self._connections:
                raise ValueError(f"duplicate live account connection: {connection_id}")
            self._connections[connection_id] = connection

    def unregister(self, connection_id: str) -> bool:
        with self._lock:
            return self._connections.pop(str(connection_id), None) is not None

    def enforce(
        self,
        event: AccountPolicyEvent,
        *,
        before_close: PolicyCleanup | None = None,
    ) -> AccountEnforcementResult:
        """Apply one restrictive policy in three deterministic phases.

        Each matching transport first atomically enters its policy-closing
        state and may send a native notification through ``begin_close``.
        The owning application then removes the account from rooms/races, and
        each transport receives a bounded close request last.  Physical socket
        closure is performed by the owning connection loop after its short
        drain window.  If cleanup fails, the close request still runs and the
        monitor retries the event.
        """

        if not event.restrictive:
            return AccountEnforcementResult()
        expected = self._key(event.account_name)
        with self._lock:
            snapshot = tuple(self._connections.values())

        matches: list[LiveAccountConnection] = []
        protocols: list[str] = []
        failures: list[AccountEnforcementFailure] = []
        if expected:
            for connection in snapshot:
                try:
                    actual = self._key(connection.account_name())
                except Exception as exc:
                    log.exception(
                        "%s failed resolving account for connection=%s protocol=%s",
                        self.name,
                        connection.connection_id,
                        connection.protocol,
                    )
                    failures.append(
                        AccountEnforcementFailure(
                            "resolve-account",
                            connection.connection_id,
                            connection.protocol,
                            exc,
                        )
                    )
                    continue
                if actual == expected:
                    matches.append(connection)
                    protocols.append(connection.protocol)

        notified = 0
        for connection in matches:
            if connection.begin_close is None:
                continue
            try:
                if connection.begin_close(event):
                    notified += 1
            except Exception as exc:
                log.exception(
                    "%s failed beginning policy close connection=%s protocol=%s",
                    self.name,
                    connection.connection_id,
                    connection.protocol,
                )
                failures.append(
                    AccountEnforcementFailure(
                        "begin-close",
                        connection.connection_id,
                        connection.protocol,
                        exc,
                    )
                )

        closing = 0
        try:
            if before_close is not None:
                before_close()
        finally:
            for connection in matches:
                try:
                    connection.request_close(event.disconnect_reason)
                    closing += 1
                except Exception as exc:
                    log.exception(
                        "%s failed policy close request connection=%s protocol=%s",
                        self.name,
                        connection.connection_id,
                        connection.protocol,
                    )
                    failures.append(
                        AccountEnforcementFailure(
                            "request-close",
                            connection.connection_id,
                            connection.protocol,
                            exc,
                        )
                    )

        if failures:
            error = AccountEnforcementError(self.name, event, tuple(failures))
            raise error from failures[0].error

        return AccountEnforcementResult(
            matched=len(matches),
            notified=notified,
            closing=closing,
            protocols=tuple(sorted(set(protocols))),
        )

    def __len__(self) -> int:
        with self._lock:
            return len(self._connections)


__all__ = [
    "ACCOUNT_POLICY_ACTIONS",
    "ACCOUNT_POLICY_CLOSE_GRACE_SECONDS",
    "RESTRICTIVE_ACCOUNT_POLICY_ACTIONS",
    "AccountEnforcementError",
    "AccountEnforcementFailure",
    "AccountEnforcementResult",
    "AccountPolicyEvent",
    "AccountPolicyMonitor",
    "PolicyCloseGate",
    "LiveAccountConnection",
    "LiveAccountConnectionRegistry",
    "SQLiteAccountPolicyEventStore",
    "append_account_policy_event",
]
