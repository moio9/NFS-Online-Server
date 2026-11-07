"""SQLite persistence for Carbon progression; account keys remain unchanged."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3


class SQLiteProgression:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        if read_only:
            self.connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30, isolation_level=None, check_same_thread=False)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS carbon_progression_accounts (
                account_key TEXT PRIMARY KEY, account_name TEXT NOT NULL,
                persona TEXT NOT NULL, profile_id INTEGER NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS carbon_progression_profile
                ON carbon_progression_accounts(profile_id) WHERE profile_id > 0;
            CREATE TABLE IF NOT EXISTS carbon_progression_stats (
                account_key TEXT NOT NULL REFERENCES carbon_progression_accounts(account_key),
                stat TEXT NOT NULL, value REAL, text_value TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(account_key, stat)
            );
            CREATE TABLE IF NOT EXISTS carbon_progression_roles (
                account_key TEXT NOT NULL REFERENCES carbon_progression_accounts(account_key),
                role TEXT NOT NULL, PRIMARY KEY(account_key, role)
            );
            CREATE TABLE IF NOT EXISTS carbon_progression_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS carbon_progression_races (
                race_key TEXT PRIMARY KEY, input_json TEXT NOT NULL,
                result_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)

    def read(self) -> dict:
        accounts = {}
        by_key = {}
        for key, name, persona, profile_id in self.connection.execute(
            "SELECT account_key, account_name, persona, profile_id FROM carbon_progression_accounts"
        ):
            row = dict(persona=persona, profile_id=profile_id, roles=[], stats={}, stat_texts={})
            accounts[name] = by_key[key] = row
        for key, stat, value, text in self.connection.execute(
            "SELECT account_key, stat, value, text_value FROM carbon_progression_stats"
        ):
            if value is not None:
                by_key[key]["stats"][stat] = value
            if text:
                by_key[key]["stat_texts"][stat] = text
        for key, role in self.connection.execute("SELECT account_key, role FROM carbon_progression_roles"):
            by_key[key]["roles"].append(role)
        return {"version": 1, "accounts": accounts}

    def write(self, payload: dict, previous: dict) -> None:
        old = previous.get("accounts", {})
        for name, row in payload["accounts"].items():
            if old.get(name) == row:
                continue
            key = name.strip().casefold()
            self.connection.execute("""
                INSERT INTO carbon_progression_accounts VALUES (?, ?, ?, ?)
                ON CONFLICT(account_key) DO UPDATE SET account_name=excluded.account_name,
                    persona=excluded.persona, profile_id=excluded.profile_id
            """, (key, name, row["persona"], row["profile_id"]))
            self.connection.execute("DELETE FROM carbon_progression_stats WHERE account_key=?", (key,))
            stats, texts = row["stats"], row["stat_texts"]
            self.connection.executemany("INSERT INTO carbon_progression_stats VALUES (?, ?, ?, ?)", [
                (key, stat, stats.get(stat), texts.get(stat, "")) for stat in stats.keys() | texts.keys()
            ])
            self.connection.execute("DELETE FROM carbon_progression_roles WHERE account_key=?", (key,))
            self.connection.executemany("INSERT INTO carbon_progression_roles VALUES (?, ?)", [
                (key, role) for role in row["roles"]
            ])

    def race(self, key: str, inputs: str) -> dict | None:
        row = self.connection.execute(
            "SELECT input_json, result_json FROM carbon_progression_races WHERE race_key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row[0] != inputs:
            raise ValueError("conflicting Carbon result for existing race key")
        return json.loads(row[1])

    def save_race(self, key: str, inputs: str, result: dict) -> None:
        self.connection.execute("INSERT INTO carbon_progression_races(race_key,input_json,result_json) VALUES (?,?,?)",
                                (key, inputs, json.dumps(result, sort_keys=True, allow_nan=False)))
