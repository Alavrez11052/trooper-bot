"""SQLite persistence layer for the FLPD bot."""

from __future__ import annotations

import time
from typing import Any, Optional

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS guild_config (
    guild_id            INTEGER PRIMARY KEY,
    embed_color         INTEGER,
    embed_footer        TEXT,
    embed_icon          TEXT,
    embed_thumbnail     TEXT,
    mod_log_channel     INTEGER,
    discipline_log_channel INTEGER,
    clock_log_channel   INTEGER,
    timefix_channel     INTEGER,
    timefix_ping_role   INTEGER,
    onduty_role         INTEGER,
    reset_ts            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS shifts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    start_ts  INTEGER NOT NULL,
    end_ts    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_shifts_user ON shifts (guild_id, user_id, end_ts);
CREATE INDEX IF NOT EXISTS idx_shifts_open ON shifts (guild_id, end_ts);

CREATE TABLE IF NOT EXISTS adjustments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    minutes    INTEGER NOT NULL,
    reason     TEXT,
    actor_id   INTEGER NOT NULL,
    created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adj_user ON adjustments (guild_id, user_id, created_ts);

CREATE TABLE IF NOT EXISTS timefix_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    minutes     INTEGER NOT NULL,
    reason      TEXT,
    status      TEXT    NOT NULL DEFAULT 'pending',
    reviewer_id INTEGER,
    created_ts  INTEGER NOT NULL,
    reviewed_ts INTEGER,
    message_id  INTEGER,
    channel_id  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_timefix_msg ON timefix_requests (message_id);

CREATE TABLE IF NOT EXISTS discipline (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    action      TEXT    NOT NULL,
    reason      TEXT,
    actor_id    INTEGER NOT NULL,
    created_ts  INTEGER NOT NULL,
    expires_ts  INTEGER,
    active      INTEGER NOT NULL DEFAULT 1,
    void_reason TEXT,
    voided_by   INTEGER,
    role_applied INTEGER
);
CREATE INDEX IF NOT EXISTS idx_disc_user ON discipline (guild_id, user_id, active);
CREATE INDEX IF NOT EXISTS idx_disc_expiry ON discipline (active, expires_ts);
"""


def now() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()
        await self._migrate()

    async def _migrate(self) -> None:
        """Adds columns introduced after initial release, for databases created by older versions."""
        cur = await self.conn.execute("PRAGMA table_info(guild_config)")
        existing = {row[1] for row in await cur.fetchall()}
        if "timefix_ping_role" not in existing:
            await self.conn.execute(
                "ALTER TABLE guild_config ADD COLUMN timefix_ping_role INTEGER")
            await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def execute(self, query: str, *args: Any) -> aiosqlite.Cursor:
        cur = await self.conn.execute(query, args)
        await self.conn.commit()
        return cur

    async def fetchone(self, query: str, *args: Any) -> Optional[aiosqlite.Row]:
        async with self.conn.execute(query, args) as cur:
            return await cur.fetchone()

    async def fetchall(self, query: str, *args: Any) -> list[aiosqlite.Row]:
        async with self.conn.execute(query, args) as cur:
            return list(await cur.fetchall())

    # ---------- guild config ----------
    async def config(self, guild_id: int) -> aiosqlite.Row:
        await self.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", guild_id)
        return await self.fetchone("SELECT * FROM guild_config WHERE guild_id = ?", guild_id)

    async def set_config(self, guild_id: int, field: str, value: Any) -> None:
        await self.config(guild_id)
        # `field` is always a hardcoded literal from the calling cog, never user input.
        await self.execute(f"UPDATE guild_config SET {field} = ? WHERE guild_id = ?", value, guild_id)

    # ---------- shifts ----------
    async def open_shift(self, guild_id: int, user_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone(
            "SELECT * FROM shifts WHERE guild_id = ? AND user_id = ? AND end_ts IS NULL "
            "ORDER BY start_ts DESC LIMIT 1", guild_id, user_id)

    async def start_shift(self, guild_id: int, user_id: int) -> int:
        cur = await self.execute(
            "INSERT INTO shifts (guild_id, user_id, start_ts) VALUES (?, ?, ?)",
            guild_id, user_id, now())
        return cur.lastrowid

    async def end_shift(self, shift_id: int) -> int:
        """Ends the shift, returns its duration in seconds."""
        end = now()
        row = await self.fetchone("SELECT start_ts FROM shifts WHERE id = ?", shift_id)
        await self.execute("UPDATE shifts SET end_ts = ? WHERE id = ?", end, shift_id)
        return end - int(row["start_ts"])

    async def force_close_all(self, guild_id: int, user_id: int) -> None:
        await self.execute(
            "UPDATE shifts SET end_ts = ? WHERE guild_id = ? AND user_id = ? AND end_ts IS NULL",
            now(), guild_id, user_id)

    async def totals(self, guild_id: int, user_id: int, since: Optional[int]) -> tuple[int, int]:
        """Returns (worked_seconds, adjustment_seconds) for CLOSED shifts only, from `since` onward."""
        params: list[Any] = [guild_id, user_id]
        clause = ""
        if since:
            clause = " AND start_ts >= ?"
            params.append(since)
        row = await self.fetchone(
            "SELECT COALESCE(SUM(end_ts - start_ts), 0) AS secs FROM shifts "
            "WHERE guild_id = ? AND user_id = ? AND end_ts IS NOT NULL" + clause, *params)

        params = [guild_id, user_id]
        clause = ""
        if since:
            clause = " AND created_ts >= ?"
            params.append(since)
        adj = await self.fetchone(
            "SELECT COALESCE(SUM(minutes), 0) AS mins FROM adjustments "
            "WHERE guild_id = ? AND user_id = ?" + clause, *params)

        return int(row["secs"]), int(adj["mins"]) * 60

    async def leaderboard(self, guild_id: int, since: Optional[int]) -> list[tuple[int, int]]:
        params: list[Any] = [guild_id]
        clause = ""
        if since:
            clause = " AND start_ts >= ?"
            params.append(since)
        totals: dict[int, int] = {}
        for r in await self.fetchall(
                "SELECT user_id, COALESCE(SUM(end_ts - start_ts), 0) AS secs FROM shifts "
                "WHERE guild_id = ? AND end_ts IS NOT NULL" + clause + " GROUP BY user_id",
                *params):
            totals[r["user_id"]] = totals.get(r["user_id"], 0) + int(r["secs"])

        params = [guild_id]
        clause = ""
        if since:
            clause = " AND created_ts >= ?"
            params.append(since)
        for r in await self.fetchall(
                "SELECT user_id, COALESCE(SUM(minutes), 0) AS mins FROM adjustments "
                "WHERE guild_id = ?" + clause + " GROUP BY user_id", *params):
            totals[r["user_id"]] = totals.get(r["user_id"], 0) + int(r["mins"]) * 60

        return sorted(totals.items(), key=lambda x: x[1], reverse=True)

    async def reset_clock(self, guild_id: int) -> None:
        await self.set_config(guild_id, "reset_ts", now())

    # ---------- adjustments ----------
    async def add_adjustment(self, guild_id: int, user_id: int, minutes: int,
                              reason: str, actor_id: int) -> int:
        cur = await self.execute(
            "INSERT INTO adjustments (guild_id, user_id, minutes, reason, actor_id, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?)", guild_id, user_id, minutes, reason, actor_id, now())
        return cur.lastrowid

    # ---------- time-fix requests ----------
    async def create_timefix(self, guild_id: int, user_id: int, minutes: int, reason: str) -> int:
        cur = await self.execute(
            "INSERT INTO timefix_requests (guild_id, user_id, minutes, reason, created_ts) "
            "VALUES (?, ?, ?, ?, ?)", guild_id, user_id, minutes, reason, now())
        return cur.lastrowid

    async def attach_timefix_message(self, req_id: int, channel_id: int, message_id: int) -> None:
        await self.execute(
            "UPDATE timefix_requests SET channel_id = ?, message_id = ? WHERE id = ?",
            channel_id, message_id, req_id)

    async def get_timefix(self, req_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM timefix_requests WHERE id = ?", req_id)

    async def resolve_timefix(self, req_id: int, status: str, reviewer_id: int) -> None:
        await self.execute(
            "UPDATE timefix_requests SET status = ?, reviewer_id = ?, reviewed_ts = ? WHERE id = ?",
            status, reviewer_id, now(), req_id)

    # ---------- discipline ----------
    async def add_discipline(self, guild_id: int, user_id: int, action: str, reason: str,
                              actor_id: int, expires_ts: Optional[int] = None,
                              role_applied: Optional[int] = None) -> int:
        cur = await self.execute(
            "INSERT INTO discipline (guild_id, user_id, action, reason, actor_id, created_ts, "
            "expires_ts, role_applied) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            guild_id, user_id, action, reason, actor_id, now(), expires_ts, role_applied)
        return cur.lastrowid

    async def record(self, guild_id: int, user_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM discipline WHERE guild_id = ? AND user_id = ? ORDER BY created_ts DESC",
            guild_id, user_id)

    async def get_discipline(self, entry_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM discipline WHERE id = ?", entry_id)

    async def void_discipline(self, entry_id: int, voided_by: int, reason: str) -> None:
        await self.execute(
            "UPDATE discipline SET active = 0, voided_by = ?, void_reason = ? WHERE id = ?",
            voided_by, reason, entry_id)

    async def expired_suspensions(self) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM discipline WHERE active = 1 AND action = 'Suspension' "
            "AND expires_ts IS NOT NULL AND expires_ts <= ?", now())

    async def lift_suspension(self, entry_id: int) -> None:
        await self.execute("UPDATE discipline SET active = 0 WHERE id = ?", entry_id)

    # ---------- panels (persistent view re-registration on restart) ----------
    async def save_panel(self, guild_id: int, channel_id: int, message_id: int) -> None:
        await self.execute(
            "CREATE TABLE IF NOT EXISTS panels (guild_id INTEGER, channel_id INTEGER, "
            "message_id INTEGER PRIMARY KEY)")
        await self.execute(
            "INSERT OR REPLACE INTO panels (guild_id, channel_id, message_id) VALUES (?, ?, ?)",
            guild_id, channel_id, message_id)
