import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import aiosqlite

from config import DB_PATH

logger = logging.getLogger(__name__)

SUBSCRIPTION_EMPTY_BACKOFF_MINUTES = (60, 120, 240, 480, 720)
SENT_POSTS_RETENTION_PER_USER = 5000
SEARCH_HISTORY_RETENTION_PER_USER = 200
SUBSCRIPTION_CLAIM_MINUTES = 5
SUBSCRIPTION_CACHE_TTL_MINUTES = 60
SUBSCRIPTION_CACHE_MIN_AVAILABLE = 20
SUBSCRIPTION_PAUSE_SETTING = "subscription_pause_until"
SQLITE_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


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


async def ensure_subscription_cache_columns(db):
    cursor = await db.execute("PRAGMA table_info(subscription_cache)")
    columns = {row[1] for row in await cursor.fetchall()}
    column_defs = {
        "sample_url": "TEXT DEFAULT ''",
        "preview_url": "TEXT DEFAULT ''",
    }
    for column, definition in column_defs.items():
        if column not in columns:
            await db.execute(f"ALTER TABLE subscription_cache ADD COLUMN {column} {definition}")


async def ensure_media_post_columns(db, table_name: str):
    cursor = await db.execute(f"PRAGMA table_info({table_name})")
    columns = {row[1] for row in await cursor.fetchall()}
    column_defs = {
        "sample_url": "TEXT DEFAULT ''",
        "preview_url": "TEXT DEFAULT ''",
    }
    for column, definition in column_defs.items():
        if column not in columns:
            await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column} {definition}")


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
                sample_url TEXT DEFAULT '',
                preview_url TEXT DEFAULT '',
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
                sample_url TEXT DEFAULT '',
                preview_url TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                rating TEXT DEFAULT '',
                score INTEGER DEFAULT 0,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, query, post_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS post_cache (
                post_id INTEGER PRIMARY KEY,
                file_url TEXT DEFAULT '',
                sample_url TEXT DEFAULT '',
                preview_url TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                rating TEXT DEFAULT '',
                score INTEGER DEFAULT 0,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscription_cache (
                user_id INTEGER,
                query TEXT,
                post_id INTEGER,
                file_url TEXT,
                sample_url TEXT DEFAULT '',
                preview_url TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                rating TEXT DEFAULT '',
                score INTEGER DEFAULT 0,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscription_cache_lookup
            ON subscription_cache (user_id, query, cached_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_post_cache_cached
            ON post_cache (cached_at)
        """)
        await ensure_media_post_columns(db, "favorites")
        await ensure_media_post_columns(db, "subscription_posts")
        await ensure_subscription_cache_columns(db)

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


def _post_from_row(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "file_url": row[1],
        "sample_url": row[2] or "",
        "preview_url": row[3] or "",
        "tags": row[4] or "",
        "rating": row[5] or "",
        "score": row[6] or 0,
    }


def _normalize_post(post: Dict[str, Any]) -> Optional[tuple[int, str, str, str, str, str, int]]:
    try:
        post_id = int(post.get("id"))
    except (TypeError, ValueError):
        return None

    return (
        post_id,
        post.get("file_url", "") or "",
        post.get("sample_url", "") or "",
        post.get("preview_url", "") or "",
        post.get("tags", "") or "",
        post.get("rating", "") or "",
        int(post.get("score") or 0),
    )


async def cache_post(post: Dict[str, Any]) -> bool:
    normalized = _normalize_post(post)
    if normalized is None:
        return False

    async with connect_db() as db:
        await db.execute("""
            INSERT INTO post_cache
            (post_id, file_url, sample_url, preview_url, tags, rating, score, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(post_id) DO UPDATE SET
                file_url = COALESCE(NULLIF(excluded.file_url, ''), post_cache.file_url),
                sample_url = COALESCE(NULLIF(excluded.sample_url, ''), post_cache.sample_url),
                preview_url = COALESCE(NULLIF(excluded.preview_url, ''), post_cache.preview_url),
                tags = COALESCE(NULLIF(excluded.tags, ''), post_cache.tags),
                rating = COALESCE(NULLIF(excluded.rating, ''), post_cache.rating),
                score = CASE WHEN excluded.score != 0 THEN excluded.score ELSE post_cache.score END,
                cached_at = CURRENT_TIMESTAMP
        """, normalized)
        await db.commit()
        return True


async def get_cached_post(post_id: int) -> Optional[Dict[str, Any]]:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT post_id, file_url, sample_url, preview_url, tags, rating, score
            FROM post_cache
            WHERE post_id = ?
        """, (post_id,))
        row = await cursor.fetchone()
        return _post_from_row(row) if row else None


async def get_subscription_cache(
    user_id: int, query: str
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT post_id, file_url, sample_url, preview_url, tags, rating, score
            FROM subscription_cache
            WHERE user_id = ? AND query = ?
        """, (user_id, query.strip()))
        posts = [_post_from_row(row) for row in await cursor.fetchall()]

        cursor = await db.execute("""
            SELECT MIN(cached_at)
            FROM subscription_cache
            WHERE user_id = ? AND query = ?
        """, (user_id, query.strip()))
        row = await cursor.fetchone()
        return posts, row[0] if row and row[0] else None


async def is_subscription_cache_stale(user_id: int, query: str) -> bool:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT 1
            FROM subscription_cache
            WHERE user_id = ?
              AND query = ?
              AND datetime(cached_at) >= datetime('now', '-' || ? || ' minutes')
            LIMIT 1
        """, (user_id, query.strip(), SUBSCRIPTION_CACHE_TTL_MINUTES))
        return await cursor.fetchone() is None


async def replace_subscription_cache(
    user_id: int, query: str, posts: List[Dict[str, Any]]
) -> Dict[str, int]:
    query = query.strip()
    seen_post_ids: set[int] = set()
    rows = []
    for post in posts:
        try:
            post_id = int(post.get("id"))
        except (TypeError, ValueError):
            continue

        file_url = post.get("file_url")
        if not file_url or post_id in seen_post_ids:
            continue

        seen_post_ids.add(post_id)
        rows.append((
            user_id,
            query,
            post_id,
            file_url,
            post.get("sample_url", "") or "",
            post.get("preview_url", "") or "",
            post.get("tags", "") or "",
            post.get("rating", "") or "",
            int(post.get("score") or 0),
        ))

    async with connect_db() as db:
        existing_ids: set[int] = set()
        if rows:
            cursor = await db.execute("""
                SELECT post_id
                FROM subscription_cache
                WHERE user_id = ? AND query = ?
            """, (user_id, query))
            existing_ids = {int(row[0]) for row in await cursor.fetchall()}

        if rows:
            await db.executemany("""
                INSERT OR REPLACE INTO subscription_cache
                (user_id, query, post_id, file_url, sample_url, preview_url, tags, rating, score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            await db.executemany("""
                INSERT INTO post_cache
                (post_id, file_url, sample_url, preview_url, tags, rating, score, cached_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(post_id) DO UPDATE SET
                    file_url = COALESCE(NULLIF(excluded.file_url, ''), post_cache.file_url),
                    sample_url = COALESCE(NULLIF(excluded.sample_url, ''), post_cache.sample_url),
                    preview_url = COALESCE(NULLIF(excluded.preview_url, ''), post_cache.preview_url),
                    tags = COALESCE(NULLIF(excluded.tags, ''), post_cache.tags),
                    rating = COALESCE(NULLIF(excluded.rating, ''), post_cache.rating),
                    score = CASE WHEN excluded.score != 0 THEN excluded.score ELSE post_cache.score END,
                    cached_at = CURRENT_TIMESTAMP
            """, [
                (post_id, file_url, sample_url, preview_url, tags, rating, score)
                for (
                    _user_id,
                    _query,
                    post_id,
                    file_url,
                    sample_url,
                    preview_url,
                    tags,
                    rating,
                    score,
                ) in rows
            ])
        cursor = await db.execute("""
            SELECT COUNT(*)
            FROM subscription_cache
            WHERE user_id = ? AND query = ?
        """, (user_id, query))
        row = await cursor.fetchone()
        await db.commit()
        return {
            "api": len(rows),
            "new": sum(1 for row in rows if row[2] not in existing_ids),
            "total": int(row[0] or 0),
        }


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

            settings.setdefault("show_tags_button", True)
            return settings

        default_settings = {
            "show_caption": True,
            "show_search_query": True,
            "show_subscription_label": True,
            "show_id": True,
            "show_score": True,
            "show_rating": True,
            "show_tags": True,
            "show_tags_button": True,
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


def _parse_sqlite_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, SQLITE_TIMESTAMP_FORMAT)
    except ValueError:
        logger.warning("Invalid SQLite timestamp value: %s", value)
        return None


async def _get_settings_json(db, user_id: int) -> Dict[str, Any]:
    cursor = await db.execute(
        "SELECT settings_json FROM user_settings WHERE user_id = ?",
        (user_id,),
    )
    row = await cursor.fetchone()
    if not row or not row[0]:
        return {}
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        logger.warning("Invalid settings JSON for user %s", user_id)
        return {}


async def _save_settings_json(db, user_id: int, settings_json: Dict[str, Any]):
    await db.execute("""
        INSERT INTO user_settings (user_id, settings_json)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET settings_json = excluded.settings_json
    """, (user_id, json.dumps(settings_json)))


async def _get_active_subscription_pause_until(db, user_id: int) -> Optional[str]:
    settings_json = await _get_settings_json(db, user_id)
    pause_until = settings_json.get(SUBSCRIPTION_PAUSE_SETTING)
    pause_until_dt = _parse_sqlite_timestamp(pause_until)
    if pause_until_dt and pause_until_dt > datetime.now(UTC).replace(tzinfo=None):
        return pause_until
    if pause_until:
        settings_json.pop(SUBSCRIPTION_PAUSE_SETTING, None)
        await _save_settings_json(db, user_id, settings_json)
    return None


async def get_subscription_pause_until(user_id: int) -> Optional[str]:
    async with connect_db() as db:
        pause_until = await _get_active_subscription_pause_until(db, user_id)
        await db.commit()
        return pause_until


async def add_subscription(user_id: int, query: str, interval_minutes: int = 10) -> bool:
    async with connect_db() as db:
        try:
            pause_until = await _get_active_subscription_pause_until(db, user_id)
            await db.execute("""
                INSERT OR REPLACE INTO subscriptions
                (
                    user_id, query, interval_minutes, is_active, last_sent,
                    no_new_posts_count, last_empty_at, next_check_at, exhausted_notified,
                    processing_until, processing_token
                )
                VALUES (
                    ?, ?, ?, 1, datetime('now', '-1 hour'), 0, NULL,
                    COALESCE(?, datetime('now')), 0, NULL, NULL
                )
            """, (user_id, query.strip(), interval_minutes, pause_until))
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


async def pause_all_active_subscriptions(user_id: int, pause_minutes: int) -> int:
    async with connect_db() as db:
        pause_until = (
            datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=pause_minutes)
        ).strftime(SQLITE_TIMESTAMP_FORMAT)
        settings_json = await _get_settings_json(db, user_id)
        settings_json[SUBSCRIPTION_PAUSE_SETTING] = pause_until
        await _save_settings_json(db, user_id, settings_json)
        cursor = await db.execute("""
            UPDATE subscriptions
            SET next_check_at = CASE
                    WHEN datetime(COALESCE(next_check_at, last_sent)) >
                         datetime(?)
                    THEN next_check_at
                    ELSE ?
                END,
                processing_until = NULL,
                processing_token = NULL
            WHERE user_id = ?
              AND is_active = 1
        """, (pause_until, pause_until, user_id))
        await db.commit()
        return cursor.rowcount


async def resume_all_active_subscriptions(user_id: int) -> int:
    async with connect_db() as db:
        settings_json = await _get_settings_json(db, user_id)
        settings_json.pop(SUBSCRIPTION_PAUSE_SETTING, None)
        await _save_settings_json(db, user_id, settings_json)
        cursor = await db.execute("""
            UPDATE subscriptions
            SET next_check_at = datetime('now'),
                processing_until = NULL,
                processing_token = NULL
            WHERE user_id = ?
              AND is_active = 1
        """, (user_id,))
        await db.commit()
        return cursor.rowcount


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
    normalized = _normalize_post(post)
    if normalized is None:
        return False
    post_id, file_url, sample_url, preview_url, tags, rating, score = normalized

    async with connect_db() as db:
        try:
            await db.execute("""
                INSERT INTO post_cache
                (post_id, file_url, sample_url, preview_url, tags, rating, score, cached_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(post_id) DO UPDATE SET
                    file_url = COALESCE(NULLIF(excluded.file_url, ''), post_cache.file_url),
                    sample_url = COALESCE(NULLIF(excluded.sample_url, ''), post_cache.sample_url),
                    preview_url = COALESCE(NULLIF(excluded.preview_url, ''), post_cache.preview_url),
                    tags = COALESCE(NULLIF(excluded.tags, ''), post_cache.tags),
                    rating = COALESCE(NULLIF(excluded.rating, ''), post_cache.rating),
                    score = CASE WHEN excluded.score != 0 THEN excluded.score ELSE post_cache.score END,
                    cached_at = CURRENT_TIMESTAMP
            """, normalized)
            await db.execute("""
                INSERT INTO favorites
                (user_id, post_id, file_url, sample_url, preview_url, tags, rating, score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                post_id,
                file_url,
                sample_url,
                preview_url,
                tags,
                rating,
                score,
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
    user_id: int,
    limit: Optional[int] = 10,
    offset: int = 0,
    tag_filter: str = "",
) -> List[Dict[str, Any]]:
    tag_filter = tag_filter.strip().lower()
    tag_where = ""
    params: list[Any] = [user_id]
    if tag_filter:
        tag_where = """
            AND lower(' ' || COALESCE(NULLIF(pc.tags, ''), f.tags) || ' ')
                LIKE ?
        """
        params.append(f"% {tag_filter} %")

    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT ? OFFSET ?"
        params.extend([limit, offset])

    async with connect_db() as db:
        cursor = await db.execute(f"""
            SELECT
                f.post_id,
                COALESCE(NULLIF(pc.file_url, ''), f.file_url),
                COALESCE(NULLIF(pc.sample_url, ''), f.sample_url),
                COALESCE(NULLIF(pc.preview_url, ''), f.preview_url),
                COALESCE(NULLIF(pc.tags, ''), f.tags),
                COALESCE(NULLIF(pc.rating, ''), f.rating),
                COALESCE(pc.score, f.score),
                f.added_at
            FROM favorites
            f LEFT JOIN post_cache pc ON pc.post_id = f.post_id
            WHERE user_id = ?
            {tag_where}
            ORDER BY added_at DESC
            {limit_clause}
        """, tuple(params))
        rows = await cursor.fetchall()
        posts = []
        for row in rows:
            post = _post_from_row(row)
            post["added_at"] = row[7]
            posts.append(post)
        return posts


async def get_favorite(user_id: int, post_id: int) -> Optional[Dict[str, Any]]:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT
                f.post_id,
                COALESCE(NULLIF(pc.file_url, ''), f.file_url),
                COALESCE(NULLIF(pc.sample_url, ''), f.sample_url),
                COALESCE(NULLIF(pc.preview_url, ''), f.preview_url),
                COALESCE(NULLIF(pc.tags, ''), f.tags),
                COALESCE(NULLIF(pc.rating, ''), f.rating),
                COALESCE(pc.score, f.score),
                f.added_at
            FROM favorites f
            LEFT JOIN post_cache pc ON pc.post_id = f.post_id
            WHERE f.user_id = ? AND f.post_id = ?
        """, (user_id, post_id))
        row = await cursor.fetchone()
        if not row:
            return None
        post = _post_from_row(row)
        post["added_at"] = row[7]
        return post


async def count_favorites(user_id: int, tag_filter: str = "") -> int:
    tag_filter = tag_filter.strip().lower()
    tag_where = ""
    params: list[Any] = [user_id]
    if tag_filter:
        tag_where = """
            AND lower(' ' || COALESCE(NULLIF(pc.tags, ''), f.tags) || ' ')
                LIKE ?
        """
        params.append(f"% {tag_filter} %")

    async with connect_db() as db:
        cursor = await db.execute(f"""
            SELECT COUNT(*)
            FROM favorites f
            LEFT JOIN post_cache pc ON pc.post_id = f.post_id
            WHERE user_id = ?
            {tag_where}
        """, tuple(params))
        row = await cursor.fetchone()
        return int(row[0] or 0)


async def add_subscription_post(user_id: int, query: str, post: Dict[str, Any]) -> bool:
    normalized = _normalize_post(post)
    if normalized is None:
        return False
    post_id, file_url, sample_url, preview_url, tags, rating, score = normalized

    async with connect_db() as db:
        try:
            await db.execute("""
                INSERT INTO post_cache
                (post_id, file_url, sample_url, preview_url, tags, rating, score, cached_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(post_id) DO UPDATE SET
                    file_url = COALESCE(NULLIF(excluded.file_url, ''), post_cache.file_url),
                    sample_url = COALESCE(NULLIF(excluded.sample_url, ''), post_cache.sample_url),
                    preview_url = COALESCE(NULLIF(excluded.preview_url, ''), post_cache.preview_url),
                    tags = COALESCE(NULLIF(excluded.tags, ''), post_cache.tags),
                    rating = COALESCE(NULLIF(excluded.rating, ''), post_cache.rating),
                    score = CASE WHEN excluded.score != 0 THEN excluded.score ELSE post_cache.score END,
                    cached_at = CURRENT_TIMESTAMP
            """, normalized)
            await db.execute("""
                INSERT INTO subscription_posts
                (user_id, query, post_id, file_url, sample_url, preview_url, tags, rating, score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                query.strip(),
                post_id,
                file_url,
                sample_url,
                preview_url,
                tags,
                rating,
                score,
            ))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def get_subscription_posts(
    user_id: int, query: str, limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    params: list[Any] = [user_id, query.strip()]
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT ?"
        params.append(limit)

    async with connect_db() as db:
        cursor = await db.execute(f"""
            SELECT
                sp.post_id,
                COALESCE(NULLIF(pc.file_url, ''), sp.file_url),
                COALESCE(NULLIF(pc.sample_url, ''), sp.sample_url),
                COALESCE(NULLIF(pc.preview_url, ''), sp.preview_url),
                COALESCE(NULLIF(pc.tags, ''), sp.tags),
                COALESCE(NULLIF(pc.rating, ''), sp.rating),
                COALESCE(pc.score, sp.score),
                sp.sent_at
            FROM subscription_posts sp
            INNER JOIN favorites f
                ON f.user_id = sp.user_id
                AND f.post_id = sp.post_id
            LEFT JOIN post_cache pc ON pc.post_id = sp.post_id
            WHERE sp.user_id = ? AND sp.query = ?
            ORDER BY sp.sent_at DESC
            {limit_clause}
        """, tuple(params))
        rows = await cursor.fetchall()
        posts = []
        for row in rows:
            post = _post_from_row(row)
            post["sent_at"] = row[7]
            posts.append(post)
        return posts


async def remove_subscription_post(user_id: int, query: str, post_id: int) -> bool:
    async with connect_db() as db:
        cursor = await db.execute("""
            DELETE FROM subscription_posts
            WHERE user_id = ? AND query = ? AND post_id = ?
        """, (user_id, query.strip(), post_id))
        await db.commit()
        return cursor.rowcount > 0
