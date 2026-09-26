"""Bridge website social actions into the live EA Messenger social service."""
from __future__ import annotations

import json
from contextlib import closing, contextmanager
import logging
import sqlite3
import time
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable

from common.social import SocialService, canonical_persona

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_social_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    source_persona TEXT NOT NULL,
    target_persona TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    result_json TEXT NOT NULL DEFAULT '',
    processed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_web_social_events_pending
    ON web_social_events(status, event_id);
CREATE TABLE IF NOT EXISTS web_social_presence (
    session_hash TEXT PRIMARY KEY,
    persona TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS web_social_messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    source_persona TEXT NOT NULL,
    target_persona TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_web_social_messages_target
    ON web_social_messages(target_persona COLLATE NOCASE, message_id);
"""


def active_web_personas(connection, now: float) -> tuple[str, ...]:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_sessions'"
    ).fetchone():
        return ()
    return tuple(row[0] for row in connection.execute(
        "SELECT DISTINCT p.persona FROM web_social_presence p "
        "JOIN web_sessions s ON s.session_hash=p.session_hash "
        "JOIN accounts a ON a.account_id=s.account_id "
        "WHERE p.expires_at>? AND s.expires_at>? AND a.enabled=1 AND a.banned=0",
        (now, now),
    ))


def ensure_web_social_schema(database_path: str | Path) -> None:
    with closing(sqlite3.connect(str(database_path), timeout=5.0)) as connection, connection:
        connection.executescript(SCHEMA)


class WebSocialEventPump:
    """Consume website requests inside the process that owns live SocialService."""

    def __init__(
        self,
        database_path: str | Path,
        social: SocialService,
        *,
        poll_seconds: float = 0.08,
        visibility_notifier: Callable[[str], None] | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.social = social
        self.poll_seconds = max(0.03, float(poll_seconds))
        self.visibility_notifier = visibility_notifier
        self._stop = Event()
        self._thread: Thread | None = None
        self._web_personas: dict[str, str] = {}
        self._next_presence_sync = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        ensure_web_social_schema(self.database_path)
        with self._connect() as connection:
            # Do not replay a message whose delivery may have preceded a crash.
            connection.execute(
                "UPDATE web_social_events SET status='error', "
                "result_json='{\"accepted\":false,\"reason\":\"interrupted\"}' "
                "WHERE status='processing'"
            )
        self._stop.clear()
        self._thread = Thread(target=self._run, name="web-social-events", daemon=True)
        self._thread.start()
        log.info("Website social event processor started")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        for persona in tuple(self._web_personas.values()):
            self.social.set_web_sender(persona, None)
            self._notify_presence(persona)
        self._web_personas.clear()

    def _notify_presence(self, persona: str) -> None:
        for relation in self.social.snapshot(persona):
            viewer = relation.user
            if not relation.friend or self.social.is_blocked(persona, viewer) or self.social.is_blocked(viewer, persona):
                continue
            row = self.social.presence_row(viewer, persona)
            presence = row.presence if row else None
            self.social.deliver(viewer, "PGET", (
                ("USER", persona),
                ("SHOW", (presence.show if presence else "CHAT") if row and row.online else "AWAY"),
                ("STAT", presence.stat if presence else ""),
                ("PROD", presence.product if presence else ""),
                ("TITL", presence.title if presence else ""),
            ))

    def _receive_message(self, target: str, verb: str, fields) -> bool:
        if verb.upper() not in {"RECV", "PMSG"}:
            return False
        values = dict(fields)
        source = canonical_persona(values.get("USER", values.get("N", "")))
        body = str(values.get("T", values.get("TEXT", values.get("BODY", "")))).strip()[:500]
        if not source or not body or self.social.is_blocked(source, target) or self.social.is_blocked(target, source):
            return False
        with self._connect() as connection:
            if target.casefold() not in {name.casefold() for name in active_web_personas(connection, time.time())}:
                return False
            connection.execute(
                "INSERT INTO web_social_messages(created_at,source_persona,target_persona,body) VALUES(?,?,?,?)",
                (time.time(), source, target, body),
            )
        return True

    def _sync_presence(self) -> None:
        with self._connect() as connection:
            active = {name.casefold(): name for name in active_web_personas(connection, time.time())}
            connection.execute("DELETE FROM web_social_presence WHERE expires_at<=?", (time.time(),))
        for key in self._web_personas.keys() - active.keys():
            persona = self._web_personas[key]
            self.social.set_web_sender(persona, None)
            self._notify_presence(persona)
        for key in active.keys() - self._web_personas.keys():
            persona = active[key]
            self.social.set_web_sender(persona, lambda verb, fields, target=persona: self._receive_message(target, verb, fields))
            self._notify_presence(persona)
        self._web_personas = active

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self.database_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _claim(self) -> sqlite3.Row | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT event_id, created_at, source_persona, target_persona, action, payload_json
                FROM web_social_events
                WHERE status='pending'
                ORDER BY event_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            updated = connection.execute(
                "UPDATE web_social_events SET status='processing' "
                "WHERE event_id=? AND status='pending'",
                (int(row["event_id"]),),
            ).rowcount
            connection.commit()
            return row if updated else None

    def _finish(self, event_id: int, *, ok: bool, result: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE web_social_events
                SET status=?, result_json=?, processed_at=?
                WHERE event_id=?
                """,
                (
                    "done" if ok else "error",
                    json.dumps(result, separators=(",", ":"), ensure_ascii=False),
                    time.time(),
                    int(event_id),
                ),
            )

    @staticmethod
    def _mutation_payload(result: object) -> dict[str, Any]:
        return {
            "accepted": bool(getattr(result, "accepted", False)),
            "reason": str(getattr(result, "reason", "") or ""),
        }

    def _notify_relation(self, viewer: str, peer: str, *, status: str) -> int:
        row = self.social.presence_row(viewer, peer)
        if row is None:
            return 0
        attr = "AT" if row.friend else "B" if row.blocked else "R" if row.request == "incoming" else "P" if row.request else "AT"
        changed = "A" if row.friend or row.blocked or row.request else "D"
        fields = (("USER", peer), ("LIST", "I" if row.blocked else "B"),
                  ("STATUS", status), ("CHNG", changed), ("ATTR", attr))
        delivered = self.social.deliver(viewer, "RNOT", fields)
        if changed == "A":
            delivered += self.social.deliver(viewer, "ROST", fields)
        if row is not None and row.presence is not None:
            presence = row.presence
            delivered += self.social.deliver(
                viewer,
                "PGET",
                (
                    ("USER", row.user),
                    ("SHOW", presence.show),
                    ("STAT", presence.stat),
                    ("PROD", presence.product),
                ),
            )
        return delivered

    def _process(self, row: sqlite3.Row) -> dict[str, Any]:
        source = canonical_persona(row["source_persona"])
        target = canonical_persona(row["target_persona"])
        action = str(row["action"] or "").strip().lower()
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        delivered = 0
        if action == "visibility":
            if not source:
                return {"accepted": False, "reason": "missing_persona", "delivered": 0}
            # The website has already saved the account-wide preference. The
            # Messenger service reads it afresh and pushes the effective state.
            if self.visibility_notifier is not None:
                self.visibility_notifier(source)
            return {"accepted": True, "reason": "visibility_updated", "delivered": 0}

        if action == "friend_request":
            result = self.social.request_friend(source, target)
            data = self._mutation_payload(result)
            if data["accepted"]:
                delivered += self._notify_relation(source, target, status="P")
                delivered += self._notify_relation(target, source, status="P")
            return {**data, "delivered": delivered}

        if action == "friend_respond":
            accepted = bool(payload.get("accepted"))
            result = self.social.respond_friend(source, target, accepted)
            data = self._mutation_payload(result)
            if data["accepted"]:
                state = "F" if accepted else "-1"
                delivered += self._notify_relation(source, target, status=state)
                delivered += self._notify_relation(target, source, status=state)
            return {**data, "delivered": delivered}

        if action == "friend_remove":
            result = self.social.remove_friend(source, target)
            data = self._mutation_payload(result)
            if data["accepted"]:
                delivered += self._notify_relation(source, target, status="-1")
                delivered += self._notify_relation(target, source, status="-1")
            return {**data, "delivered": delivered}

        if action in {"block", "unblock"}:
            blocked = action == "block"
            result = self.social.set_blocked(source, target, blocked)
            data = self._mutation_payload(result)
            if data["accepted"]:
                delivered += self._notify_relation(source, target, status="B" if blocked else "-1")
                delivered += self._notify_relation(target, source, status="-1")
            return {**data, "blocked": blocked, "delivered": delivered}

        if action == "message":
            self._sync_presence()
            body = str(payload.get("body", "") or "").strip()
            if not body:
                return {"accepted": False, "reason": "empty_message", "delivered": 0}
            if len(body) > 500:
                body = body[:500]
            if self.social.is_blocked(source, target) or self.social.is_blocked(target, source):
                return {"accepted": False, "reason": "blocked", "delivered": 0}
            target_row = self.social.presence_row(target, source)
            if target_row is not None and target_row.presence is not None:
                presence = target_row.presence
                delivered += self.social.deliver(
                    target,
                    "PGET",
                    (("USER", source), ("SHOW", presence.show), ("STAT", presence.stat)),
                )
            delivered += self.social.deliver(
                target, "PADD", (("LRSC", "PC"), ("USER", source))
            )
            message_delivered = self.social.deliver(
                target,
                "RECV",
                (
                    ("USER", source),
                    ("N", source),
                    ("T", body),
                    ("BODY", body),
                    ("TEXT", body),
                    ("F", "P"),
                    ("TYPE", "C"),
                ),
            )
            # Web recipients persist the message in their delivery callback.
            if message_delivered > 0 and target.casefold() not in self._web_personas:
                with self._connect() as connection:
                    connection.execute(
                        "INSERT INTO web_social_messages(created_at,source_persona,target_persona,body) VALUES(?,?,?,?)",
                        (time.time(), source, target, body),
                    )
            return {
                "accepted": message_delivered > 0,
                "reason": "sent" if message_delivered > 0 else "messaging_unavailable" if delivered else "player_offline",
                "delivered": message_delivered,
            }

        if action == "report":
            reason = str(payload.get("reason", "") or "").strip()[:256]
            if not reason:
                return {"accepted": False, "reason": "empty_report", "delivered": 0}
            report = self.social.record_report(
                source,
                target,
                reason,
                language=str(payload.get("language", "") or "")[:32],
                source="website",
                source_event_id=int(row["event_id"]),
            )
            return {
                "accepted": True,
                "reason": "reported",
                "reportId": int(getattr(report, "report_id", 0) or 0),
                "delivered": 0,
            }

        return {"accepted": False, "reason": "unsupported_action", "delivered": 0}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if time.monotonic() >= self._next_presence_sync:
                    self._sync_presence()
                    self._next_presence_sync = time.monotonic() + 0.5
                row = self._claim()
                if row is None:
                    self._stop.wait(self.poll_seconds)
                    continue
                event_id = int(row["event_id"])
                try:
                    result = (
                        {"accepted": False, "reason": "expired"}
                        if time.time() - float(row["created_at"]) > 30.0
                        else self._process(row)
                    )
                    self._finish(event_id, ok=bool(result.get("accepted")), result=result)
                except Exception as exc:
                    log.exception("failed to process website social event id=%d", event_id)
                    self._finish(
                        event_id,
                        ok=False,
                        result={"accepted": False, "reason": type(exc).__name__},
                    )
            except Exception:
                log.exception("website social event pump iteration failed")
                self._stop.wait(max(self.poll_seconds, 0.25))
