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
