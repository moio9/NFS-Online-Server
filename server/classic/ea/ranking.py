"""Persistent game-neutral classic EA ranking/statistics backend.

The wire adapters decide which fields and commands a game uses. U2 has six
ranked modes while MW retains its aggregate plus four durable mode slots, so
the shared representation uses the larger U2 6x7 layout and each game clamps
and serializes only the categories it owns.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
from pathlib import Path
from threading import RLock
from typing import Callable, Iterable, Mapping

from classic.core.catalog import GameId


log = logging.getLogger(__name__)


MW_STAT_CATEGORY_COUNT = 5
U2_STAT_CATEGORY_COUNT = 6
# Maximum durable category count. Keep this alias for protocol code which is
# exclusively validating U2's six zero-based race types.
STAT_CATEGORY_COUNT = U2_STAT_CATEGORY_COUNT
STAT_FIELDS = (
    "rating",
    "wins",
    "losses",
    "disconnects",
    "rep",
    "opponents_rep",
    "opponents_rating",
)
STAT_CATEGORY_SIZE = len(STAT_FIELDS)
STAT_VALUE_COUNT = STAT_CATEGORY_COUNT * STAT_CATEGORY_SIZE
DEFAULT_CATEGORY = (9999, 0, 0, 0, 100, 101, 101)


def _safe_nonnegative(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _safe_nonnegative_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if not math.isfinite(number):
        number = float(default)
    return max(0.0, number)


def _normalize_persona(value: object) -> str:
    return str(value or "").strip().casefold()


@dataclass
class ClassicPlayerStats:
    persona: str
    values: list[int]
    mw_nos_used: list[float]

    @classmethod
    def create(
        cls,
        persona: str,
        values: Iterable[object] | None = None,
        mw_nos_used: Iterable[object] | None = None,
    ) -> "ClassicPlayerStats":
        base = list(DEFAULT_CATEGORY) * STAT_CATEGORY_COUNT
        if values is not None:
            for index, value in enumerate(values):
                if index >= STAT_VALUE_COUNT:
                    break
                base[index] = _safe_nonnegative(value)
        nos_values = [0.0] * STAT_CATEGORY_COUNT
        if mw_nos_used is not None:
            for index, value in enumerate(mw_nos_used):
                if index >= STAT_CATEGORY_COUNT:
                    break
                nos_values[index] = _safe_nonnegative_float(value)
        return cls(str(persona or "").strip() or "Player", base, nos_values)

    @staticmethod
    def _category_index(value: object) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = 0
        return max(0, min(STAT_CATEGORY_COUNT - 1, number))

    def _offset(self, category_index: object) -> int:
        return self._category_index(category_index) * STAT_CATEGORY_SIZE

    def category(self, category_index: object) -> list[int]:
        offset = self._offset(category_index)
        return list(self.values[offset : offset + STAT_CATEGORY_SIZE])

    def get(self, category_index: object, field: str) -> int:
        try:
            field_index = STAT_FIELDS.index(str(field))
        except ValueError:
            return 0
        return int(self.values[self._offset(category_index) + field_index])

    def set(self, category_index: object, field: str, value: object) -> None:
        try:
            field_index = STAT_FIELDS.index(str(field))
        except ValueError:
            return
        self.values[self._offset(category_index) + field_index] = _safe_nonnegative(value)

    def bump_result(
        self,
        category_index: object,
        outcome: str,
        *,
        include_aggregate: bool = True,
    ) -> None:
        category = self._category_index(category_index)
        normalized = str(outcome or "").strip().upper()
        targets = {category}
        if include_aggregate:
            targets.add(0)
        for target in targets:
            if normalized == "WIN":
                self.set(target, "wins", self.get(target, "wins") + 1)
                self.set(target, "rep", self.get(target, "rep") + 100)
            elif normalized in {"DISC", "DISCONNECT", "DNF"}:
                self.set(
                    target,
                    "disconnects",
                    self.get(target, "disconnects") + 1,
                )
                self.set(target, "rep", max(0, self.get(target, "rep") - 25))
            else:
                self.set(target, "losses", self.get(target, "losses") + 1)
                self.set(target, "rep", max(0, self.get(target, "rep") - 5))

    def add_mw_nos_used(
        self,
        category_index: object,
        value: object,
        *,
        include_aggregate: bool = True,
    ) -> None:
        category = self._category_index(category_index)
        amount = _safe_nonnegative_float(value)
        targets = {category}
        if include_aggregate:
            targets.add(0)
        for target in targets:
            self.mw_nos_used[target] += amount

    def get_mw_nos_used(self, category_index: object) -> float:
        return self.mw_nos_used[self._category_index(category_index)]

    def full_hex_csv(self) -> str:
        return ",".join(f"{_safe_nonnegative(value):x}" for value in self.values)

    def u2_personal_snap_hex_csv(self, category_index: object) -> str:
        """Return the stock U2 personal SNAP triple for one race mode.

        U2 requests its personal row on CHAN 12..17, one channel per mode.
        The client consumes exactly three hexadecimal values from ``S``;
        sending the complete durable 6x7 representation makes every channel
        read the first (Circuit) block.
        """

        category = self._category_index(category_index)
        row = [
            self.get(category, "rep"),
            self.get(category, "wins"),
            self.get(category, "losses"),
        ]
        return ",".join(f"{_safe_nonnegative(value):x}" for value in row)

    def u2_snap_points_hex(self, category_index: object) -> str:
        """Return stock U2's scalar ``P`` (points) for one race mode."""

        return f"{_safe_nonnegative(self.get(category_index, 'rep')):x}"

    def mw_snap_hex_csv(self, category_index: object, visible_rank: int) -> str:
        """Return the seven longs consumed from one MW ``+snp`` row."""

        values = self.category(category_index)
        row = [
            max(1, int(visible_rank)),
            values[1],
            values[2],
            values[3],
            values[4],
            values[5],
            values[6],
        ]
        return ",".join(f"{_safe_nonnegative(value):x}" for value in row)

    def mw_personal_hex_csv(self, category_index: object) -> str:
        """Return MW's full personal row with the requested mode's skill rating.

        The durable ``rating`` slot is rebuilt as the ordinal leaderboard rank.
        MW's FIND=$ snapshot instead expects the mode's mutable skill value in
        that first slot.  Keep the stored/list rank intact and only remap the
        requested mode while serializing the personal snapshot.
        """

        category = self._category_index(category_index)
        wire = list(self.values[: MW_STAT_CATEGORY_COUNT * STAT_CATEGORY_SIZE])
        wire[category * STAT_CATEGORY_SIZE] = self.get(category, "rep")
        return ",".join(f"{_safe_nonnegative(value):x}" for value in wire)

    def u2_profile_hex_csv(self) -> str:
        """Return U2's native 38-long USER ``STAT``/``LMSTAT`` layout.

        The retail client reserves eight global longs followed by six five-long
        race-mode blocks. Each mode block contains Skill Rating, ranked wins,
        ranked losses, unranked wins and unranked losses. Results accepted by
        this server are ranked, so the two unranked counters remain zero.
        """

        wire = [0] * 38
        wire[1] = sum(
            self.get(category, "disconnects")
            for category in range(U2_STAT_CATEGORY_COUNT)
        )
        for category in range(U2_STAT_CATEGORY_COUNT):
            base = 8 + category * 5
            wire[base] = self.get(category, "rep")
            wire[base + 1] = self.get(category, "wins")
            wire[base + 2] = self.get(category, "losses")
        return ",".join(f"{_safe_nonnegative(value):x}" for value in wire) + ","

    def mw_profile_hex_csv(self) -> str:
        """Return MW's 44-long USER ``STAT``/``LMSTAT`` layout.

        MW groups personal statistics into three 12-long event-mode blocks,
        with Skill Rating at indexes 8, 20, and 32 and the counters beginning
        one long later at 9, 21, and 33.  In every counter block the client
        sums indexes 0+2 as wins and 1+3 as losses and uses index 10 for its
        disconnect percentage.  Index 6 of each counter block is cumulative
        Nitrous Oxide use. Stored categories 1..3 correspond to those three
        modes; category 0 remains the server-side aggregate.
        """

        wire = [0] * 44
        wire[1] = self.get(0, "disconnects")
        for mode in range(3):
            category = mode + 1
            base = 9 + mode * 12
            wire[base - 1] = self.get(category, "rep")
            wire[base] = self.get(category, "wins")
            wire[base + 1] = self.get(category, "losses")
            wire[base + 6] = int(self.get_mw_nos_used(category) + 0.5)
            wire[base + 10] = self.get(category, "disconnects")
        return ",".join(f"{_safe_nonnegative(value):x}" for value in wire) + ","

    def to_dict(self) -> dict[str, object]:
        return {
            "persona": self.persona,
            "values": list(self.values),
            "mw_nos_used": list(self.mw_nos_used),
        }


class ClassicRankingStore:
    """Thread-safe JSON store separated by game and persona."""

    VERSION = 2

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        persona_visible: Callable[[str], bool] | None = None,
    ) -> None:
        self.path = Path(path) if path else None
        self._persona_visible = persona_visible or (lambda _persona: True)
        self._lock = RLock()
        self._by_game: dict[str, dict[str, ClassicPlayerStats]] = {}
        self._disk_signature: tuple[int, int, int] | None = None
        if self.path is not None:
            self._load()

    @staticmethod
    def _game_key(game: GameId | str) -> str:
        return game.value if isinstance(game, GameId) else str(game or "").strip()

    @staticmethod
    def _category_count(game: GameId | str) -> int:
        game_key = ClassicRankingStore._game_key(game)
        if game_key == GameId.MOST_WANTED.value:
            return MW_STAT_CATEGORY_COUNT
        return U2_STAT_CATEGORY_COUNT

    @staticmethod
    def _migrate_u2_v1_values(values: object) -> object:
        """Move the former five U2 slots into the retail six-mode order.

        Version 1 used ``URL, Circuit, Sprint, Drag, Drift`` after URL stopped
        acting as an aggregate. Retail U2 orders its six ranked modes as
        ``Circuit, Sprint, Drag, Drift, Street X, URL``.
        """

        if not isinstance(values, (list, tuple)) or len(values) < 35:
            return values
        old = list(values)
        blocks = [
            old[offset : offset + STAT_CATEGORY_SIZE]
            for offset in range(0, 35, STAT_CATEGORY_SIZE)
        ]
        migrated: list[object] = []
        for block in (
            blocks[1],
            blocks[2],
            blocks[3],
            blocks[4],
            list(DEFAULT_CATEGORY),
            blocks[0],
        ):
            migrated.extend(block)
        return migrated

    def _path_signature(self) -> tuple[int, int, int] | None:
        if self.path is None:
            return None
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return (int(stat.st_mtime_ns), int(stat.st_size), int(getattr(stat, "st_ino", 0)))

    @staticmethod
    def _decode_games(raw: object) -> dict[str, dict[str, ClassicPlayerStats]]:
        decoded: dict[str, dict[str, ClassicPlayerStats]] = {}
        try:
            version = int(raw.get("version", 1)) if isinstance(raw, dict) else 1
        except (TypeError, ValueError):
            version = 1
        games = raw.get("games", {}) if isinstance(raw, dict) else {}
        if not isinstance(games, dict):
            return decoded
        for game_key, game_value in games.items():
            personas = game_value.get("personas", {}) if isinstance(game_value, dict) else {}
            if not isinstance(personas, dict):
                continue
            bucket: dict[str, ClassicPlayerStats] = {}
            for raw_key, value in personas.items():
                if not isinstance(value, dict):
                    continue
                persona = str(value.get("persona") or raw_key or "").strip()
                if not persona:
                    continue
                values = value.get("values")
                if (
                    str(game_key) == GameId.UNDERGROUND2.value
                    and version < ClassicRankingStore.VERSION
                ):
                    values = ClassicRankingStore._migrate_u2_v1_values(values)
                stats = ClassicPlayerStats.create(
                    persona,
                    values,
                    value.get("mw_nos_used"),
                )
                bucket[_normalize_persona(persona)] = stats
            decoded[str(game_key)] = bucket
        return decoded

    def _read_disk(self) -> dict[str, dict[str, ClassicPlayerStats]]:
        assert self.path is not None
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"invalid ranking data {self.path}: {exc}") from exc
        return self._decode_games(raw)

    def _load(self) -> None:
        with self._lock:
            self._by_game = self._read_disk()
            for game_key in tuple(self._by_game):
                self._rebuild_ratings_locked(game_key)
            self._disk_signature = self._path_signature()

    def _reload_if_changed_locked(self) -> bool:
        """Reload a manual/administrative edit without restarting the server.

        Editors commonly save by replacing the JSON file atomically, so the
        signature includes mtime, size, and inode. A temporarily incomplete
        save is ignored and retried on the next statistics access instead of
        breaking the player's USER/snap request.
        """

        if self.path is None:
            return False
        signature = self._path_signature()
        if signature == self._disk_signature:
            return False
        try:
            decoded = self._read_disk()
        except ValueError as exc:
            log.warning("ranking hot reload deferred: %s", exc)
            return False
        self._by_game = decoded
        for game_key in tuple(self._by_game):
            self._rebuild_ratings_locked(game_key)
        self._disk_signature = signature
        log.info("ranking data hot reloaded from %s", self.path)
        return True

    def reload_if_changed(self) -> bool:
        with self._lock:
            return self._reload_if_changed_locked()

    def _save_locked(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.VERSION,
            "games": {
                game_key: {
                    "personas": {
                        key: stats.to_dict()
                        for key, stats in sorted(bucket.items())
                    }
                }
                for game_key, bucket in sorted(self._by_game.items())
            },
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)
        self._disk_signature = self._path_signature()

    def _bucket_locked(self, game: GameId | str) -> dict[str, ClassicPlayerStats]:
        return self._by_game.setdefault(self._game_key(game), {})

    def get_or_create(
        self,
        game: GameId | str,
        persona: str,
    ) -> ClassicPlayerStats:
        display = str(persona or "").strip() or "Player"
        key = _normalize_persona(display)
        with self._lock:
            self._reload_if_changed_locked()
            bucket = self._bucket_locked(game)
            stats = bucket.get(key)
            if stats is None:
                stats = ClassicPlayerStats.create(display)
                bucket[key] = stats
                self._rebuild_ratings_locked(self._game_key(game))
                self._save_locked()
            elif stats.persona != display:
                stats.persona = display
                self._save_locked()
            return ClassicPlayerStats.create(
                stats.persona,
                stats.values,
                stats.mw_nos_used,
            )

    def full_hex_csv(self, game: GameId | str, persona: str) -> str:
        return self.get_or_create(game, persona).full_hex_csv()

    def profile_hex_csv(self, game: GameId | str, persona: str) -> str:
        """Return the game-specific CSV consumed by the signed ``user`` frame.

        U2 consumes a 38-long layout with six compact mode blocks. MW instead
        consumes a 44-long layout with three larger event-mode blocks.
        """

        stats = self.get_or_create(game, persona)
        if self._game_key(game) == GameId.MOST_WANTED.value:
            return stats.mw_profile_hex_csv()
        return stats.u2_profile_hex_csv()

    def persona_id_for_profile(self, profile_id: int) -> int | None:
        """Return a shared persona ID when the active backend owns one."""

        del profile_id
        return None

    def personas(self, game: GameId | str) -> tuple[str, ...]:
        """Return the persisted display names for one game's statistics."""
        game_key = self._game_key(game)
        with self._lock:
            self._reload_if_changed_locked()
            return tuple(
                row.persona
                for row in sorted(
                    self._by_game.get(game_key, {}).values(),
                    key=lambda item: item.persona.casefold(),
                )
                if self._persona_visible(row.persona)
            )

    def summary(
        self,
        game: GameId | str,
        persona: str,
        category_index: int = 0,
    ) -> dict[str, int | float | str]:
        stats = self.get_or_create(game, persona)
        values = stats.category(category_index)
        return {
            "persona": stats.persona,
            "rank": values[0],
            "wins": values[1],
            "losses": values[2],
            "disconnects": values[3],
            "rep": values[4],
            "opponents_rep": values[5],
            "opponents_rating": values[6],
            "nos_used": stats.get_mw_nos_used(category_index),
        }

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
        include_key = _normalize_persona(include_persona)
        with self._lock:
            self._reload_if_changed_locked()
            bucket = self._bucket_locked(game_key)
            if include_key and include_key not in bucket:
                bucket[include_key] = ClassicPlayerStats.create(include_persona)
                self._save_locked()
            self._rebuild_ratings_locked(game_key)
            ordered = self._ordered_locked(game_key, category_index)
            window = ordered[max(0, int(start)) : max(0, int(start)) + max(0, int(limit))]
            return [
                ClassicPlayerStats.create(
                    item.persona,
                    item.values,
                    item.mw_nos_used,
                )
                for item in window
            ]

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
        # These values are consumed by the SQLite backend.  The legacy JSON
        # fallback keeps accepting the same facade without attempting to add
        # a second durable race-history source.
        del race_key, reporter_key, persona_id, race_metadata, result_metadata
        game_key = self._game_key(game)
        display = str(persona or "").strip() or "Player"
        key = _normalize_persona(display)
        opponents = [str(item or "").strip() for item in opponent_personas]
        opponents = [item for item in opponents if item and _normalize_persona(item) != key]
        category_count = self._category_count(game_key)
        category = max(0, min(category_count - 1, int(category_index)))
        include_aggregate = game_key == GameId.MOST_WANTED.value
        with self._lock:
            self._reload_if_changed_locked()
            bucket = self._bucket_locked(game_key)
            stats = bucket.setdefault(key, ClassicPlayerStats.create(display))
            stats.persona = display
            if not self._persona_visible(display):
                result = stats.category(category)
                return {
                    "persona": stats.persona,
                    "rank": result[0],
                    "wins": result[1],
                    "losses": result[2],
                    "disconnects": result[3],
                    "rep": result[4],
                    "nos_used": stats.get_mw_nos_used(category),
                }
            stats.bump_result(
                category,
                outcome,
                include_aggregate=include_aggregate,
            )
            stats.add_mw_nos_used(
                category,
                nos_used,
                include_aggregate=include_aggregate,
            )
            opponent_rows = [
                bucket.setdefault(_normalize_persona(item), ClassicPlayerStats.create(item))
                for item in opponents
            ]
            if opponent_rows:
                targets = {category}
                if include_aggregate:
                    targets.add(0)
                for target in targets:
                    average_rep = sum(row.get(target, "rep") for row in opponent_rows) // len(opponent_rows)
                    average_rank = sum(row.get(target, "rating") for row in opponent_rows) // len(opponent_rows)
                    stats.set(target, "opponents_rep", average_rep)
                    stats.set(target, "opponents_rating", average_rank)
            self._rebuild_ratings_locked(game_key)
            self._save_locked()
            result = stats.category(category)
            return {
                "persona": stats.persona,
                "rank": result[0],
                "wins": result[1],
                "losses": result[2],
                "disconnects": result[3],
                "rep": result[4],
                "nos_used": stats.get_mw_nos_used(category),
            }

    def update_fields(
        self,
        game: GameId | str,
        persona: str,
        category_index: int,
        updates: Mapping[str, object],
        *,
        relative: bool = False,
    ) -> dict[str, int | float | str]:
        """Set or increment one category and persist it immediately.

        ``rating`` is deliberately excluded because it is the ordinal rank
        rebuilt from skill/wins/losses/disconnects after every modification.
        """

        allowed = set(STAT_FIELDS) - {"rating"}
        normalized: dict[str, int] = {}
        for raw_field, raw_value in updates.items():
            field = str(raw_field).strip()
            if field not in allowed:
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
        display = str(persona or "").strip() or "Player"
        key = _normalize_persona(display)
        category_count = self._category_count(game_key)
        category = max(0, min(category_count - 1, int(category_index)))
        with self._lock:
            self._reload_if_changed_locked()
            bucket = self._bucket_locked(game_key)
            stats = bucket.setdefault(key, ClassicPlayerStats.create(display))
            stats.persona = display
            for field, value in normalized.items():
                if relative:
                    value = stats.get(category, field) + value
                stats.set(category, field, value)
            self._rebuild_ratings_locked(game_key)
            self._save_locked()
            values = stats.category(category)
            return {
                "persona": stats.persona,
                "rank": values[0],
                "wins": values[1],
                "losses": values[2],
                "disconnects": values[3],
                "rep": values[4],
                "opponents_rep": values[5],
                "opponents_rating": values[6],
                "nos_used": stats.get_mw_nos_used(category),
            }

    def reset(
        self,
        game: GameId | str,
        persona: str,
        category_index: int | None = None,
    ) -> ClassicPlayerStats:
        game_key = self._game_key(game)
        display = str(persona or "").strip() or "Player"
        key = _normalize_persona(display)
        with self._lock:
            self._reload_if_changed_locked()
            bucket = self._bucket_locked(game_key)
            stats = bucket.setdefault(key, ClassicPlayerStats.create(display))
            stats.persona = display
            if category_index is None:
                stats.values = list(DEFAULT_CATEGORY) * STAT_CATEGORY_COUNT
                stats.mw_nos_used = [0.0] * STAT_CATEGORY_COUNT
            else:
                category_count = self._category_count(game_key)
                category = max(0, min(category_count - 1, int(category_index)))
                offset = category * STAT_CATEGORY_SIZE
                stats.values[offset : offset + STAT_CATEGORY_SIZE] = list(DEFAULT_CATEGORY)
                stats.mw_nos_used[category] = 0.0
            self._rebuild_ratings_locked(game_key)
            self._save_locked()
            return ClassicPlayerStats.create(
                stats.persona,
                stats.values,
                stats.mw_nos_used,
            )

    def delete(self, game: GameId | str, persona: str) -> bool:
        game_key = self._game_key(game)
        key = _normalize_persona(persona)
        with self._lock:
            self._reload_if_changed_locked()
            bucket = self._bucket_locked(game_key)
            removed = bucket.pop(key, None) is not None
            if removed:
                self._rebuild_ratings_locked(game_key)
                self._save_locked()
            return removed

    def _ordered_locked(
        self,
        game_key: str,
        category_index: int,
    ) -> list[ClassicPlayerStats]:
        category_count = self._category_count(game_key)
        category = max(0, min(category_count - 1, int(category_index)))
        bucket = self._by_game.get(game_key, {})
        return sorted(
            (
                row
                for row in bucket.values()
                if self._persona_visible(row.persona)
            ),
            key=lambda row: (
                -row.get(category, "rep"),
                -row.get(category, "wins"),
                row.get(category, "losses"),
                row.get(category, "disconnects"),
                row.persona.casefold(),
            ),
        )

    def _rebuild_ratings_locked(self, game_key: str) -> None:
        for category in range(self._category_count(game_key)):
            for rank, row in enumerate(self._ordered_locked(game_key, category), 1):
                row.set(category, "rating", rank)
