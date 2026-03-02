"""
Database layer — async SQLite via aiosqlite.
Handles schema creation and provides helpers used by plugins.
Each plugin is responsible for its own table definitions, registered via
db.register_schema(). The core schema covers message logging and the
central user registry.
"""

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"

import asyncio
import logging
import time
import aiosqlite
from pathlib import Path
from typing import Optional, List, Any

logger = logging.getLogger(__name__)

# Privilege level constants — imported by dispatcher and plugins
PRIV_MUTED   = 0   # silently ignored
PRIV_DEFAULT = 1   # read-only, auto-assigned on first contact
PRIV_ADMIN   = 15  # full access

CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    channel     TEXT,
    sender_id   TEXT NOT NULL,
    sender_name TEXT,
    content     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_ts      ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel);
CREATE INDEX IF NOT EXISTS idx_messages_sender  ON messages(sender_id);

CREATE TABLE IF NOT EXISTS plugin_meta (
    plugin  TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1
);

-- Central user registry.
-- Auto-created on first contact; display_name updated from every inbound message
-- and advertisement so names stay current without any manual action.
CREATE TABLE IF NOT EXISTS users (
    pubkey_prefix   TEXT PRIMARY KEY,
    display_name    TEXT,
    name_updated_ts INTEGER,
    first_seen_ts   INTEGER NOT NULL,
    last_seen_ts    INTEGER NOT NULL,
    privilege       INTEGER NOT NULL DEFAULT 1,
    welcomed_ts     INTEGER,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_name ON users(display_name);
"""


# Schema migrations — run on every startup; idempotent (column-exists errors ignored).
_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN welcomed_ts INTEGER",
]


class Database:
    def __init__(self, db_path: str):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db: Optional[aiosqlite.Connection] = None
        self._extra_schemas: List[str] = []

    def register_schema(self, sql: str):
        """Plugins call this before db.initialize() to add their tables."""
        self._extra_schemas.append(sql)

    async def initialize(self):
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(CORE_SCHEMA)
        for schema in self._extra_schemas:
            await self._db.executescript(schema)
        # Migrations — ALTER TABLE for columns added after initial release.
        # SQLite ignores duplicate column errors only if we catch them.
        for migration in _MIGRATIONS:
            try:
                await self._db.execute(migration)
            except Exception:
                pass  # column already exists
        await self._db.commit()
        logger.info(f"Database ready at {self._path}")

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        return await self._db.execute(sql, params)

    async def executemany(self, sql: str, params_list):
        return await self._db.executemany(sql, params_list)

    async def fetchone(self, sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        async with self._db.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> List[aiosqlite.Row]:
        async with self._db.execute(sql, params) as cur:
            return await cur.fetchall()

    async def commit(self):
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    async def log_message(self, ts: int, channel: Optional[str],
                          sender_id: str, sender_name: Optional[str], content: str):
        await self.execute(
            "INSERT INTO messages (ts, channel, sender_id, sender_name, content) VALUES (?,?,?,?,?)",
            (ts, channel, sender_id, sender_name or sender_id, content),
        )
        await self.commit()

    # ── User registry helpers ─────────────────────────────────────────────────

    async def get_user(self, pubkey_prefix: str) -> Optional[aiosqlite.Row]:
        """Fetch a user record, or None if not yet seen."""
        return await self.fetchone(
            "SELECT * FROM users WHERE pubkey_prefix=?", (pubkey_prefix,)
        )

    async def format_user(self, pubkey_prefix: str,
                          fallback_name: Optional[str] = None) -> str:
        """
        Return a log-friendly identifier in the form  'key (Name)'
        or just 'key' if no name is known.

        Used uniformly across connection and dispatcher log lines so every
        message and command can be traced to a human-readable station.
        """
        name = fallback_name
        if not name:
            row = await self.fetchone(
                "SELECT display_name FROM users WHERE pubkey_prefix=?",
                (pubkey_prefix,)
            )
            if row and row["display_name"]:
                name = row["display_name"]
        if name:
            return f"{pubkey_prefix} ({name})"
        return pubkey_prefix

    async def set_welcomed(self, pubkey_prefix: str):
        """Record the current time as the last welcome message sent to this user."""
        now = int(time.time())
        await self.execute(
            "UPDATE users SET welcomed_ts=? WHERE pubkey_prefix=?",
            (now, pubkey_prefix),
        )
        await self.commit()

    async def upsert_user(self, pubkey_prefix: str,
                          display_name: Optional[str] = None) -> int:
        """
        Get-or-create a user record. Returns the user's privilege level.

        On first contact: creates with privilege=1 (PRIV_DEFAULT).
        On subsequent contacts: updates last_seen_ts and display_name
        (only if a non-empty name is provided and it differs from stored).
        Never downgrades privilege — only explicit !setpriv can change it.
        """
        now = int(time.time())
        existing = await self.get_user(pubkey_prefix)

        if existing is None:
            await self.execute(
                """INSERT INTO users
                   (pubkey_prefix, display_name, name_updated_ts,
                    first_seen_ts, last_seen_ts, privilege)
                   VALUES (?,?,?,?,?,?)""",
                (pubkey_prefix, display_name, now if display_name else None,
                 now, now, PRIV_DEFAULT),
            )
            await self.commit()
            logger.info(
                f"New user registered: {pubkey_prefix}"
                + (f" name={display_name!r}" if display_name else "")
                + f" privilege={PRIV_DEFAULT}"
            )
            return PRIV_DEFAULT

        # Update last_seen and name if we got a better one
        old_name  = existing["display_name"]
        new_name  = display_name if display_name else old_name
        name_changed = bool(display_name and display_name != old_name)
        name_ts   = now if name_changed else existing["name_updated_ts"]

        await self.execute(
            """UPDATE users SET
               last_seen_ts=?, display_name=?, name_updated_ts=?
               WHERE pubkey_prefix=?""",
            (now, new_name, name_ts, pubkey_prefix),
        )
        await self.commit()

        if name_changed:
            logger.info(
                f"User name updated: {pubkey_prefix} "
                f"{old_name!r} → {display_name!r}"
            )

        return existing["privilege"]

    async def get_privilege(self, pubkey_prefix: str) -> int:
        """
        Return the user's privilege level, creating the record if needed.
        This is the fast path called by the dispatcher on every command.
        """
        row = await self.fetchone(
            "SELECT privilege FROM users WHERE pubkey_prefix=?", (pubkey_prefix,)
        )
        if row:
            return row["privilege"]
        # First contact — auto-create at default privilege
        return await self.upsert_user(pubkey_prefix)

    async def set_privilege(self, pubkey_prefix: str, privilege: int) -> bool:
        """
        Set a user's privilege level. Returns False if user not found.
        Clamps to valid range 0-15.
        """
        privilege = max(0, min(15, privilege))
        result = await self.execute(
            "UPDATE users SET privilege=? WHERE pubkey_prefix=?",
            (privilege, pubkey_prefix),
        )
        await self.commit()
        return result.rowcount > 0

    async def find_user(self, query: str) -> Optional[aiosqlite.Row]:
        """
        Find a user by exact pubkey_prefix or partial display_name match.
        Returns the best single match, or None.
        """
        # Exact ID match first
        row = await self.fetchone(
            "SELECT * FROM users WHERE pubkey_prefix=?", (query,)
        )
        if row:
            return row
        # Prefix match on pubkey
        row = await self.fetchone(
            "SELECT * FROM users WHERE pubkey_prefix LIKE ?", (f"{query}%",)
        )
        if row:
            return row
        # Case-insensitive name match
        return await self.fetchone(
            "SELECT * FROM users WHERE LOWER(display_name) LIKE LOWER(?)",
            (f"%{query}%",)
        )
