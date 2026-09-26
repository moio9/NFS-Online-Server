"""Share Carbon's temporary presence choice with public status readers.

The game session remains authoritative for play and message delivery.  This
table only records the Messenger connection's public visibility choice.
"""

from __future__ import annotations

import time
import sqlite3

from common.accounts import SQLiteAccountDatabase


_SCHEMA = """
CREATE TABLE IF NOT EXISTS carbon_public_presence (
    persona TEXT PRIMARY KEY COLLATE NOCASE,
    connection_id TEXT NOT NULL,
    show TEXT NOT NULL,
    updated_at REAL NOT NULL
)
"""


def set_carbon_public_presence(
    database: SQLiteAccountDatabase,
    persona: str,
    connection_id: str,
    show: str,
) -> None:
    """Record PENDING until retail PSET arrives, then its actual SHOW value."""
    with database.transaction() as connection:
        connection.execute(_SCHEMA)
        connection.execute(
            "INSERT INTO carbon_public_presence(persona,connection_id,show,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(persona) DO UPDATE SET "
            "connection_id=excluded.connection_id,show=excluded.show,updated_at=excluded.updated_at",
            (persona, connection_id, show, time.time()),
        )


def clear_carbon_public_presence(
    database: SQLiteAccountDatabase,
    persona: str,
    connection_id: str,
) -> None:
    """A late close must not erase a newer connection's presence."""
    with database.transaction() as connection:
        connection.execute(_SCHEMA)
        connection.execute(
            "DELETE FROM carbon_public_presence WHERE persona=? AND connection_id=?",
            (persona, connection_id),
        )


def website_appear_offline(database: SQLiteAccountDatabase, persona: str) -> bool:
    """Read the account's durable website visibility preference.

    Older installations may not have the website table yet. In that case the
    default preference is visible. The real login and room state is untouched.
    """
    key = database.normalize(persona)
    if not key:
        return False
    try:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT pref.appear_online "
                "FROM personas AS p "
                "JOIN web_account_preferences AS pref ON pref.account_id=p.account_id "
                "WHERE p.display_name_key=?",
                (key,),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table: web_account_preferences" in str(exc).casefold():
            return False
        raise
    return row is not None and not bool(row["appear_online"])
