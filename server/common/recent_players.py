"""Bounded, durable encounter history, separate from friend relationships."""

from itertools import combinations
import json
from pathlib import Path


class RecentPlayers:
    LIMIT = 50

    def __init__(self, database=None, path: Path | None = None):
        self.database = database
        self.path = path
        self.rows: dict[tuple[str, str, str], tuple[str, float]] = {}
        if database is not None:
            with database.transaction() as connection:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS recent_players (
                        owner_id INTEGER NOT NULL REFERENCES personas(persona_id) ON DELETE CASCADE,
                        target_id INTEGER NOT NULL REFERENCES personas(persona_id) ON DELETE CASCADE,
                        game TEXT NOT NULL,
                        last_seen REAL NOT NULL,
                        PRIMARY KEY(owner_id, game, target_id),
                        CHECK(owner_id <> target_id)
                    )
                """)
        elif path is not None and path.exists():
            for owner, game, target, display, timestamp in json.loads(path.read_text(encoding="utf-8")):
                self.rows[(owner, game, target)] = (display, float(timestamp))

    def record(self, game: str, personas: list[str], now: float) -> None:
        names = {name.casefold(): name for name in personas if name}
        if len(names) < 2:
            return
        if self.database is not None:
            with self.database.transaction() as connection:
                ids = {}
                for key in names:
                    row = connection.execute(
                        "SELECT persona_id FROM personas WHERE display_name_key=?", (key,)
                    ).fetchone()
                    if row is not None:
                        ids[key] = int(row[0])
                for left, right in combinations(ids.values(), 2):
                    for owner, target in ((left, right), (right, left)):
                        connection.execute("""
                            INSERT INTO recent_players(owner_id, target_id, game, last_seen)
                            VALUES(?,?,?,?) ON CONFLICT(owner_id, game, target_id)
                            DO UPDATE SET last_seen=excluded.last_seen
                        """, (owner, target, game, now))
                for owner in ids.values():
                    connection.execute("""
                        DELETE FROM recent_players WHERE owner_id=? AND game=? AND target_id NOT IN (
                            SELECT target_id FROM recent_players WHERE owner_id=? AND game=?
                            ORDER BY last_seen DESC, target_id LIMIT ?
                        )
                    """, (owner, game, owner, game, self.LIMIT))
            return
        for left, right in combinations(names, 2):
            for owner, target in ((left, right), (right, left)):
                self.rows[(owner, game, target)] = (names[target], now)
        for owner in names:
            keys = sorted(
                (key for key in self.rows if key[:2] == (owner, game)),
                key=lambda key: (-self.rows[key][1], key[2]),
            )
            for key in keys[self.LIMIT:]:
                self.rows.pop(key)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps([
                [*key, *value] for key, value in sorted(self.rows.items())
            ]), encoding="utf-8")
            temporary.replace(self.path)

    def snapshot(self, owner: str, game: str) -> tuple[str, ...]:
        if self.database is not None:
            with self.database.connect() as connection:
                rows = connection.execute("""
                    SELECT target.display_name FROM recent_players AS recent
                    JOIN personas AS owner ON owner.persona_id=recent.owner_id
                    JOIN personas AS target ON target.persona_id=recent.target_id
                    WHERE owner.display_name_key=? AND recent.game=?
                    ORDER BY recent.last_seen DESC, target.display_name_key LIMIT ?
                """, (owner, game, self.LIMIT)).fetchall()
            return tuple(str(row[0]) for row in rows)
        keys = sorted(
            (key for key in self.rows if key[:2] == (owner, game)),
            key=lambda key: (-self.rows[key][1], key[2]),
        )
        return tuple(self.rows[key][0] for key in keys[:self.LIMIT])
