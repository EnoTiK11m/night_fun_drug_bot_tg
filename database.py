import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Set, Tuple

import aiosqlite

from config import DB_PATH

logger = logging.getLogger(__name__)

SUBSCRIPTION_EMPTY_BACKOFF_MINUTES = (60, 120, 240, 480, 720)
SENT_POSTS_RETENTION_PER_USER = 5000
SEARCH_HISTORY_RETENTION_PER_USER = 200
SUBSCRIPTION_CLAIM_MINUTES = 5


@asynccontextmanager
async def connect_db():
    db = await aiosqlite.connect(DB_PATH, timeout=30)
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=30000")
        await db.execute("PRAGMA foreign_keys=ON")
        yield db
    finally:
        await db.close()


async def ensure_subscription_columns(db):
    cursor = await db.execute("PRAGMA table_info(subscriptions)")
    columns = {row[1] for row in await cursor.fetchall()}
    column_defs = {
        "no_new_posts_count": "INTEGER DEFAULT 0",
        "last_empty_at": "TIMESTAMP",
        "next_check_at": "TIMESTAMP",
        "exhausted_notified": "BOOLEAN DEFAULT 0",
        "processing_until": "TIMESTAMP",
        "processing_token": "TEXT",
    }
    for column, definition in column_defs.items():
        if column not in columns:
            await db.execute(f"ALTER TABLE subscriptions ADD COLUMN {column} {definition}")

    await db.execute("""
        UPDATE subscriptions
        SET next_check_at = COALESCE(
            next_check_at,
            datetime(last_sent, '+' || interval_minutes || ' minutes')
        )
    """)


def get_empty_backoff_minutes(empty_count: int, interval_minutes: int) -> int:
    index = max(0, min(empty_count - 1, len(SUBSCRIPTION_EMPTY_BACKOFF_MINUTES) - 1))
    return max(interval_minutes, SUBSCRIPTION_EMPTY_BACKOFF_MINUTES[index])


async def init_db():
    async with connect_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                last_query TEXT DEFAULT '',
                last_pid INTEGER DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER,
                tag TEXT,
                PRIMARY KEY (user_id, tag)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER,
                query TEXT,
                interval_minutes INTEGER DEFAULT 10,
                is_active BOOLEAN DEFAULT 1,
                last_sent TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                no_new_posts_count INTEGER DEFAULT 0,
                last_empty_at TIMESTAMP,
                next_check_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                exhausted_notified BOOLEAN DEFAULT 0,
                processing_until TIMESTAMP,
                processing_token TEXT,
                PRIMARY KEY (user_id, query)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS search_history (
                user_id INTEGER,
                query TEXT,
                searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER,
                post_id INTEGER,
                file_url TEXT,
                tags TEXT DEFAULT '',
                rating TEXT DEFAULT '',
                score INTEGER DEFAULT 0,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, post_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS sent_posts (
                user_id INTEGER,
                post_id INTEGER,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, post_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscription_posts (
                user_id INTEGER,
                query TEXT,
                post_id INTEGER,
                file_url TEXT,
                tags TEXT DEFAULT '',
                rating TEXT DEFAULT '',
                score INTEGER DEFAULT 0,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, query, post_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                show_caption BOOLEAN DEFAULT 1,
                show_search_query BOOLEAN DEFAULT 1,
                show_subscription_label BOOLEAN DEFAULT 1,
                show_id BOOLEAN DEFAULT 1,
                show_score BOOLEAN DEFAULT 1,
                show_rating BOOLEAN DEFAULT 1,
                show_tags BOOLEAN DEFAULT 1,
                settings_json TEXT DEFAULT '{}'
            )
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_search_history_user_query
            ON search_history (user_id, query)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_search_history_user_searched
            ON search_history (user_id, searched_at)
        """)
        await ensure_subscription_columns(db)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscriptions_next_check
            ON subscriptions (is_active, next_check_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_sent_posts_user_sent
            ON sent_posts (user_id, sent_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_favorites_user_added
            ON favorites (user_id, added_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscription_posts_user_query_sent
            ON subscription_posts (user_id, query, sent_at)
        """)

        await db.commit()


async def get_user_blacklist(user_id: int) -> Set[str]:
    async with connect_db() as db:
        cursor = await db.execute(
            "SELECT tag FROM blacklist WHERE user_id = ?",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}


async def add_to_blacklist(user_id: int, tag: str) -> bool:
    tag = tag.lower().strip()
    async with connect_db() as db:
        try:
            await db.execute(
                "INSERT INTO blacklist (user_id, tag) VALUES (?, ?)",
                (user_id, tag)
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_from_blacklist(user_id: int, tag: str) -> bool:
    tag = tag.lower().strip()
    async with connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM blacklist WHERE user_id = ? AND tag = ?",
            (user_id, tag)
        )
        await db.commit()
        return cursor.rowcount > 0


async def save_user_query(user_id: int, query: str, pid: int = 0):
    async with connect_db() as db:
        await db.execute("""
            INSERT OR REPLACE INTO users (user_id, last_query, last_pid)
            VALUES (?, ?, ?)
        """, (user_id, query, pid))
        await db.execute("""
            INSERT INTO search_history (user_id, query)
            VALUES (?, ?)
        """, (user_id, query.strip()))
        await db.execute("""
            DELETE FROM search_history
            WHERE user_id = ?
              AND rowid NOT IN (
                SELECT rowid
                FROM search_history
                WHERE user_id = ?
                ORDER BY searched_at DESC, rowid DESC
                LIMIT ?
              )
        """, (user_id, user_id, SEARCH_HISTORY_RETENTION_PER_USER))
        await db.commit()


async def get_user_query(user_id: int) -> Optional[tuple]:
    async with connect_db() as db:
        cursor = await db.execute(
            "SELECT last_query, last_pid FROM users WHERE user_id = ?",
            (user_id,)
        )
        return await cursor.fetchone()


async def get_sent_post_ids(user_id: int) -> Set[int]:
    async with connect_db() as db:
        cursor = await db.execute(
            "SELECT post_id FROM sent_posts WHERE user_id = ?",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}


async def mark_post_sent(user_id: int, post_id: int):
    async with connect_db() as db:
        await db.execute("""
            INSERT OR IGNORE INTO sent_posts (user_id, post_id)
            VALUES (?, ?)
        """, (user_id, post_id))
        await db.execute("""
            DELETE FROM sent_posts
            WHERE user_id = ?
              AND post_id NOT IN (
                SELECT post_id
                FROM sent_posts
                WHERE user_id = ?
                ORDER BY sent_at DESC, rowid DESC
                LIMIT ?
              )
        """, (user_id, user_id, SENT_POSTS_RETENTION_PER_USER))
        await db.commit()


async def get_search_history(user_id: int, limit: int = 10) -> List[str]:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT query
            FROM search_history
            WHERE user_id = ? AND query <> ''
            GROUP BY query
            ORDER BY MAX(rowid) DESC
            LIMIT ?
        """, (user_id, limit))
        rows = await cursor.fetchall()
        return [row[0] for row in rows]


async def get_user_settings(user_id: int) -> Dict[str, Any]:
    async with connect_db() as db:
        cursor = await db.execute(
            "SELECT * FROM user_settings WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()

        if row:
            settings = {
                "show_caption": bool(row[1]),
                "show_search_query": bool(row[2]),
                "show_subscription_label": bool(row[3]),
                "show_id": bool(row[4]),
                "show_score": bool(row[5]),
                "show_rating": bool(row[6]),
                "show_tags": bool(row[7]),
            }

            if row[8]:
                try:
                    json_settings = json.loads(row[8])
                    settings.update(json_settings)
                except json.JSONDecodeError:
                    logger.warning("Invalid settings JSON for user %s", user_id)

            return settings

        default_settings = {
            "show_caption": True,
            "show_search_query": True,
            "show_subscription_label": True,
            "show_id": True,
            "show_score": True,
            "show_rating": True,
            "show_tags": True,
        }
        await save_user_settings(user_id, default_settings)
        return default_settings


async def save_user_settings(user_id: int, settings: Dict[str, Any]):
    async with connect_db() as db:
        main_settings = {}
        json_settings = {}
        main_fields = [
            "show_caption",
            "show_search_query",
            "show_subscription_label",
            "show_id",
            "show_score",
            "show_rating",
            "show_tags",
        ]

        for key, value in settings.items():
            if key in main_fields:
                main_settings[key] = value
            else:
                json_settings[key] = value

        if main_settings:
            await db.execute("""
                INSERT OR REPLACE INTO user_settings
                (user_id, show_caption, show_search_query, show_subscription_label,
                 show_id, show_score, show_rating, show_tags, settings_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                main_settings.get("show_caption", True),
                main_settings.get("show_search_query", True),
                main_settings.get("show_subscription_label", True),
                main_settings.get("show_id", True),
                main_settings.get("show_score", True),
                main_settings.get("show_rating", True),
                main_settings.get("show_tags", True),
                json.dumps(json_settings),
            ))

        await db.commit()


async def update_user_setting(user_id: int, setting_name: str, value: Any):
    settings = await get_user_settings(user_id)
    settings[setting_name] = value
    await save_user_settings(user_id, settings)


async def add_subscription(user_id: int, query: str, interval_minutes: int = 10) -> bool:
    async with connect_db() as db:
        try:
            await db.execute("""
                INSERT OR REPLACE INTO subscriptions
                (
                    user_id, query, interval_minutes, is_active, last_sent,
                    no_new_posts_count, last_empty_at, next_check_at, exhausted_notified,
                    processing_until, processing_token
                )
                VALUES (?, ?, ?, 1, datetime('now', '-1 hour'), 0, NULL, datetime('now'), 0, NULL, NULL)
            """, (user_id, query.strip(), interval_minutes))
            await db.commit()
            return True
        except Exception as e:
            logger.exception("Error adding subscription: %s", e)
            return False


async def remove_subscription(user_id: int, query: str) -> bool:
    async with connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND query = ?",
            (user_id, query.strip())
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_user_subscriptions(user_id: int) -> List[Tuple[str, int]]:
    async with connect_db() as db:
        cursor = await db.execute(
            "SELECT query, interval_minutes FROM subscriptions WHERE user_id = ? AND is_active = 1",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return [(row[0], row[1]) for row in rows]


async def get_all_user_subscriptions(user_id: int) -> List[Tuple[str, int, bool, int, Optional[str]]]:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT query, interval_minutes, is_active, no_new_posts_count, next_check_at
            FROM subscriptions
            WHERE user_id = ?
        """, (user_id,))
        rows = await cursor.fetchall()
        return [(row[0], row[1], bool(row[2]), row[3] or 0, row[4]) for row in rows]


async def update_subscription_time(
    user_id: int, query: str, processing_token: Optional[str] = None
) -> bool:
    async with connect_db() as db:
        params: tuple[Any, ...]
        token_filter = ""
        if processing_token is not None:
            token_filter = " AND processing_token = ?"
            params = (user_id, query.strip(), processing_token)
        else:
            params = (user_id, query.strip())

        cursor = await db.execute(f"""
            UPDATE subscriptions
            SET last_sent = CURRENT_TIMESTAMP,
                no_new_posts_count = 0,
                last_empty_at = NULL,
                next_check_at = datetime('now', '+' || interval_minutes || ' minutes'),
                exhausted_notified = 0,
                processing_until = NULL,
                processing_token = NULL
            WHERE user_id = ? AND query = ?
            {token_filter}
        """, params)
        await db.commit()
        return cursor.rowcount > 0


async def mark_subscription_empty(
    user_id: int, query: str, processing_token: Optional[str] = None
) -> Tuple[int, int, bool]:
    query = query.strip()
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT interval_minutes, no_new_posts_count, exhausted_notified
            FROM subscriptions
            WHERE user_id = ? AND query = ?
        """, (user_id, query))
        row = await cursor.fetchone()
        if not row:
            return 0, 0, False

        interval_minutes = int(row[0] or 10)
        empty_count = int(row[1] or 0) + 1
        should_notify = not bool(row[2])
        backoff_minutes = get_empty_backoff_minutes(empty_count, interval_minutes)

        params: tuple[Any, ...]
        token_filter = ""
        if processing_token is not None:
            token_filter = " AND processing_token = ?"
            params = (empty_count, backoff_minutes, user_id, query, processing_token)
        else:
            params = (empty_count, backoff_minutes, user_id, query)

        await db.execute(f"""
            UPDATE subscriptions
            SET last_empty_at = CURRENT_TIMESTAMP,
                no_new_posts_count = ?,
                next_check_at = datetime('now', '+' || ? || ' minutes'),
                exhausted_notified = 1,
                processing_until = NULL,
                processing_token = NULL
            WHERE user_id = ? AND query = ?
            {token_filter}
        """, params)
        await db.commit()
        return empty_count, backoff_minutes, should_notify


async def update_subscription_interval(user_id: int, query: str, interval_minutes: int) -> bool:
    async with connect_db() as db:
        cursor = await db.execute("""
            UPDATE subscriptions
            SET interval_minutes = ?,
                no_new_posts_count = 0,
                last_empty_at = NULL,
                next_check_at = datetime('now', '+' || ? || ' minutes'),
                exhausted_notified = 0,
                processing_until = NULL,
                processing_token = NULL
            WHERE user_id = ? AND query = ?
        """, (interval_minutes, interval_minutes, user_id, query.strip()))
        await db.commit()
        return cursor.rowcount > 0


async def get_due_subscriptions() -> List[Tuple[int, str, int, int]]:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT user_id, query, interval_minutes, no_new_posts_count
            FROM subscriptions
            WHERE is_active = 1
            AND datetime(COALESCE(next_check_at, last_sent)) <= datetime('now')
            AND (
                processing_until IS NULL
                OR datetime(processing_until) <= datetime('now')
            )
            LIMIT 50
        """)
        return await cursor.fetchall()


async def claim_due_subscription(user_id: int, query: str) -> Optional[str]:
    token = uuid.uuid4().hex
    async with connect_db() as db:
        cursor = await db.execute("""
            UPDATE subscriptions
            SET processing_until = datetime('now', '+' || ? || ' minutes'),
                processing_token = ?
            WHERE user_id = ?
              AND query = ?
              AND is_active = 1
              AND datetime(COALESCE(next_check_at, last_sent)) <= datetime('now')
              AND (
                processing_until IS NULL
                OR datetime(processing_until) <= datetime('now')
              )
        """, (SUBSCRIPTION_CLAIM_MINUTES, token, user_id, query.strip()))
        await db.commit()
        return token if cursor.rowcount == 1 else None


async def release_subscription_claim(user_id: int, query: str, processing_token: str):
    async with connect_db() as db:
        await db.execute("""
            UPDATE subscriptions
            SET processing_until = NULL,
                processing_token = NULL
            WHERE user_id = ?
              AND query = ?
              AND processing_token = ?
        """, (user_id, query.strip(), processing_token))
        await db.commit()


async def release_stale_subscription_claims():
    async with connect_db() as db:
        await db.execute("""
            UPDATE subscriptions
            SET processing_until = NULL,
                processing_token = NULL
            WHERE processing_until IS NOT NULL
              AND datetime(processing_until) <= datetime('now', '+' || ? || ' minutes')
        """, (SUBSCRIPTION_CLAIM_MINUTES,))
        await db.commit()


async def toggle_subscription(user_id: int, query: str) -> Optional[bool]:
    async with connect_db() as db:
        cursor = await db.execute(
            "SELECT is_active FROM subscriptions WHERE user_id = ? AND query = ?",
            (user_id, query.strip())
        )
        row = await cursor.fetchone()

        if row:
            new_state = not bool(row[0])
            await db.execute("""
                UPDATE subscriptions
                SET is_active = ?,
                    next_check_at = CASE
                        WHEN ? = 1 THEN datetime('now')
                        ELSE next_check_at
                    END,
                    processing_until = NULL,
                    processing_token = NULL
                WHERE user_id = ? AND query = ?
            """, (new_state, int(new_state), user_id, query.strip()))
            await db.commit()
            return new_state

        return None


async def add_favorite(user_id: int, post: Dict[str, Any]) -> bool:
    post_id = post.get("id")
    if post_id is None:
        return False

    async with connect_db() as db:
        try:
            await db.execute("""
                INSERT INTO favorites (user_id, post_id, file_url, tags, rating, score)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                int(post_id),
                post.get("file_url", ""),
                post.get("tags", ""),
                post.get("rating", ""),
                int(post.get("score", 0) or 0),
            ))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_favorite(user_id: int, post_id: int) -> bool:
    async with connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM favorites WHERE user_id = ? AND post_id = ?",
            (user_id, post_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_favorites(
    user_id: int, limit: int = 10, offset: int = 0
) -> List[Dict[str, Any]]:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT post_id, file_url, tags, rating, score, added_at
            FROM favorites
            WHERE user_id = ?
            ORDER BY added_at DESC
            LIMIT ? OFFSET ?
        """, (user_id, limit, offset))
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "file_url": row[1],
                "tags": row[2],
                "rating": row[3],
                "score": row[4],
                "added_at": row[5],
            }
            for row in rows
        ]


async def count_favorites(user_id: int) -> int:
    async with connect_db() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM favorites WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return int(row[0] or 0)


async def add_subscription_post(user_id: int, query: str, post: Dict[str, Any]) -> bool:
    post_id = post.get("id")
    if post_id is None:
        return False

    async with connect_db() as db:
        try:
            await db.execute("""
                INSERT INTO subscription_posts
                (user_id, query, post_id, file_url, tags, rating, score)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                query.strip(),
                int(post_id),
                post.get("file_url", ""),
                post.get("tags", ""),
                post.get("rating", ""),
                int(post.get("score", 0) or 0),
            ))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def get_subscription_posts(user_id: int, query: str, limit: int = 50) -> List[Dict[str, Any]]:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT sp.post_id, sp.file_url, sp.tags, sp.rating, sp.score, sp.sent_at
            FROM subscription_posts sp
            INNER JOIN favorites f
                ON f.user_id = sp.user_id
                AND f.post_id = sp.post_id
            WHERE sp.user_id = ? AND sp.query = ?
            ORDER BY sp.sent_at DESC
            LIMIT ?
        """, (user_id, query.strip(), limit))
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "file_url": row[1],
                "tags": row[2],
                "rating": row[3],
                "score": row[4],
                "sent_at": row[5],
            }
            for row in rows
        ]


async def remove_subscription_post(user_id: int, query: str, post_id: int) -> bool:
    async with connect_db() as db:
        cursor = await db.execute("""
            DELETE FROM subscription_posts
            WHERE user_id = ? AND query = ? AND post_id = ?
        """, (user_id, query.strip(), post_id))
        await db.commit()
        return cursor.rowcount > 0
