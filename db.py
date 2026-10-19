import time

import aiosqlite

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id         INTEGER PRIMARY KEY,
    chat_id       INTEGER NOT NULL,
    account_id    INTEGER NOT NULL,
    persona       TEXT,
    last_match_id INTEGER DEFAULT 0,
    last_played   INTEGER DEFAULT 0,
    next_check    INTEGER DEFAULT 0,
    rank_tier     INTEGER,
    mmr           INTEGER,
    active        INTEGER DEFAULT 1,
    created_at    INTEGER
);
CREATE TABLE IF NOT EXISTS pending (
    match_id     INTEGER,
    tg_id        INTEGER,
    player_slot  INTEGER,
    first_seen   INTEGER,
    PRIMARY KEY (match_id, tg_id)
);
CREATE TABLE IF NOT EXISTS sent (
    match_id INTEGER,
    tg_id    INTEGER,
    sent_at  INTEGER,
    PRIMARY KEY (match_id, tg_id)
);
CREATE TABLE IF NOT EXISTS feedback (
    owner_msg_id INTEGER PRIMARY KEY,
    user_chat_id INTEGER
);
CREATE TABLE IF NOT EXISTS emoji_map (
    key             TEXT PRIMARY KEY,
    custom_emoji_id TEXT
);
CREATE TABLE IF NOT EXISTS avg_cache (
    key        TEXT PRIMARY KEY,
    data       TEXT,
    fetched_at INTEGER
);
"""


class DB:
    def __init__(self, path=DB_PATH):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def open(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        # миграции: колонки, добавленные после первого запуска
        cur = await self.conn.execute("PRAGMA table_info(users)")
        cols = {r["name"] for r in await cur.fetchall()}
        for col, decl in [("username", "TEXT"),
                          ("report_mode", "TEXT DEFAULT 'all'"),   # all | noturbo | ranked — о каких играх слать отчёты
                          ("weekly", "INTEGER DEFAULT 1"),          # итоги недели
                          ("tilt", "INTEGER DEFAULT 1"),            # предупреждение после 3 поражений подряд
                          ("loss_streak", "INTEGER DEFAULT 0"),
                          ("weekly_sent", "INTEGER DEFAULT 0")]:
            if col not in cols:
                await self.conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    # --- пользователи ---
    async def get_user(self, tg_id):
        cur = await self.conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        return await cur.fetchone()

    async def upsert_user(self, tg_id, chat_id, account_id, persona, last_match_id, last_played, rank_tier):
        await self.conn.execute(
            """INSERT INTO users (tg_id, chat_id, account_id, persona, last_match_id, last_played,
                                  rank_tier, active, created_at)
               VALUES (?,?,?,?,?,?,?,1,?)
               ON CONFLICT(tg_id) DO UPDATE SET chat_id=excluded.chat_id, account_id=excluded.account_id,
                 persona=excluded.persona, last_match_id=excluded.last_match_id,
                 last_played=excluded.last_played, rank_tier=excluded.rank_tier, active=1, mmr=NULL""",
            (tg_id, chat_id, account_id, persona, last_match_id, last_played, rank_tier, int(time.time())),
        )
        await self.conn.commit()

    async def delete_user(self, tg_id):
        await self.conn.execute("DELETE FROM users WHERE tg_id=?", (tg_id,))
        await self.conn.execute("DELETE FROM pending WHERE tg_id=?", (tg_id,))
        await self.conn.execute("DELETE FROM sent WHERE tg_id=?", (tg_id,))
        await self.conn.commit()

    async def update_user(self, tg_id, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(f"UPDATE users SET {cols} WHERE tg_id=?", (*fields.values(), tg_id))
        await self.conn.commit()

    async def users_due(self, now):
        cur = await self.conn.execute("SELECT * FROM users WHERE active=1 AND next_check<=?", (now,))
        return await cur.fetchall()

    async def count_users(self):
        cur = await self.conn.execute("SELECT COUNT(*) FROM users WHERE active=1")
        return (await cur.fetchone())[0]

    # --- очередь матчей ---
    async def add_pending(self, match_id, tg_id, player_slot):
        await self.conn.execute(
            "INSERT OR IGNORE INTO pending (match_id, tg_id, player_slot, first_seen) VALUES (?,?,?,?)",
            (match_id, tg_id, player_slot, int(time.time())),
        )
        await self.conn.commit()

    async def all_pending(self):
        cur = await self.conn.execute("SELECT * FROM pending ORDER BY match_id")
        return await cur.fetchall()

    async def remove_pending(self, match_id, tg_id):
        await self.conn.execute("DELETE FROM pending WHERE match_id=? AND tg_id=?", (match_id, tg_id))
        await self.conn.commit()

    async def mark_sent(self, match_id, tg_id):
        await self.conn.execute(
            "INSERT OR IGNORE INTO sent (match_id, tg_id, sent_at) VALUES (?,?,?)",
            (match_id, tg_id, int(time.time())),
        )
        await self.conn.commit()

    async def was_sent(self, match_id, tg_id):
        cur = await self.conn.execute("SELECT 1 FROM sent WHERE match_id=? AND tg_id=?", (match_id, tg_id))
        return await cur.fetchone() is not None

    # --- обратная связь: сообщение у владельца -> чат пользователя ---
    async def add_feedback(self, owner_msg_id, user_chat_id):
        await self.conn.execute("INSERT OR REPLACE INTO feedback VALUES (?,?)", (owner_msg_id, user_chat_id))
        await self.conn.commit()

    async def feedback_chat(self, owner_msg_id):
        cur = await self.conn.execute("SELECT user_chat_id FROM feedback WHERE owner_msg_id=?", (owner_msg_id,))
        row = await cur.fetchone()
        return row[0] if row else None

    # --- собственные эмодзи ---
    async def all_emoji(self):
        cur = await self.conn.execute("SELECT key, custom_emoji_id FROM emoji_map")
        return {r["key"]: r["custom_emoji_id"] for r in await cur.fetchall()}

    async def set_emoji(self, key, cid):
        await self.conn.execute("INSERT OR REPLACE INTO emoji_map (key, custom_emoji_id) VALUES (?,?)", (key, cid))
        await self.conn.commit()

    # --- кэш средних ---
    async def get_cache(self, key, max_age):
        cur = await self.conn.execute("SELECT data, fetched_at FROM avg_cache WHERE key=?", (key,))
        row = await cur.fetchone()
        if row and time.time() - row["fetched_at"] < max_age:
            return row["data"]
        return None

    async def set_cache(self, key, data):
        await self.conn.execute(
            "INSERT OR REPLACE INTO avg_cache (key, data, fetched_at) VALUES (?,?,?)",
            (key, data, int(time.time())),
        )
        await self.conn.commit()

    # --- админка ---
    async def set_username(self, tg_id, username):
        await self.conn.execute("UPDATE users SET username=? WHERE tg_id=?", (username, tg_id))
        await self.conn.commit()

    async def _one(self, sql, *args):
        cur = await self.conn.execute(sql, args)
        return (await cur.fetchone())[0]

    async def admin_summary(self, now):
        day, week = now - 86400, now - 7 * 86400
        return {
            "total": await self._one("SELECT COUNT(*) FROM users"),
            "active": await self._one("SELECT COUNT(*) FROM users WHERE active=1"),
            "inactive": await self._one("SELECT COUNT(*) FROM users WHERE active=0"),
            "new_day": await self._one("SELECT COUNT(*) FROM users WHERE created_at>=?", day),
            "new_week": await self._one("SELECT COUNT(*) FROM users WHERE created_at>=?", week),
            "played_week": await self._one("SELECT COUNT(*) FROM users WHERE last_played>=?", week),
            "reports_day": await self._one("SELECT COUNT(*) FROM sent WHERE sent_at>=?", day),
            "reports_week": await self._one("SELECT COUNT(*) FROM sent WHERE sent_at>=?", week),
            "reports_total": await self._one("SELECT COUNT(*) FROM sent"),
            "pending": await self._one("SELECT COUNT(*) FROM pending"),
        }

    _USERS_SQL = """SELECT u.*, (SELECT COUNT(*) FROM sent s WHERE s.tg_id=u.tg_id) AS reports,
                           (SELECT MAX(sent_at) FROM sent s WHERE s.tg_id=u.tg_id) AS last_report
                    FROM users u"""

    async def users_page(self, limit, offset):
        cur = await self.conn.execute(self._USERS_SQL + " ORDER BY u.created_at DESC LIMIT ? OFFSET ?",
                                      (limit, offset))
        return await cur.fetchall()

    async def user_card(self, tg_id):
        cur = await self.conn.execute(self._USERS_SQL + " WHERE u.tg_id=?", (tg_id,))
        return await cur.fetchone()

    async def all_users(self):
        cur = await self.conn.execute(self._USERS_SQL + " ORDER BY u.created_at")
        return await cur.fetchall()

    async def weekly_due(self, since):
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE active=1 AND weekly=1 AND COALESCE(weekly_sent,0)<?", (since,))
        return await cur.fetchall()

    async def active_chats(self):
        cur = await self.conn.execute("SELECT tg_id, chat_id FROM users WHERE active=1")
        return await cur.fetchall()
