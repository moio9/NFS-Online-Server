"""Shared social-domain state for classic EA and newer game adapters.

The service intentionally contains no wire-protocol strings.  It owns active
lobby/control identities, presence, friend requests, blocks and message
routing.  Game adapters translate these generic rows into their own packets.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import logging
from pathlib import Path
import time
from threading import RLock
from typing import Callable, Iterable, Mapping

from .accounts import SQLiteAccountDatabase
from .recent_players import RecentPlayers


log = logging.getLogger(__name__)

ControlSender = Callable[[str, tuple[tuple[str, str], ...]], bool]


def canonical_persona(value: object) -> str:
    """Return the bare EA persona from JID/resource-style wire values."""
    text = str(value or "").strip().strip('"')
    if not text:
        return ""
    if "/" in text and not text.startswith("/"):
        text = text.split("/", 1)[0].strip()
    if "@" in text:
        text = text.split("@", 1)[0].strip()
    return text


def persona_key(value: object) -> str:
    return canonical_persona(value).casefold()


def stable_persona_id(value: object) -> int:
    """Stable positive identifier suitable for classic roster UID fields."""
    key = persona_key(value).encode("utf-8")
    digest = hashlib.sha256(key).digest()
    return 100_000_000 + (int.from_bytes(digest[:4], "big") % 900_000_000)


@dataclass(frozen=True)
class LobbyIdentity:
    connection_id: str
    account_name: str
    persona: str
    client_ip: str
    game_id: str = ""
    session_token: str = ""
    registered_at: float = 0.0


@dataclass(frozen=True)
class Presence:
    show: str = "PASS"
    stat: str = ""
    product: str = ""
    title: str = ""
    attr: str = ""


@dataclass(frozen=True)
class SocialRow:
    user: str
    online: bool
    friend: bool = False
    request: str = ""  # "incoming" or "outgoing"
    blocked: bool = False
    attr: str = ""
    presence: Presence | None = None


@dataclass(frozen=True)
class RelationResult:
    accepted: bool
    reason: str
    changed: bool = False


@dataclass(frozen=True)
class SocialReport:
    created_at: float
    reporter: str
    target: str
    reason: str
    language: str = ""
    source: str = ""


class SocialService:
    """Thread-safe reusable social graph and live-delivery registry."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        database: SQLiteAccountDatabase | None = None,
        persona_provider: Callable[[], Iterable[str]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.database = database
        self.path = None if database is not None else (Path(path) if path else None)
        self._persona_provider = persona_provider or (lambda: ())
        self._clock = clock or time.time
        self._lock = RLock()
        self._display: dict[str, str] = {}
        self._lobby_by_connection: dict[str, LobbyIdentity] = {}
        # Transient game-room membership.  This is intentionally separate from
        # login/presence: EA Messenger's Player List represents the members of
        # the viewer's current session, not every authenticated player in the
        # same title.
        self._session_by_connection: dict[str, tuple[str, str, str]] = {}
        self._control_senders: dict[str, tuple[str, ControlSender]] = {}
        self._controls_by_persona: dict[str, set[str]] = {}
        self._presence: dict[str, Presence] = {}
        self._friends: dict[str, set[str]] = {}
        self._pending: set[tuple[str, str]] = set()
        self._blocks: dict[str, set[str]] = {}
        self._reports: list[SocialReport] = []
        if self.database is not None:
            self._load_sqlite_locked()
        elif self.path is not None:
            self._load()
        self._recent_players = RecentPlayers(
            database,
            self.path.with_suffix(".recent.json") if self.path is not None else None,
        )

    @staticmethod
    def _clean_values(value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            source = value.replace(";", ",").split(",")
        elif isinstance(value, (list, tuple, set, frozenset)):
            source = value
        else:
            source = ()
        result: list[str] = []
        seen: set[str] = set()
        for item in source:
            display = canonical_persona(item)
            key = persona_key(display)
            if key and key not in seen:
                seen.add(key)
                result.append(display)
        return tuple(result)

    def _remember_locked(self, value: object) -> str:
        display = canonical_persona(value)
        key = persona_key(display)
        if key and display:
            self._display[key] = display
        return key

    def _display_name_locked(self, key: str) -> str:
        return self._display.get(key, key)

    def _load(self) -> None:
        assert self.path is not None
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid social data {self.path}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise ValueError(f"invalid social data {self.path}: expected object")

        display = raw.get("display", {})
        if isinstance(display, Mapping):
            for raw_key, raw_value in display.items():
                value = canonical_persona(raw_value or raw_key)
                key = persona_key(value)
                if key:
                    self._display[key] = value

        friends = raw.get("friends", {})
        if isinstance(friends, Mapping):
            for owner_value, targets_value in friends.items():
                owner = self._remember_locked(owner_value)
                if not owner:
                    continue
                for target_value in self._clean_values(targets_value):
                    target = self._remember_locked(target_value)
                    if target and target != owner:
                        self._friends.setdefault(owner, set()).add(target)
                        self._friends.setdefault(target, set()).add(owner)

        pending = raw.get("pending", [])
        if isinstance(pending, list):
            for item in pending:
                if not isinstance(item, Mapping):
                    continue
                requester = self._remember_locked(item.get("from", ""))
                target = self._remember_locked(item.get("to", ""))
                if requester and target and requester != target:
                    self._pending.add((requester, target))

        blocks = raw.get("blocks", {})
        if isinstance(blocks, Mapping):
            for owner_value, targets_value in blocks.items():
                owner = self._remember_locked(owner_value)
                if not owner:
                    continue
                for target_value in self._clean_values(targets_value):
                    target = self._remember_locked(target_value)
                    if target and target != owner:
                        self._blocks.setdefault(owner, set()).add(target)

    def _load_sqlite_locked(self) -> None:
        database = self.database
        if database is None:
            return
        self._friends.clear()
        self._pending.clear()
        self._blocks.clear()
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.relation,
                       source.display_name AS source_name,
                       target.display_name AS target_name
                  FROM social_relations AS r
                  JOIN personas AS source ON source.persona_id=r.source_persona_id
                  JOIN personas AS target ON target.persona_id=r.target_persona_id
                 ORDER BY r.relation, source.display_name_key, target.display_name_key
                """
            ).fetchall()
        for row in rows:
            source_display = canonical_persona(row["source_name"])
            target_display = canonical_persona(row["target_name"])
            source = self._remember_locked(source_display)
            target = self._remember_locked(target_display)
            relation = str(row["relation"])
            if not source or not target or source == target:
                continue
            if relation == "friend":
                self._friends.setdefault(source, set()).add(target)
            elif relation == "pending":
                self._pending.add((source, target))
            elif relation == "blocked":
                self._blocks.setdefault(source, set()).add(target)

    @staticmethod
    def _persona_ids(connection, owner_key: str, target_key: str) -> tuple[int, int] | None:
        rows = connection.execute(
            """
            SELECT persona_id, display_name_key
              FROM personas
             WHERE display_name_key IN (?, ?)
            """,
            (owner_key, target_key),
        ).fetchall()
        found = {str(row["display_name_key"]): int(row["persona_id"]) for row in rows}
        if owner_key not in found or target_key not in found:
            return None
        return found[owner_key], found[target_key]

    def _sqlite_request_friend(self, owner: object, target: object) -> RelationResult:
        assert self.database is not None
        owner_key = persona_key(owner)
        target_key = persona_key(target)
        if not owner_key or not target_key:
            return RelationResult(False, "missing_persona")
        if owner_key == target_key:
            return RelationResult(False, "same_identity")
        now = float(self._clock())
        with self.database.transaction() as connection:
            ids = self._persona_ids(connection, owner_key, target_key)
            if ids is None:
                return RelationResult(False, "target_not_found")
            owner_id, target_id = ids
            blocked = connection.execute(
                """
                SELECT 1 FROM social_relations
                 WHERE relation='blocked'
                   AND ((source_persona_id=? AND target_persona_id=?)
                     OR (source_persona_id=? AND target_persona_id=?))
                 LIMIT 1
                """,
                (owner_id, target_id, target_id, owner_id),
            ).fetchone()
            if blocked is not None:
                return RelationResult(False, "blocked")
            friend = connection.execute(
                """
                SELECT 1 FROM social_relations
                 WHERE source_persona_id=? AND target_persona_id=? AND relation='friend'
                """,
                (owner_id, target_id),
            ).fetchone()
            if friend is not None:
                return RelationResult(True, "already_friends", False)
            reverse = connection.execute(
                """
                SELECT 1 FROM social_relations
                 WHERE source_persona_id=? AND target_persona_id=? AND relation='pending'
                """,
                (target_id, owner_id),
            ).fetchone()
            outgoing = connection.execute(
                """
                SELECT 1 FROM social_relations
                 WHERE source_persona_id=? AND target_persona_id=? AND relation='pending'
                """,
                (owner_id, target_id),
            ).fetchone()
            if reverse is not None:
                connection.execute(
                    "DELETE FROM social_relations WHERE relation='pending' AND ((source_persona_id=? AND target_persona_id=?) OR (source_persona_id=? AND target_persona_id=?))",
                    (owner_id, target_id, target_id, owner_id),
                )
                connection.executemany(
                    "INSERT OR IGNORE INTO social_relations(source_persona_id,target_persona_id,relation,created_at) VALUES(?,?, 'friend', ?)",
                    ((owner_id, target_id, now), (target_id, owner_id, now)),
                )
                result = RelationResult(True, "accepted", True)
            elif outgoing is not None:
                result = RelationResult(True, "already_pending", False)
            else:
                connection.execute(
                    "INSERT INTO social_relations(source_persona_id,target_persona_id,relation,created_at) VALUES(?,?, 'pending', ?)",
                    (owner_id, target_id, now),
                )
                result = RelationResult(True, "requested", True)
        with self._lock:
            self._load_sqlite_locked()
        return result

    def _sqlite_respond_friend(self, owner: object, requester: object, accept: bool) -> RelationResult:
        assert self.database is not None
        owner_key = persona_key(owner)
        requester_key = persona_key(requester)
        if not owner_key or not requester_key:
            return RelationResult(False, "missing_persona")
        now = float(self._clock())
        with self.database.transaction() as connection:
            ids = self._persona_ids(connection, owner_key, requester_key)
            if ids is None:
                return RelationResult(False, "target_not_found")
            owner_id, requester_id = ids
            pending = connection.execute(
                "SELECT 1 FROM social_relations WHERE source_persona_id=? AND target_persona_id=? AND relation='pending'",
                (requester_id, owner_id),
            ).fetchone()
            if pending is None:
                return RelationResult(False, "request_not_found")
            connection.execute(
                "DELETE FROM social_relations WHERE source_persona_id=? AND target_persona_id=? AND relation='pending'",
                (requester_id, owner_id),
            )
            if accept:
                connection.executemany(
                    "INSERT OR IGNORE INTO social_relations(source_persona_id,target_persona_id,relation,created_at) VALUES(?,?, 'friend', ?)",
                    ((owner_id, requester_id, now), (requester_id, owner_id, now)),
                )
                reason = "accepted"
            else:
                reason = "declined"
        with self._lock:
            self._load_sqlite_locked()
        return RelationResult(True, reason, True)

    def _sqlite_remove_friend(self, owner: object, target: object) -> RelationResult:
        assert self.database is not None
        owner_key = persona_key(owner)
        target_key = persona_key(target)
        if not owner_key or not target_key:
            return RelationResult(False, "missing_persona")
        with self.database.transaction() as connection:
            ids = self._persona_ids(connection, owner_key, target_key)
            if ids is None:
                return RelationResult(True, "not_present", False)
            owner_id, target_id = ids
            cursor = connection.execute(
                """
                DELETE FROM social_relations
                 WHERE relation IN ('friend','pending')
                   AND ((source_persona_id=? AND target_persona_id=?)
                     OR (source_persona_id=? AND target_persona_id=?))
                """,
                (owner_id, target_id, target_id, owner_id),
            )
            changed = cursor.rowcount > 0
        if changed:
            with self._lock:
                self._load_sqlite_locked()
        return RelationResult(True, "removed" if changed else "not_present", changed)

    def _sqlite_set_blocked(self, owner: object, target: object, blocked: bool) -> RelationResult:
        assert self.database is not None
        owner_key = persona_key(owner)
        target_key = persona_key(target)
        if not owner_key or not target_key:
            return RelationResult(False, "missing_persona")
        if owner_key == target_key:
            return RelationResult(False, "same_identity")
        now = float(self._clock())
        with self.database.transaction() as connection:
            ids = self._persona_ids(connection, owner_key, target_key)
            if ids is None:
                return RelationResult(False, "target_not_found")
            owner_id, target_id = ids
            if blocked:
                before = connection.total_changes
                connection.execute(
                    "INSERT OR IGNORE INTO social_relations(source_persona_id,target_persona_id,relation,created_at) VALUES(?,?, 'blocked', ?)",
                    (owner_id, target_id, now),
                )
                connection.execute(
                    """
                    DELETE FROM social_relations
                     WHERE relation IN ('friend','pending')
                       AND ((source_persona_id=? AND target_persona_id=?)
                         OR (source_persona_id=? AND target_persona_id=?))
                    """,
                    (owner_id, target_id, target_id, owner_id),
                )
                changed = connection.total_changes > before
            else:
                cursor = connection.execute(
                    "DELETE FROM social_relations WHERE source_persona_id=? AND target_persona_id=? AND relation='blocked'",
                    (owner_id, target_id),
                )
                changed = cursor.rowcount > 0
        if changed:
            with self._lock:
                self._load_sqlite_locked()
        return RelationResult(True, "blocked" if blocked else "unblocked", changed)

    def _save_locked(self) -> None:
        if self.database is not None or self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "display": dict(sorted(self._display.items())),
            "friends": {
                self._display_name_locked(owner): [
                    self._display_name_locked(target)
                    for target in sorted(targets)
                ]
                for owner, targets in sorted(self._friends.items())
                if targets
            },
            "pending": [
                {
                    "from": self._display_name_locked(requester),
                    "to": self._display_name_locked(target),
                }
                for requester, target in sorted(self._pending)
            ],
            "blocks": {
                self._display_name_locked(owner): [
                    self._display_name_locked(target)
                    for target in sorted(targets)
                ]
                for owner, targets in sorted(self._blocks.items())
                if targets
            },
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def _session_directory_targets_locked(
        self,
        game_id: str,
        session_id: str,
        subject_key: str,
    ) -> tuple[str, ...]:
        """Return controlled personas sharing one transient game session."""

        wanted_game = str(game_id or "").strip().casefold()
        wanted_session = str(session_id or "").strip()
        if not wanted_game or not wanted_session or not subject_key:
            return ()
        members = {
            persona
            for persona, game, session in self._session_by_connection.values()
            if game == wanted_game and session == wanted_session
        }
        recipients: list[str] = []
        for owner_key in sorted(members):
            if owner_key == subject_key or owner_key not in self._controls_by_persona:
                continue
            friends = subject_key in self._friends.get(owner_key, set())
            incoming = (subject_key, owner_key) in self._pending
            outgoing = (owner_key, subject_key) in self._pending
            blocked = (
                subject_key in self._blocks.get(owner_key, set())
                or owner_key in self._blocks.get(subject_key, set())
            )
            if friends or incoming or outgoing or blocked:
                continue
            recipients.append(self._display_name_locked(owner_key))
        return tuple(recipients)

    def _publish_game_directory_add(
        self,
        persona: str,
        game_id: str,
        recipients: tuple[str, ...],
    ) -> None:
        if not recipients:
            return
        key = persona_key(persona)
        with self._lock:
            presence = self._presence.get(key, Presence())
        for recipient in recipients:
            self.deliver(
                recipient,
                "RNOT",
                (("CHNG", "A"), ("USER", persona), ("ATTR", "D")),
            )
            self.deliver(
                recipient,
                "PGET",
                (
                    ("SHOW", presence.show or "PASS"),
                    ("STAT", presence.stat),
                    ("PROD", presence.product),
                    ("TITL", presence.title),
                    ("USER", persona),
                    ("ATTR", "D"),
                ),
            )

    def _publish_game_directory_remove(
        self,
        persona: str,
        recipients: tuple[str, ...],
    ) -> None:
        for recipient in recipients:
            self.deliver(
                recipient,
                "RNOT",
                (("CHNG", "D"), ("USER", persona), ("ATTR", "D")),
            )

    def _session_player_rows_locked(
        self,
        owner_key: str,
        game_id: str,
        session_id: str,
    ) -> tuple[SocialRow, ...]:
        targets = {
            member_key
            for member_key, game, session in self._session_by_connection.values()
            if game == game_id and session == session_id and member_key != owner_key
        }
        selected: list[SocialRow] = []
        for target in targets:
            if not target:
                continue
            row = self._row_locked(owner_key, target)
            blocked_reverse = owner_key in self._blocks.get(target, set())
            if row.blocked or blocked_reverse or row.friend or row.request:
                continue
            if row.online:
                selected.append(row)
        selected.sort(key=lambda row: row.user.casefold())
        return tuple(selected)

    def _publish_player_rows_add(
        self, recipient: str, rows: tuple[SocialRow, ...]
    ) -> None:
        for row in rows:
            self.deliver(
                recipient,
                "RNOT",
                (("CHNG", "A"), ("USER", row.user), ("ATTR", "D")),
            )
            presence = row.presence or Presence()
            self.deliver(
                recipient,
                "PGET",
                (
                    ("SHOW", presence.show or "PASS"),
                    ("STAT", presence.stat),
                    ("PROD", presence.product),
                    ("TITL", presence.title),
                    ("USER", row.user),
                    ("ATTR", "D"),
                ),
            )

    def _publish_player_rows_remove(
        self, recipient: str, rows: tuple[SocialRow, ...]
    ) -> None:
        for row in rows:
            self.deliver(
                recipient,
                "RNOT",
                (("CHNG", "D"), ("USER", row.user), ("ATTR", "D")),
            )

    def set_game_session(
        self,
        connection_id: str,
        persona: object,
        game_id: str,
        session_id: object,
    ) -> None:
        """Set one connection's current room/session membership.

        An empty ``session_id`` removes the membership.  Notifications are
        emitted only when the persona enters or leaves the session as a whole,
        so duplicate protocol connections for the same persona do not create
        duplicate Player List rows.
        """

        connection = str(connection_id or "").strip()
        display = canonical_persona(persona)
        key = persona_key(display)
        wanted_game = str(game_id or "").strip().casefold()
        wanted_session = str(session_id or "").strip()
        if not connection:
            return

        old_remove: tuple[str, str, tuple[str, ...]] | None = None
        new_add: tuple[str, str, tuple[str, ...]] | None = None
        old_peer_rows: tuple[SocialRow, ...] = ()
        new_peer_rows: tuple[SocialRow, ...] = ()
        with self._lock:
            previous = self._session_by_connection.get(connection)
            desired = (key, wanted_game, wanted_session) if key and wanted_game and wanted_session else None
            if previous == desired:
                return

            if previous is not None:
                old_key, old_game, old_session = previous
                old_peer_rows = self._session_player_rows_locked(
                    old_key, old_game, old_session
                )
                self._session_by_connection.pop(connection, None)
                still_member = any(
                    member_key == old_key and game == old_game and session == old_session
                    for member_key, game, session in self._session_by_connection.values()
                )
                if not still_member:
                    recipients = self._session_directory_targets_locked(
                        old_game, old_session, old_key
                    )
                    old_remove = (self._display_name_locked(old_key), old_game, recipients)

            if desired is not None:
                was_member = any(
                    member_key == key and game == wanted_game and session == wanted_session
                    for member_key, game, session in self._session_by_connection.values()
                )
                self._remember_locked(display)
                self._session_by_connection[connection] = desired
                if wanted_game == "carbon" and not was_member:
                    self._recent_players.record(wanted_game, [
                        self._display_name_locked(member_key)
                        for member_key, game, session in self._session_by_connection.values()
                        if game == wanted_game and session == wanted_session
                    ], float(self._clock()))
                new_peer_rows = self._session_player_rows_locked(
                    key, wanted_game, wanted_session
                )
                if not was_member:
                    recipients = self._session_directory_targets_locked(
                        wanted_game, wanted_session, key
                    )
                    new_add = (display, wanted_game, recipients)

        if old_remove is not None:
            old_persona, _old_game, recipients = old_remove
            if _old_game != "carbon":
                self._publish_game_directory_remove(old_persona, recipients)
                self._publish_player_rows_remove(old_persona, old_peer_rows)
        if new_add is not None:
            new_persona, new_game, recipients = new_add
            self._publish_game_directory_add(new_persona, new_game, recipients)
            self._publish_player_rows_add(new_persona, new_peer_rows)

    def clear_game_session(self, connection_id: str) -> None:
        connection = str(connection_id or "").strip()
        if not connection:
            return
        with self._lock:
            previous = self._session_by_connection.get(connection)
            display = self._display_name_locked(previous[0]) if previous else ""
            game_id = previous[1] if previous else ""
        self.set_game_session(connection, display, game_id, "")

    def register_lobby(
        self,
        connection_id: str,
        account_name: str,
        persona: str,
        client_ip: str,
        *,
        game_id: str = "",
        session_token: str = "",
    ) -> LobbyIdentity:
        connection = str(connection_id or "").strip()
        display = canonical_persona(persona)
        if not connection or not display or not str(client_ip or "").strip():
            raise ValueError("connection_id, persona and client_ip are required")
        identity = LobbyIdentity(
            connection,
            str(account_name or display).strip() or display,
            display,
            str(client_ip).strip(),
            str(game_id or "").strip().casefold(),
            str(session_token or ""),
            self._clock(),
        )
        with self._lock:
            previous = self._lobby_by_connection.get(connection)
            key = self._remember_locked(display)
            self._lobby_by_connection[connection] = identity
            self._presence.setdefault(key, Presence())
            if previous is not None and persona_key(previous.persona) != key:
                previous_key = persona_key(previous.persona)
                if not any(
                    persona_key(other.persona) == previous_key
                    for other_connection, other in self._lobby_by_connection.items()
                    if other_connection != connection
                ):
                    current = self._presence.get(previous_key, Presence())
                    self._presence[previous_key] = Presence(
                        show="AWAY",
                        stat=current.stat,
                        product=current.product,
                        title=current.title,
                        attr=current.attr,
                    )
        return identity

    def unregister_lobby(self, connection_id: str) -> None:
        connection = str(connection_id or "").strip()
        if not connection:
            return
        # Remove transient room membership before the lobby identity disappears
        # so peers receive the Player List removal with the correct display name.
        self.clear_game_session(connection)
        with self._lock:
            identity = self._lobby_by_connection.pop(connection, None)
            if identity is None:
                return
            key = persona_key(identity.persona)
            if not any(
                persona_key(other.persona) == key
                for other in self._lobby_by_connection.values()
            ):
                current = self._presence.get(key, Presence())
                self._presence[key] = Presence(
                    show="AWAY",
                    stat=current.stat,
                    product=current.product,
                    title=current.title,
                    attr=current.attr,
                )

    def resolve_lobby(
        self,
        client_ip: str,
        requested_persona: object = "",
        *,
        game_id: str = "",
        unclaimed_only: bool = False,
    ) -> LobbyIdentity | None:
        peer = str(client_ip or "").strip()
        wanted = persona_key(requested_persona)
        wanted_game = str(game_id or "").strip().casefold()
        with self._lock:
            candidates = [
                identity
                for identity in self._lobby_by_connection.values()
                if identity.client_ip == peer
                and (not wanted_game or identity.game_id == wanted_game)
            ]
            claimed = set(self._controls_by_persona)
            if wanted:
                candidates = [
                    identity
                    for identity in candidates
                    if persona_key(identity.persona) == wanted
                ]
                if unclaimed_only:
                    candidates = [
                        identity
                        for identity in candidates
                        if persona_key(identity.persona) not in claimed
                    ]
                return candidates[0] if len(candidates) == 1 else None

            unclaimed = [
                identity
                for identity in candidates
                if persona_key(identity.persona) not in claimed
            ]
            if unclaimed:
                # Stock clients often omit the persona in AUTH. Choosing the
                # oldest unclaimed lobby login matches the normal sequence.
                return min(unclaimed, key=lambda item: item.registered_at)
            if unclaimed_only:
                return None
            return candidates[0] if len(candidates) == 1 else None

    def has_lobby(self, client_ip: str, *, game_id: str = "") -> bool:
        """Return whether this peer currently has a lobby identity for a game.

        Presence-only Messenger sockets such as stock U2's PSET transition do
        not carry a persona.  They still need dialect selection when several
        same-machine personas are already claimed by authenticated controls.
        """

        peer = str(client_ip or "").strip()
        wanted_game = str(game_id or "").strip().casefold()
        with self._lock:
            return any(
                identity.client_ip == peer
                and (not wanted_game or identity.game_id == wanted_game)
                for identity in self._lobby_by_connection.values()
            )

    def register_control(
        self,
        connection_id: str,
        client_ip: str,
        requested_persona: object,
        sender: ControlSender,
        *,
        game_id: str = "",
    ) -> LobbyIdentity | None:
        connection = str(connection_id or "").strip()
        if not connection or not callable(sender):
            return None
        identity = self.resolve_lobby(
            client_ip,
            requested_persona,
            game_id=game_id,
        )
        if identity is None:
            return None
        key = persona_key(identity.persona)
        with self._lock:
            old = self._control_senders.get(connection)
            if old is not None:
                old_key, _old_sender = old
                bucket = self._controls_by_persona.get(old_key)
                if bucket is not None:
                    bucket.discard(connection)
                    if not bucket:
                        self._controls_by_persona.pop(old_key, None)
            self._remember_locked(identity.persona)
            self._control_senders[connection] = (key, sender)
            self._controls_by_persona.setdefault(key, set()).add(connection)
        return identity

    def unregister_control(self, connection_id: str) -> None:
        connection = str(connection_id or "").strip()
        if not connection:
            return
        with self._lock:
            current = self._control_senders.pop(connection, None)
            if current is None:
                return
            key, _sender = current
            bucket = self._controls_by_persona.get(key)
            if bucket is not None:
                bucket.discard(connection)
                if not bucket:
                    self._controls_by_persona.pop(key, None)

    def _online_locked(self, key: str) -> bool:
        return any(
            persona_key(identity.persona) == key
            for identity in self._lobby_by_connection.values()
        )

    def set_presence(
        self,
        persona: object,
        *,
        show: str = "",
        stat: str = "",
        product: str = "",
        title: str = "",
        attr: str | None = None,
    ) -> Presence:
        key = persona_key(persona)
        if not key:
            raise ValueError("persona is required")
        with self._lock:
            self._remember_locked(persona)
            current = self._presence.get(key, Presence())
            updated = Presence(
                show=str(show or current.show or "PASS"),
                stat=str(stat or current.stat),
                product=str(product or current.product),
                title=str(title or current.title),
                attr=current.attr if attr is None else str(attr),
            )
            self._presence[key] = updated
        return updated

    def presence_row(self, owner: object, target: object) -> SocialRow | None:
        target_key = persona_key(target)
        if not target_key:
            return None
        with self._lock:
            owner_key = persona_key(owner)
            if target_key == owner_key:
                return None
            return self._row_locked(owner_key, target_key)

    def _row_locked(self, owner: str, target: str) -> SocialRow:
        friends = target in self._friends.get(owner, set())
        incoming = (target, owner) in self._pending
        outgoing = (owner, target) in self._pending
        blocked = target in self._blocks.get(owner, set())
        online = self._online_locked(target)
        request = "incoming" if incoming else ("outgoing" if outgoing else "")
        if blocked:
            attr = "B"
        elif incoming:
            attr = "R"
        elif outgoing:
            attr = "P"
        elif friends:
            attr = "AT" if online else "B"
        else:
            attr = "D" if online else ""
        return SocialRow(
            user=self._display_name_locked(target),
            online=online,
            friend=friends,
            request=request,
            blocked=blocked,
            attr=attr,
            presence=self._presence.get(target),
        )

    def snapshot(self, owner: object, list_tag: str = "B") -> tuple[SocialRow, ...]:
        owner_key = persona_key(owner)
        tag = str(list_tag or "B").strip().upper()
        with self._lock:
            targets: set[str] = set()
            if tag == "I":
                targets.update(self._blocks.get(owner_key, set()))
            elif tag in {"ALL", "A"}:
                targets.update(self._known_keys_locked())
                targets.discard(owner_key)
            else:
                targets.update(self._friends.get(owner_key, set()))
                targets.update(target for requester, target in self._pending if requester == owner_key)
                targets.update(requester for requester, target in self._pending if target == owner_key)
            return tuple(
                self._row_locked(owner_key, target)
                for target in sorted(targets, key=lambda item: self._display_name_locked(item).casefold())
            )

    def recent_player_snapshot(self, owner: object, game_id: str, *, include_relations: bool = False) -> tuple[SocialRow, ...]:
        """Return encountered players even after room leave or reconnect."""
        owner_key = persona_key(owner)
        with self._lock:
            result = []
            for display in self._recent_players.snapshot(owner_key, game_id.casefold()):
                target = self._remember_locked(display)
                row = self._row_locked(owner_key, target)
                if row.blocked or owner_key in self._blocks.get(target, set()):
                    continue
                if not include_relations and (row.friend or row.request):
                    continue
                result.append(replace(row, attr="D"))
            return tuple(result)

    def game_player_snapshot(
        self,
        owner: object,
        game_id: str,
    ) -> tuple[SocialRow, ...]:
        """Return transient players sharing the viewer's current session.

        Merely being authenticated in the same title is not enough.  The
        viewer and target must both be members of the same active room/game.
        Durable friends, requests and blocks remain in their own Messenger
        categories and are omitted from this transient list.
        """

        owner_key = persona_key(owner)
        wanted_game = str(game_id or "").strip().casefold()
        if not owner_key or not wanted_game:
            return ()
        with self._lock:
            owner_sessions = {
                session
                for member_key, game, session in self._session_by_connection.values()
                if member_key == owner_key and game == wanted_game and session
            }
            if not owner_sessions:
                return ()
            rows: dict[str, SocialRow] = {}
            for session in owner_sessions:
                for row in self._session_player_rows_locked(
                    owner_key, wanted_game, session
                ):
                    rows.setdefault(row.user.casefold(), row)
            return tuple(rows[key] for key in sorted(rows))

    def _known_keys_locked(self) -> set[str]:
        keys = set(self._display)
        keys.update(persona_key(identity.persona) for identity in self._lobby_by_connection.values())
        try:
            provided = tuple(self._persona_provider())
        except Exception:
            log.exception("social persona provider failed; directory snapshot may be incomplete")
            provided = ()
        for value in provided:
            key = self._remember_locked(value)
            if key:
                keys.add(key)
        return {key for key in keys if key}

    def search(self, owner: object, query: object = "", limit: int = 20) -> tuple[SocialRow, ...]:
        owner_key = persona_key(owner)
        wanted = canonical_persona(query).casefold()
        maximum = max(1, min(100, int(limit)))
        with self._lock:
            keys = self._known_keys_locked()
            keys.discard(owner_key)
            selected = [
                key
                for key in keys
                if not wanted or wanted in self._display_name_locked(key).casefold()
            ]
            selected.sort(key=lambda item: (not self._online_locked(item), self._display_name_locked(item).casefold()))
            return tuple(self._row_locked(owner_key, key) for key in selected[:maximum])

    def request_friend(self, owner: object, target: object) -> RelationResult:
        if self.database is not None:
            return self._sqlite_request_friend(owner, target)
        owner_key = persona_key(owner)
        target_key = persona_key(target)
        if not owner_key or not target_key:
            return RelationResult(False, "missing_persona")
        if owner_key == target_key:
            return RelationResult(False, "same_identity")
        with self._lock:
            self._remember_locked(owner)
            self._remember_locked(target)
            if target_key in self._blocks.get(owner_key, set()) or owner_key in self._blocks.get(target_key, set()):
                return RelationResult(False, "blocked")
            if target_key in self._friends.get(owner_key, set()):
                return RelationResult(True, "already_friends", False)
            if (target_key, owner_key) in self._pending:
                self._pending.discard((target_key, owner_key))
                self._friends.setdefault(owner_key, set()).add(target_key)
                self._friends.setdefault(target_key, set()).add(owner_key)
                self._save_locked()
                result = RelationResult(True, "accepted", True)
            elif (owner_key, target_key) in self._pending:
                result = RelationResult(True, "already_pending", False)
            else:
                self._pending.add((owner_key, target_key))
                self._save_locked()
                result = RelationResult(True, "requested", True)
        return result

    def respond_friend(self, owner: object, requester: object, accept: bool) -> RelationResult:
        if self.database is not None:
            return self._sqlite_respond_friend(owner, requester, accept)
        owner_key = persona_key(owner)
        requester_key = persona_key(requester)
        if not owner_key or not requester_key:
            return RelationResult(False, "missing_persona")
        with self._lock:
            pair = (requester_key, owner_key)
            if pair not in self._pending:
                return RelationResult(False, "request_not_found")
            self._pending.discard(pair)
            if accept:
                self._friends.setdefault(owner_key, set()).add(requester_key)
                self._friends.setdefault(requester_key, set()).add(owner_key)
                reason = "accepted"
            else:
                reason = "declined"
            self._save_locked()
            return RelationResult(True, reason, True)

    def remove_friend(self, owner: object, target: object) -> RelationResult:
        if self.database is not None:
            return self._sqlite_remove_friend(owner, target)
        owner_key = persona_key(owner)
        target_key = persona_key(target)
        if not owner_key or not target_key:
            return RelationResult(False, "missing_persona")
        with self._lock:
            changed = False
            for left, right in ((owner_key, target_key), (target_key, owner_key)):
                bucket = self._friends.get(left)
                if bucket is not None and right in bucket:
                    bucket.discard(right)
                    changed = True
                    if not bucket:
                        self._friends.pop(left, None)
                if (left, right) in self._pending:
                    self._pending.discard((left, right))
                    changed = True
            if changed:
                self._save_locked()
            return RelationResult(True, "removed" if changed else "not_present", changed)

    def set_blocked(self, owner: object, target: object, blocked: bool) -> RelationResult:
        if self.database is not None:
            return self._sqlite_set_blocked(owner, target, blocked)
        owner_key = persona_key(owner)
        target_key = persona_key(target)
        if not owner_key or not target_key:
            return RelationResult(False, "missing_persona")
        if owner_key == target_key:
            return RelationResult(False, "same_identity")
        with self._lock:
            self._remember_locked(owner)
            self._remember_locked(target)
            bucket = self._blocks.setdefault(owner_key, set())
            changed = False
            if blocked:
                if target_key not in bucket:
                    bucket.add(target_key)
                    changed = True
                for left, right in ((owner_key, target_key), (target_key, owner_key)):
                    friends = self._friends.get(left)
                    if friends is not None and right in friends:
                        friends.discard(right)
                        changed = True
                    if (left, right) in self._pending:
                        self._pending.discard((left, right))
                        changed = True
            elif target_key in bucket:
                bucket.discard(target_key)
                changed = True
            if not bucket:
                self._blocks.pop(owner_key, None)
            if changed:
                self._save_locked()
            return RelationResult(True, "blocked" if blocked else "unblocked", changed)

    def is_blocked(self, sender: object, target: object) -> bool:
        sender_key = persona_key(sender)
        target_key = persona_key(target)
        if self.database is not None:
            if not sender_key or not target_key:
                return False
            with self.database.connect() as connection:
                ids = self._persona_ids(connection, sender_key, target_key)
                if ids is None:
                    return False
                sender_id, target_id = ids
                return connection.execute(
                    """
                    SELECT 1 FROM social_relations
                     WHERE relation='blocked'
                       AND ((source_persona_id=? AND target_persona_id=?)
                         OR (source_persona_id=? AND target_persona_id=?))
                     LIMIT 1
                    """,
                    (sender_id, target_id, target_id, sender_id),
                ).fetchone() is not None
        with self._lock:
            return (
                target_key in self._blocks.get(sender_key, set())
                or sender_key in self._blocks.get(target_key, set())
            )

    def record_report(
        self,
        reporter: object,
        target: object,
        reason: object = "",
        *,
        language: object = "",
        source: object = "",
    ) -> SocialReport:
        """Retain a bounded in-memory audit of social feedback reports."""

        report = SocialReport(
            created_at=float(self._clock()),
            reporter=canonical_persona(reporter),
            target=canonical_persona(target),
            reason=str(reason or "").strip(),
            language=str(language or "").strip(),
            source=str(source or "").strip(),
        )
        with self._lock:
            self._reports.append(report)
            del self._reports[:-128]
        return report

    def report_snapshot(self) -> tuple[SocialReport, ...]:
        with self._lock:
            return tuple(self._reports)

    def deliver(
        self,
        target: object,
        verb: str,
        fields: Iterable[tuple[str, object]],
    ) -> int:
        key = persona_key(target)
        if not key:
            return 0
        normalized = tuple((str(name), str(value)) for name, value in fields)
        with self._lock:
            sender_ids = tuple(self._controls_by_persona.get(key, set()))
            senders = [
                self._control_senders[connection][1]
                for connection in sender_ids
                if connection in self._control_senders
            ]
        delivered = 0
        for sender in senders:
            try:
                if sender(str(verb), normalized):
                    delivered += 1
            except Exception:
                log.exception(
                    "social delivery callback failed: target=%s verb=%s",
                    key,
                    verb,
                )
        return delivered
