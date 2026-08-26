"""SQLite-backed Classic ranking and race-history storage.

The Classic wire protocol continues to consume :class:`ClassicPlayerStats`.
This module only changes persistence: identities remain authoritative in the
shared account database, leaderboard positions are derived at read time, and
race results update history and aggregates in one SQLite transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from pathlib import Path
import sqlite3
import time
from typing import Iterable, Mapping
from uuid import uuid4

from classic.core.catalog import GameId
from classic.ea.ranking import (
    ClassicPlayerStats,
    ClassicRankingStore,
    DEFAULT_CATEGORY,
    MW_STAT_CATEGORY_COUNT,
    STAT_CATEGORY_COUNT,
    STAT_FIELDS,
    U2_STAT_CATEGORY_COUNT,
)
from common.accounts import SQLiteAccountDatabase


log = logging.getLogger(__name__)


_STAT_COLUMNS = {
    "wins": "wins",
    "losses": "losses",
    "disconnects": "disconnects",
    "rep": "skill",
    "opponents_rep": "opponents_skill",
    "opponents_rating": "opponents_rank",
}
_DISCONNECT_OUTCOMES = frozenset({"DISC", "DISCONNECT", "DNF"})


def _nonnegative_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _nonnegative_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if not math.isfinite(number):
        number = float(default)
    return max(0.0, number)


def _metadata_json(values: Mapping[str, object] | None) -> str:
    return json.dumps(
        dict(values or {}),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


class SQLiteClassicRankingStore(ClassicRankingStore):
    """Classic ranking API implemented over ``SQLiteAccountDatabase``.

    ``strict_personas`` makes account identities authoritative. With the
    default enabled, any unknown persona raises ``KeyError``. When disabled,
    read-only lookups return a detached default row for an unknown name;
    writes still require a real persona because all durable rows reference the
    shared ``personas`` table.
    """

    VERSION = ClassicRankingStore.VERSION

    def __init__(
        self,
        database: SQLiteAccountDatabase,
        legacy_path: str | Path | None = None,
        strict_personas: bool = True,
    ) -> None:
        self.database = database
        self.path = database.path
        self.legacy_path = Path(legacy_path) if legacy_path is not None else None
        self.strict_personas = bool(strict_personas)
        self._server_run_id = uuid4().hex
        self._maybe_import_legacy()

    @staticmethod
    def _game_key(game: GameId | str) -> str:
        return game.value if isinstance(game, GameId) else str(game or "").strip()

    @classmethod
    def _category_count(cls, game: GameId | str) -> int:
        if cls._game_key(game) == GameId.MOST_WANTED.value:
            return MW_STAT_CATEGORY_COUNT
        return U2_STAT_CATEGORY_COUNT

    @classmethod
    def _category(cls, game: GameId | str, category_index: object) -> int:
        count = cls._category_count(game)
        return max(0, min(count - 1, int(category_index)))

    @staticmethod
    def _display(persona: object) -> str:
        return str(persona or "").strip() or "Player"

    @staticmethod
    def _now() -> float:
        return time.time()

    def _persona_row(
        self,
        connection: sqlite3.Connection,
        persona: object,
        *,
        require: bool | None = None,
    ) -> sqlite3.Row | None:
        display = self._display(persona)
        row = connection.execute(
            """
            SELECT p.persona_id, p.display_name, p.display_name_key,
                   a.enabled, a.banned,
                   EXISTS(
                       SELECT 1 FROM blocked_emails AS blocked
                        WHERE blocked.email_key=a.email_key
                   ) AS email_blocked
              FROM personas AS p
              JOIN accounts AS a ON a.account_id=p.account_id
             WHERE p.display_name_key=?
             LIMIT 1
            """,
            (self.database.normalize(display),),
        ).fetchone()
        required = self.strict_personas if require is None else bool(require)
        if row is None and required:
            raise KeyError(display)
        return row

    def _persona_row_by_id(
        self,
        connection: sqlite3.Connection,
        persona_id: int,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT p.persona_id, p.display_name, p.display_name_key,
                   a.enabled, a.banned,
                   EXISTS(
                       SELECT 1 FROM blocked_emails AS blocked
                        WHERE blocked.email_key=a.email_key
                   ) AS email_blocked
              FROM personas AS p
              JOIN accounts AS a ON a.account_id=p.account_id
             WHERE p.persona_id=?
            """,
            (int(persona_id),),
        ).fetchone()
        if row is None:
            raise KeyError(str(persona_id))
        return row

    def _ensure_stats_rows(
        self,
        connection: sqlite3.Connection,
        persona_id: int,
        game_key: str,
    ) -> None:
        now = self._now()
        connection.executemany(
            """
            INSERT OR IGNORE INTO game_player_stats(
                persona_id, game, category, wins, losses, disconnects,
                skill, opponents_skill, opponents_rank, metric_total,
                created_at, updated_at
            ) VALUES(?, ?, ?, 0, 0, 0, 100, 101, 101, 0, ?, ?)
            """,
            (
                (int(persona_id), game_key, category, now, now)
                for category in range(self._category_count(game_key))
            ),
        )

    @staticmethod
    def _visibility_sql() -> str:
        return (
            " AND a.enabled=1 AND a.banned=0"
            " AND b.email_key IS NULL"
            " AND COALESCE(v.hidden, 0)=0"
        )

    def _rating_rows(
        self,
        connection: sqlite3.Connection,
        game_key: str,
        persona_id: int,
        *,
        visible_only: bool,
    ) -> dict[int, int]:
        return self._rating_rows_for_personas(
            connection,
            game_key,
            (int(persona_id),),
            visible_only=visible_only,
        ).get(int(persona_id), {})

    def _rating_rows_for_personas(
        self,
        connection: sqlite3.Connection,
        game_key: str,
        persona_ids: Iterable[int],
        *,
        visible_only: bool,
    ) -> dict[int, dict[int, int]]:
        selected = tuple(dict.fromkeys(int(value) for value in persona_ids))
        if not selected:
            return {}
        count = self._category_count(game_key)
        visibility_join = ""
        visibility_where = ""
        if visible_only:
            visibility_join = (
                " JOIN accounts AS a ON a.account_id=p.account_id"
                " LEFT JOIN blocked_emails AS b ON b.email_key=a.email_key"
                " LEFT JOIN game_leaderboard_visibility AS v"
                " ON v.persona_id=s.persona_id AND v.game=s.game"
            )
            visibility_where = self._visibility_sql()
        rows = connection.execute(
            f"""
            WITH ranked AS (
                SELECT s.persona_id, s.category,
                       ROW_NUMBER() OVER (
                           PARTITION BY s.game, s.category
                           ORDER BY s.skill DESC, s.wins DESC,
                                    s.losses ASC, s.disconnects ASC,
                                    p.display_name_key ASC
                       ) AS rating
                  FROM game_player_stats AS s
                  JOIN personas AS p ON p.persona_id=s.persona_id
                  {visibility_join}
                 WHERE s.game=? AND s.category>=0 AND s.category<?
                       {visibility_where}
            )
            SELECT persona_id, category, rating
              FROM ranked
             WHERE persona_id IN ({','.join('?' for _ in selected)})
            """,
            (game_key, count, *selected),
        ).fetchall()
        result: dict[int, dict[int, int]] = {value: {} for value in selected}
        for row in rows:
            result[int(row["persona_id"])][int(row["category"])] = int(
                row["rating"]
            )
        return result

    def _stats_for_row(
        self,
        connection: sqlite3.Connection,
        game_key: str,
        persona_row: sqlite3.Row,
        *,
        visible_ratings: bool = False,
        ratings: Mapping[int, int] | None = None,
    ) -> ClassicPlayerStats:
        persona_id = int(persona_row["persona_id"])
        if ratings is None:
            ratings = self._rating_rows(
                connection,
                game_key,
                persona_id,
                visible_only=visible_ratings,
            )
        stats = ClassicPlayerStats.create(str(persona_row["display_name"]))
        rows = connection.execute(
            """
            SELECT category, wins, losses, disconnects, skill,
                   opponents_skill, opponents_rank, metric_total
              FROM game_player_stats
             WHERE persona_id=? AND game=?
             ORDER BY category
            """,
            (persona_id, game_key),
        ).fetchall()
        count = self._category_count(game_key)
        for row in rows:
            category = int(row["category"])
            if not 0 <= category < STAT_CATEGORY_COUNT:
                continue
            stats.set(
                category,
                "rating",
                ratings.get(category, DEFAULT_CATEGORY[0])
                if category < count
                else DEFAULT_CATEGORY[0],
            )
            stats.set(category, "wins", row["wins"])
            stats.set(category, "losses", row["losses"])
            stats.set(category, "disconnects", row["disconnects"])
            stats.set(category, "rep", row["skill"])
            stats.set(category, "opponents_rep", row["opponents_skill"])
            stats.set(category, "opponents_rating", row["opponents_rank"])
            stats.mw_nos_used[category] = _nonnegative_float(row["metric_total"])
        return stats

    @staticmethod
    def _summary(
        stats: ClassicPlayerStats,
        category_index: int,
        *,
        compact: bool = False,
    ) -> dict[str, int | float | str]:
        values = stats.category(category_index)
        result: dict[str, int | float | str] = {
            "persona": stats.persona,
            "rank": values[0],
            "wins": values[1],
            "losses": values[2],
            "disconnects": values[3],
            "rep": values[4],
        }
        if not compact:
            result["opponents_rep"] = values[5]
            result["opponents_rating"] = values[6]
        result["nos_used"] = stats.get_mw_nos_used(category_index)
        return result

    def get_or_create(
        self,
        game: GameId | str,
        persona: str,
    ) -> ClassicPlayerStats:
        game_key = self._game_key(game)
        display = self._display(persona)
        expected_rows = self._category_count(game_key)
        with self.database.connect() as connection:
            row = self._persona_row(connection, display)
            if row is None:
                return ClassicPlayerStats.create(display)
            existing_rows = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM game_player_stats
                     WHERE persona_id=? AND game=?
                    """,
                    (int(row["persona_id"]), game_key),
                ).fetchone()[0]
            )
            if existing_rows >= expected_rows:
                return self._stats_for_row(
                    connection,
                    game_key,
                    row,
                    visible_ratings=True,
                )
        with self.database.transaction() as connection:
            row = self._persona_row(connection, display)
            if row is None:
                return ClassicPlayerStats.create(display)
            self._ensure_stats_rows(connection, int(row["persona_id"]), game_key)
            return self._stats_for_row(
                connection,
                game_key,
                row,
                visible_ratings=True,
            )

    def full_hex_csv(self, game: GameId | str, persona: str) -> str:
        return self.get_or_create(game, persona).full_hex_csv()

    def profile_hex_csv(self, game: GameId | str, persona: str) -> str:
        stats = self.get_or_create(game, persona)
        if self._game_key(game) == GameId.MOST_WANTED.value:
            return stats.mw_profile_hex_csv()
        return stats.u2_profile_hex_csv()

    def persona_id_for_profile(self, profile_id: int) -> int | None:
        identity = self.database.identity_for_profile(int(profile_id))
        return identity.persona_id if identity is not None else None

    def personas(self, game: GameId | str) -> tuple[str, ...]:
        game_key = self._game_key(game)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT p.display_name, p.display_name_key
                  FROM game_player_stats AS s
                  JOIN personas AS p ON p.persona_id=s.persona_id
                  JOIN accounts AS a ON a.account_id=p.account_id
                  LEFT JOIN blocked_emails AS b ON b.email_key=a.email_key
                  LEFT JOIN game_leaderboard_visibility AS v
                    ON v.persona_id=s.persona_id AND v.game=s.game
                 WHERE s.game=? AND a.enabled=1 AND a.banned=0
                       AND b.email_key IS NULL
                       AND COALESCE(v.hidden, 0)=0
                 ORDER BY p.display_name_key
                """,
                (game_key,),
            ).fetchall()
        return tuple(str(row["display_name"]) for row in rows)

    def summary(
        self,
        game: GameId | str,
        persona: str,
        category_index: int = 0,
    ) -> dict[str, int | float | str]:
        # The legacy summary API exposes the complete shared six-category
        # representation even though MW mutations clamp to its five slots.
        category = ClassicPlayerStats._category_index(category_index)
        return self._summary(self.get_or_create(game, persona), category)

    def leaderboard(
        self,
        game: GameId | str,
        category_index: int,
        *,
        start: int = 0,
        limit: int = 100,
        include_persona: str = "",
    ) -> list[ClassicPlayerStats]:
        game_key = self._game_key(game)
        category = self._category(game_key, category_index)
        offset = max(0, int(start))
        row_limit = max(0, int(limit))
        if str(include_persona or "").strip():
            needs_rows = False
            with self.database.connect() as connection:
                include = self._persona_row(connection, include_persona)
                if (
                    include is not None
                    and bool(include["enabled"])
                    and not bool(include["banned"])
                    and not bool(include["email_blocked"])
                ):
                    existing_rows = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM game_player_stats
                             WHERE persona_id=? AND game=?
                            """,
                            (int(include["persona_id"]), game_key),
                        ).fetchone()[0]
                    )
                    needs_rows = existing_rows < self._category_count(game_key)
            if needs_rows:
                with self.database.transaction() as connection:
                    include = self._persona_row(connection, include_persona)
                    assert include is not None
                    self._ensure_stats_rows(
                        connection,
                        int(include["persona_id"]),
                        game_key,
                    )
        with self.database.connect() as connection:
            board = connection.execute(
                """
                WITH ranked AS (
                    SELECT s.persona_id,
                           ROW_NUMBER() OVER (
                               ORDER BY s.skill DESC, s.wins DESC,
                                        s.losses ASC, s.disconnects ASC,
                                        p.display_name_key ASC
                           ) AS rating
                      FROM game_player_stats AS s
                      JOIN personas AS p ON p.persona_id=s.persona_id
                      JOIN accounts AS a ON a.account_id=p.account_id
                      LEFT JOIN blocked_emails AS b ON b.email_key=a.email_key
                      LEFT JOIN game_leaderboard_visibility AS v
                        ON v.persona_id=s.persona_id AND v.game=s.game
                     WHERE s.game=? AND s.category=?
                           AND a.enabled=1 AND a.banned=0
                           AND b.email_key IS NULL
                           AND COALESCE(v.hidden, 0)=0
                )
                SELECT persona_id, rating
                  FROM ranked
                 ORDER BY rating
                 LIMIT ? OFFSET ?
                """,
                (game_key, category, row_limit, offset),
            ).fetchall()
            persona_ids = [int(item["persona_id"]) for item in board]
            ratings = self._rating_rows_for_personas(
                connection,
                game_key,
                persona_ids,
                visible_only=True,
            )
            return [
                self._stats_for_row(
                    connection,
                    game_key,
                    self._persona_row_by_id(connection, int(item["persona_id"])),
                    visible_ratings=True,
                    ratings=ratings.get(int(item["persona_id"]), {}),
                )
                for item in board
            ]

    def _opponent_averages(
        self,
        connection: sqlite3.Connection,
        game_key: str,
        opponent_rows: list[sqlite3.Row],
        targets: set[int],
    ) -> dict[int, tuple[int, int]]:
        if not opponent_rows:
            return {}
        result: dict[int, tuple[int, int]] = {}
        for category in targets:
            skills: list[int] = []
            ratings: list[int] = []
            for row in opponent_rows:
                if (
                    not bool(row["enabled"])
                    or bool(row["banned"])
                    or bool(row["email_blocked"])
                ):
                    continue
                stat = connection.execute(
                    """
                    SELECT skill FROM game_player_stats
                     WHERE persona_id=? AND game=? AND category=?
                    """,
                    (int(row["persona_id"]), game_key, category),
                ).fetchone()
                if stat is None:
                    continue
                skills.append(int(stat["skill"]))
                ratings.append(
                    self._rating_rows(
                        connection,
                        game_key,
                        int(row["persona_id"]),
                        visible_only=True,
                    ).get(category, DEFAULT_CATEGORY[0])
                )
            if skills:
                result[category] = (
                    sum(skills) // len(skills),
                    sum(ratings) // len(ratings),
                )
        return result

    def _race_result_inserted(
        self,
        connection: sqlite3.Connection,
        *,
        game_key: str,
        persona_id: int,
        category: int,
        outcome: str,
        nos_used: float,
        race_key: str | None,
        reporter_key: str | int | None,
        race_metadata: Mapping[str, object] | None,
        result_metadata: Mapping[str, object] | None,
    ) -> tuple[bool, int, bool]:
        race_values = dict(race_metadata or {})
        result_values = dict(result_metadata or {})
        server_run_id = str(
            race_values.get("server_run_id") or self._server_run_id
        ).strip() or self._server_run_id
        session_id = str(
            race_key or race_values.get("session_id") or uuid4().hex
        ).strip() or uuid4().hex
        ranked = 1 if bool(race_values.get("ranked", True)) else 0
        now = self._now()
        connection.execute(
            """
            INSERT INTO game_races(
                game, server_run_id, session_id, category, ranked, track,
                direction, laps, status, metadata_json, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game, server_run_id, session_id) DO NOTHING
            """,
            (
                game_key,
                server_run_id,
                session_id,
                category,
                ranked,
                str(race_values.get("track") or ""),
                _nonnegative_int(race_values.get("direction")),
                _nonnegative_int(race_values.get("laps")),
                str(race_values.get("status") or "reported"),
                _metadata_json(race_values),
                now,
                now,
            ),
        )
        race = connection.execute(
            """
            SELECT race_id, category, ranked, track, direction, laps, status
              FROM game_races
             WHERE game=? AND server_run_id=? AND session_id=?
            """,
            (game_key, server_run_id, session_id),
        ).fetchone()
        assert race is not None
        metadata_conflict = int(race["category"]) != category
        if "ranked" in race_values:
            metadata_conflict = metadata_conflict or (
                int(race["ranked"]) != (1 if bool(race_values["ranked"]) else 0)
            )
        if "track" in race_values:
            metadata_conflict = metadata_conflict or (
                str(race["track"]) != str(race_values.get("track") or "")
            )
        if "direction" in race_values:
            metadata_conflict = metadata_conflict or (
                int(race["direction"])
                != _nonnegative_int(race_values.get("direction"))
            )
        if "laps" in race_values:
            metadata_conflict = metadata_conflict or (
                int(race["laps"]) != _nonnegative_int(race_values.get("laps"))
            )
        if metadata_conflict:
            connection.execute(
                "UPDATE game_races SET status='disputed', updated_at=? WHERE race_id=?",
                (now, int(race["race_id"])),
            )
        elif (
            str(race_values.get("status") or "").strip() == "complete"
            and str(race["status"]) != "disputed"
        ):
            connection.execute(
                "UPDATE game_races SET status='complete', updated_at=? WHERE race_id=?",
                (now, int(race["race_id"])),
            )
        reporter = str(reporter_key if reporter_key is not None else persona_id)
        result_values.setdefault("reporter_key", reporter)
        disconnected = 1 if outcome in _DISCONNECT_OUTCOMES else 0
        inserted = connection.execute(
            """
            INSERT INTO game_race_results(
                race_id, persona_id, reporter_key, category,
                outcome, place, disconnected,
                elapsed_ms, best_lap_ms, best_drift, nos_used, source,
                metadata_json, aggregate_applied, reported_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                int(race["race_id"]),
                int(persona_id),
                reporter,
                category,
                outcome,
                _nonnegative_int(result_values.get("place")),
                disconnected,
                _nonnegative_int(result_values.get("elapsed_ms")),
                _nonnegative_int(result_values.get("best_lap_ms")),
                _nonnegative_int(result_values.get("best_drift")),
                nos_used,
                str(result_values.get("source") or ""),
                _metadata_json(result_values),
                now,
            ),
        ).rowcount > 0
        return inserted, int(race["race_id"]), bool(race["ranked"])

    def record_result(
        self,
        game: GameId | str,
        persona: str,
        *,
        category_index: int = 0,
        outcome: str,
        opponent_personas: Iterable[str] = (),
        nos_used: float = 0.0,
        race_key: str | None = None,
        reporter_key: str | int | None = None,
        persona_id: int | None = None,
        race_metadata: Mapping[str, object] | None = None,
        result_metadata: Mapping[str, object] | None = None,
    ) -> dict[str, int | float | str]:
        game_key = self._game_key(game)
        category = self._category(game_key, category_index)
        display = self._display(persona)
        normalized_outcome = str(outcome or "").strip().upper() or "LOSS"
        metric = _nonnegative_float(nos_used)
        target_key = self.database.normalize(display)
        opponent_names = [self._display(item) for item in opponent_personas]
        opponent_names = [
            item
            for item in opponent_names
            if self.database.normalize(item) != target_key
        ]
        include_aggregate = game_key == GameId.MOST_WANTED.value
        targets = {category}
        if include_aggregate:
            targets.add(0)

        with self.database.transaction() as connection:
            row = (
                self._persona_row_by_id(connection, persona_id)
                if persona_id is not None
                else self._persona_row(connection, display, require=True)
            )
            assert row is not None
            persona_id = int(row["persona_id"])
            self._ensure_stats_rows(connection, persona_id, game_key)
            if (
                not bool(row["enabled"])
                or bool(row["banned"])
                or bool(row["email_blocked"])
            ):
                log.warning(
                    "ignored classic result from inactive account: game=%s persona=%s",
                    game_key,
                    row["display_name"],
                )
                return self._summary(
                    self._stats_for_row(
                        connection,
                        game_key,
                        row,
                        visible_ratings=True,
                    ),
                    category,
                    compact=True,
                )
            opponents: list[sqlite3.Row] = []
            for opponent in opponent_names:
                opponent_row = self._persona_row(
                    connection,
                    opponent,
                    require=self.strict_personas,
                )
                if opponent_row is None:
                    continue
                self._ensure_stats_rows(
                    connection,
                    int(opponent_row["persona_id"]),
                    game_key,
                )
                opponents.append(opponent_row)
            averages = self._opponent_averages(
                connection,
                game_key,
                opponents,
                targets,
            )
            inserted, race_id, race_ranked = self._race_result_inserted(
                connection,
                game_key=game_key,
                persona_id=persona_id,
                category=category,
                outcome=normalized_outcome,
                nos_used=metric,
                race_key=race_key,
                reporter_key=reporter_key,
                race_metadata=race_metadata,
                result_metadata=result_metadata,
            )
            if inserted and race_ranked:
                wins = 1 if normalized_outcome == "WIN" else 0
                disconnected = 1 if normalized_outcome in _DISCONNECT_OUTCOMES else 0
                losses = 0 if wins or disconnected else 1
                skill_delta = 100 if wins else (-25 if disconnected else -5)
                now = self._now()
                for target in targets:
                    average = averages.get(target)
                    if average is None:
                        connection.execute(
                            """
                            UPDATE game_player_stats
                               SET wins=wins+?, losses=losses+?,
                                   disconnects=disconnects+?,
                                   skill=MAX(0, skill+?),
                                   metric_total=metric_total+?, updated_at=?
                             WHERE persona_id=? AND game=? AND category=?
                            """,
                            (
                                wins,
                                losses,
                                disconnected,
                                skill_delta,
                                metric,
                                now,
                                persona_id,
                                game_key,
                                target,
                            ),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE game_player_stats
                               SET wins=wins+?, losses=losses+?,
                                   disconnects=disconnects+?,
                                   skill=MAX(0, skill+?),
                                   opponents_skill=?, opponents_rank=?,
                                   metric_total=metric_total+?, updated_at=?
                             WHERE persona_id=? AND game=? AND category=?
                            """,
                            (
                                wins,
                                losses,
                                disconnected,
                                skill_delta,
                                average[0],
                                average[1],
                                metric,
                                now,
                                persona_id,
                                game_key,
                                target,
                            ),
                        )
                connection.execute(
                    """
                    UPDATE game_race_results SET aggregate_applied=1
                     WHERE race_id=? AND persona_id=?
                    """,
                    (race_id, persona_id),
                )
                connection.execute(
                    "UPDATE game_races SET updated_at=? WHERE race_id=?",
                    (now, race_id),
                )
            stats = self._stats_for_row(
                connection,
                game_key,
                row,
                visible_ratings=True,
            )
            return self._summary(stats, category, compact=True)

    def update_fields(
        self,
        game: GameId | str,
        persona: str,
        category_index: int,
        updates: Mapping[str, object],
        *,
        relative: bool = False,
    ) -> dict[str, int | float | str]:
        normalized: dict[str, int] = {}
        for raw_field, raw_value in updates.items():
            field = str(raw_field).strip()
            if field not in _STAT_COLUMNS:
                raise ValueError(f"unsupported statistics field: {field}")
            try:
                value = int(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid value for {field}: {raw_value!r}") from exc
            if not relative and value < 0:
                raise ValueError(f"{field} must not be negative")
            normalized[field] = value
        if not normalized:
            raise ValueError("at least one statistics field is required")

        game_key = self._game_key(game)
        category = self._category(game_key, category_index)
        with self.database.transaction() as connection:
            row = self._persona_row(connection, persona, require=True)
            assert row is not None
            persona_id = int(row["persona_id"])
            self._ensure_stats_rows(connection, persona_id, game_key)
            now = self._now()
            for field, value in normalized.items():
                column = _STAT_COLUMNS[field]
                if relative:
                    connection.execute(
                        f"""
                        UPDATE game_player_stats
                           SET {column}=MAX(0, {column}+?), updated_at=?
                         WHERE persona_id=? AND game=? AND category=?
                        """,
                        (value, now, persona_id, game_key, category),
                    )
                else:
                    connection.execute(
                        f"""
                        UPDATE game_player_stats
                           SET {column}=?, updated_at=?
                         WHERE persona_id=? AND game=? AND category=?
                        """,
                        (value, now, persona_id, game_key, category),
                    )
            stats = self._stats_for_row(
                connection,
                game_key,
                row,
                visible_ratings=True,
            )
            return self._summary(stats, category)

    def reset(
        self,
        game: GameId | str,
        persona: str,
        category_index: int | None = None,
    ) -> ClassicPlayerStats:
        game_key = self._game_key(game)
        with self.database.transaction() as connection:
            row = self._persona_row(connection, persona, require=True)
            assert row is not None
            persona_id = int(row["persona_id"])
            self._ensure_stats_rows(connection, persona_id, game_key)
            parameters: list[object] = [self._now(), persona_id, game_key]
            category_clause = ""
            if category_index is not None:
                category_clause = " AND category=?"
                parameters.append(self._category(game_key, category_index))
            connection.execute(
                f"""
                UPDATE game_player_stats
                   SET wins=0, losses=0, disconnects=0, skill=100,
                       opponents_skill=101, opponents_rank=101,
                       metric_total=0, updated_at=?
                 WHERE persona_id=? AND game=?{category_clause}
                """,
                tuple(parameters),
            )
            return self._stats_for_row(
                connection,
                game_key,
                row,
                visible_ratings=True,
            )

    def delete(self, game: GameId | str, persona: str) -> bool:
        game_key = self._game_key(game)
        with self.database.transaction() as connection:
            row = self._persona_row(connection, persona, require=False)
            if row is None:
                return False
            removed = connection.execute(
                "DELETE FROM game_player_stats WHERE persona_id=? AND game=?",
                (int(row["persona_id"]), game_key),
            ).rowcount
            return removed > 0

    def reload_if_changed(self) -> bool:
        """SQLite readers observe committed changes without file hot reloads."""

        return False

    def _maybe_import_legacy(self) -> None:
        path = self.legacy_path
        if path is None or not path.exists():
            return
        source_path = str(path.resolve())
        # The marker check is repeated under BEGIN IMMEDIATE below to close
        # the race between two processes starting against an empty database.
        with self.database.connect() as connection:
            already_imported = connection.execute(
                "SELECT 1 FROM legacy_imports WHERE source_path=?",
                (source_path,),
            ).fetchone()
        if already_imported is not None:
            return
        try:
            payload_bytes = path.read_bytes()
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise ValueError(f"invalid ranking data {path}: {exc}") from exc
        decoded = ClassicRankingStore._decode_games(payload)
        source_sha256 = hashlib.sha256(payload_bytes).hexdigest()

        with self.database.transaction() as connection:
            imported = connection.execute(
                "SELECT 1 FROM legacy_imports WHERE source_path=?",
                (source_path,),
            ).fetchone()
            if imported is not None:
                return
            populated = connection.execute(
                "SELECT 1 FROM game_player_stats LIMIT 1"
            ).fetchone()
            has_legacy_rows = any(bucket for bucket in decoded.values())
            if populated is not None and has_legacy_rows:
                raise ValueError(
                    "cannot import legacy ranking data into a populated "
                    f"account database: {path}"
                )
            rows_imported = 0
            for game_key, bucket in decoded.items():
                normalized_game = self._game_key(game_key)
                for legacy_stats in bucket.values():
                    persona_row = self._persona_row(
                        connection,
                        legacy_stats.persona,
                        require=True,
                    )
                    assert persona_row is not None
                    persona_id = int(persona_row["persona_id"])
                    self._ensure_stats_rows(connection, persona_id, normalized_game)
                    now = self._now()
                    for category in range(self._category_count(normalized_game)):
                        values = legacy_stats.category(category)
                        connection.execute(
                            """
                            UPDATE game_player_stats
                               SET wins=?, losses=?, disconnects=?, skill=?,
                                   opponents_skill=?, opponents_rank=?,
                                   metric_total=?, updated_at=?
                             WHERE persona_id=? AND game=? AND category=?
                            """,
                            (
                                values[STAT_FIELDS.index("wins")],
                                values[STAT_FIELDS.index("losses")],
                                values[STAT_FIELDS.index("disconnects")],
                                values[STAT_FIELDS.index("rep")],
                                values[STAT_FIELDS.index("opponents_rep")],
                                values[STAT_FIELDS.index("opponents_rating")],
                                legacy_stats.get_mw_nos_used(category),
                                now,
                                persona_id,
                                normalized_game,
                                category,
                            ),
                        )
                        rows_imported += 1
            connection.execute(
                """
                INSERT INTO legacy_imports(
                    source_path, source_sha256, rows_imported, imported_at
                ) VALUES(?, ?, ?, ?)
                """,
                (source_path, source_sha256, rows_imported, self._now()),
            )
        log.info(
            "imported %d classic ranking rows from %s",
            rows_imported,
            path,
        )
