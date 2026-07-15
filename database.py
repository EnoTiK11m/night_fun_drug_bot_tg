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
MAIN_SETTING_FIELDS = (
    "show_caption",
    "show_search_query",
    "show_subscription_label",
    "show_id",
    "show_score",
    "show_rating",
    "show_tags",
)
DEFAULT_USER_SETTINGS = {
    "show_caption": True,
    "show_search_query": True,
    "show_subscription_label": True,
    "show_id": True,
    "show_score": True,
    "show_rating": True,
    "show_tags": True,
    "show_tags_button": True,
    "gallery_sort": "random",
    "gallery_size": 10,
    "rating_filter": "all",
    "media_type": "all",
    "orientation": "any",
    "min_width": 0,
    "min_height": 0,
    "quality_mode": "auto",
    "max_file_mb": 10,
}

BLACKLIST_PRESETS = {
    "animated": {"animated", "gif", "webm"},
    "male": {"male", "1boy", "multiple_boys"},
    "extreme": {"gore", "scat", "guro"},
}


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


async def ensure_blacklist_columns(db):
    cursor = await db.execute("PRAGMA table_info(blacklist)")
    columns = {row[1] for row in await cursor.fetchall()}
    for column, definition in {
        "expires_at": "TIMESTAMP",
        "source": "TEXT DEFAULT 'manual'",
    }.items():
        if column not in columns:
            await db.execute(f"ALTER TABLE blacklist ADD COLUMN {column} {definition}")


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
            CREATE TABLE IF NOT EXISTS callback_payloads (
                action TEXT NOT NULL,
                token TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (action, token)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS favorite_collections (
                collection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS favorite_collection_items (
                collection_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(collection_id, post_id),
                FOREIGN KEY(collection_id) REFERENCES favorite_collections(collection_id)
                    ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS favorite_notes (
                user_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                note TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, post_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS delivery_failures (
                failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                post_json TEXT NOT NULL,
                caption TEXT DEFAULT '',
                attempts INTEGER DEFAULT 1,
                last_error TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, post_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event_type TEXT NOT NULL,
                post_id INTEGER,
                query TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        await ensure_blacklist_columns(db)

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
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_collection_items_user
            ON favorite_collection_items (user_id, collection_id, added_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_bot_events_user_created
            ON bot_events (user_id, created_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_delivery_failures_created
            ON delivery_failures (created_at)
        """)
        await db.execute("""
            DELETE FROM bot_events
            WHERE event_id NOT IN (
                SELECT event_id FROM bot_events ORDER BY event_id DESC LIMIT 100000
            )
        """)
        await ensure_media_post_columns(db, "favorites")
        await ensure_media_post_columns(db, "subscription_posts")
        await ensure_subscription_cache_columns(db)

        await db.commit()


async def get_user_blacklist(user_id: int) -> Set[str]:
    async with connect_db() as db:
        await db.execute(
            "DELETE FROM blacklist WHERE user_id = ? AND expires_at IS NOT NULL "
            "AND expires_at <= CURRENT_TIMESTAMP",
            (user_id,),
        )
        cursor = await db.execute(
            "SELECT tag FROM blacklist WHERE user_id = ?",
            (user_id,)
        )
        rows = await cursor.fetchall()
        await db.commit()
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


async def add_temporary_blacklist_tag(user_id: int, tag: str, minutes: int) -> bool:
    tag = tag.lower().strip()
    minutes = max(1, min(int(minutes), 30 * 24 * 60))
    async with connect_db() as db:
        cursor = await db.execute(
            "SELECT COALESCE(source, 'manual') FROM blacklist WHERE user_id = ? AND tag = ?",
            (user_id, tag),
        )
        row = await cursor.fetchone()
        if row and row[0] != "temporary":
            return False
        await db.execute("""
            INSERT INTO blacklist (user_id, tag, expires_at, source)
            VALUES (?, ?, datetime('now', '+' || ? || ' minutes'), 'temporary')
            ON CONFLICT(user_id, tag) DO UPDATE SET
                expires_at = excluded.expires_at,
                source = 'temporary'
        """, (user_id, tag, minutes))
        await db.commit()
        return True


async def get_blacklist_entries(user_id: int) -> List[Dict[str, Any]]:
    await get_user_blacklist(user_id)
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT tag, expires_at, COALESCE(source, 'manual')
            FROM blacklist WHERE user_id = ? ORDER BY tag
        """, (user_id,))
        return [
            {"tag": row[0], "expires_at": row[1], "source": row[2]}
            for row in await cursor.fetchall()
        ]


async def apply_blacklist_preset(user_id: int, preset: str) -> int:
    tags = BLACKLIST_PRESETS.get(preset, set())
    added_tags = []
    for tag in tags:
        if await add_to_blacklist(user_id, tag):
            added_tags.append(tag)
    if added_tags:
        async with connect_db() as db:
            placeholders = ",".join("?" for _ in added_tags)
            await db.execute(
                f"UPDATE blacklist SET source = ? WHERE user_id = ? AND tag IN ({placeholders})",
                (f"preset:{preset}", user_id, *added_tags),
            )
            await db.commit()
    return len(added_tags)


async def remove_blacklist_preset(user_id: int, preset: str) -> int:
    async with connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM blacklist WHERE user_id = ? AND source = ?",
            (user_id, f"preset:{preset}"),
        )
        await db.commit()
        return cursor.rowcount


async def replace_user_blacklist(user_id: int, tags: Set[str]) -> int:
    normalized = sorted({tag.lower().strip() for tag in tags if tag.strip()})[:500]
    async with connect_db() as db:
        await db.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
        await db.executemany(
            "INSERT INTO blacklist (user_id, tag, source) VALUES (?, ?, 'import')",
            [(user_id, tag) for tag in normalized],
        )
        await db.commit()
    return len(normalized)


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
            INSERT INTO bot_events (user_id, event_type, query)
            VALUES (?, 'search', ?)
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
        cursor = await db.execute("""
            INSERT OR IGNORE INTO sent_posts (user_id, post_id)
            VALUES (?, ?)
        """, (user_id, post_id))
        if cursor.rowcount:
            await db.execute("""
                INSERT INTO bot_events (user_id, event_type, post_id)
                VALUES (?, 'viewed', ?)
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
            settings = DEFAULT_USER_SETTINGS.copy()
            settings.update({
                "show_caption": bool(row[1]),
                "show_search_query": bool(row[2]),
                "show_subscription_label": bool(row[3]),
                "show_id": bool(row[4]),
                "show_score": bool(row[5]),
                "show_rating": bool(row[6]),
                "show_tags": bool(row[7]),
            })

            if row[8]:
                try:
                    json_settings = json.loads(row[8])
                    settings.update(json_settings)
                except json.JSONDecodeError:
                    logger.warning("Invalid settings JSON for user %s", user_id)

            return settings

        default_settings = DEFAULT_USER_SETTINGS.copy()
        await save_user_settings(user_id, default_settings)
        return default_settings


async def save_user_settings(user_id: int, settings: Dict[str, Any]):
    async with connect_db() as db:
        merged_settings = DEFAULT_USER_SETTINGS.copy()
        cursor = await db.execute(
            "SELECT * FROM user_settings WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row:
            merged_settings.update({
                "show_caption": bool(row[1]),
                "show_search_query": bool(row[2]),
                "show_subscription_label": bool(row[3]),
                "show_id": bool(row[4]),
                "show_score": bool(row[5]),
                "show_rating": bool(row[6]),
                "show_tags": bool(row[7]),
            })
            if row[8]:
                try:
                    merged_settings.update(json.loads(row[8]))
                except json.JSONDecodeError:
                    logger.warning("Invalid settings JSON for user %s", user_id)

        merged_settings.update(settings)
        json_settings = {
            key: value
            for key, value in merged_settings.items()
            if key not in MAIN_SETTING_FIELDS
        }
        await db.execute("""
            INSERT OR REPLACE INTO user_settings
            (user_id, show_caption, show_search_query, show_subscription_label,
             show_id, show_score, show_rating, show_tags, settings_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            bool(merged_settings["show_caption"]),
            bool(merged_settings["show_search_query"]),
            bool(merged_settings["show_subscription_label"]),
            bool(merged_settings["show_id"]),
            bool(merged_settings["show_score"]),
            bool(merged_settings["show_rating"]),
            bool(merged_settings["show_tags"]),
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
              AND datetime(processing_until) <= datetime('now')
        """)
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
            await db.execute("""
                INSERT INTO bot_events (user_id, event_type, post_id)
                VALUES (?, 'favorite_added', ?)
            """, (user_id, post_id))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_favorite(user_id: int, post_id: int) -> bool:
    async with connect_db() as db:
        await db.execute(
            "DELETE FROM favorite_collection_items WHERE user_id = ? AND post_id = ?",
            (user_id, post_id),
        )
        await db.execute(
            "DELETE FROM favorite_notes WHERE user_id = ? AND post_id = ?",
            (user_id, post_id),
        )
        cursor = await db.execute(
            "DELETE FROM favorites WHERE user_id = ? AND post_id = ?",
            (user_id, post_id)
        )
        await db.commit()
        return cursor.rowcount > 0


def _normalize_collection_name(name: str) -> str:
    return " ".join(name.strip().split())[:40]


async def create_favorite_collection(user_id: int, name: str) -> Optional[int]:
    name = _normalize_collection_name(name)
    if not name:
        return None
    async with connect_db() as db:
        try:
            cursor = await db.execute(
                "INSERT INTO favorite_collections (user_id, name) VALUES (?, ?)",
                (user_id, name),
            )
            await db.commit()
            return int(cursor.lastrowid)
        except aiosqlite.IntegrityError:
            return None


async def get_favorite_collections(user_id: int) -> List[Dict[str, Any]]:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT c.collection_id, c.name, COUNT(i.post_id), c.created_at
            FROM favorite_collections c
            LEFT JOIN favorite_collection_items i
              ON i.collection_id = c.collection_id AND i.user_id = c.user_id
            WHERE c.user_id = ?
            GROUP BY c.collection_id, c.name, c.created_at
            ORDER BY lower(c.name), c.collection_id
        """, (user_id,))
        return [
            {"id": row[0], "name": row[1], "count": row[2], "created_at": row[3]}
            for row in await cursor.fetchall()
        ]


async def get_favorite_collection(user_id: int, collection_id: int) -> Optional[Dict[str, Any]]:
    collections = await get_favorite_collections(user_id)
    return next((item for item in collections if item["id"] == collection_id), None)


async def rename_favorite_collection(user_id: int, collection_id: int, name: str) -> bool:
    name = _normalize_collection_name(name)
    if not name:
        return False
    async with connect_db() as db:
        try:
            cursor = await db.execute(
                "UPDATE favorite_collections SET name = ? WHERE user_id = ? AND collection_id = ?",
                (name, user_id, collection_id),
            )
            await db.commit()
            return cursor.rowcount > 0
        except aiosqlite.IntegrityError:
            return False


async def delete_favorite_collection(user_id: int, collection_id: int) -> bool:
    async with connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM favorite_collections WHERE user_id = ? AND collection_id = ?",
            (user_id, collection_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def add_favorite_to_collection(user_id: int, collection_id: int, post_id: int) -> bool:
    async with connect_db() as db:
        cursor = await db.execute(
            "SELECT 1 FROM favorite_collections WHERE user_id = ? AND collection_id = ?",
            (user_id, collection_id),
        )
        if not await cursor.fetchone():
            return False
        cursor = await db.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND post_id = ?",
            (user_id, post_id),
        )
        if not await cursor.fetchone():
            return False
        try:
            await db.execute("""
                INSERT INTO favorite_collection_items (collection_id, user_id, post_id)
                VALUES (?, ?, ?)
            """, (collection_id, user_id, post_id))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_favorite_from_collection(user_id: int, collection_id: int, post_id: int) -> bool:
    async with connect_db() as db:
        cursor = await db.execute("""
            DELETE FROM favorite_collection_items
            WHERE user_id = ? AND collection_id = ? AND post_id = ?
        """, (user_id, collection_id, post_id))
        await db.commit()
        return cursor.rowcount > 0


async def get_collection_favorites(
    user_id: int,
    collection_id: int,
    limit: Optional[int] = 10,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    params: list[Any] = [user_id, collection_id]
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    async with connect_db() as db:
        cursor = await db.execute(f"""
            SELECT f.post_id,
                   COALESCE(NULLIF(pc.file_url, ''), f.file_url),
                   COALESCE(NULLIF(pc.sample_url, ''), f.sample_url),
                   COALESCE(NULLIF(pc.preview_url, ''), f.preview_url),
                   COALESCE(NULLIF(pc.tags, ''), f.tags),
                   COALESCE(NULLIF(pc.rating, ''), f.rating),
                   COALESCE(pc.score, f.score), i.added_at
            FROM favorite_collection_items i
            JOIN favorites f ON f.user_id = i.user_id AND f.post_id = i.post_id
            LEFT JOIN post_cache pc ON pc.post_id = f.post_id
            WHERE i.user_id = ? AND i.collection_id = ?
            ORDER BY i.added_at DESC, i.post_id DESC
            {limit_clause}
        """, tuple(params))
        posts = []
        for row in await cursor.fetchall():
            post = _post_from_row(row)
            post["added_at"] = row[7]
            posts.append(post)
        return posts


async def count_collection_favorites(user_id: int, collection_id: int) -> int:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT COUNT(*) FROM favorite_collection_items
            WHERE user_id = ? AND collection_id = ?
        """, (user_id, collection_id))
        row = await cursor.fetchone()
        return int(row[0] or 0)


async def set_favorite_note(user_id: int, post_id: int, note: str) -> bool:
    note = note.strip()[:1000]
    async with connect_db() as db:
        cursor = await db.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND post_id = ?",
            (user_id, post_id),
        )
        if not await cursor.fetchone():
            return False
        if note:
            await db.execute("""
                INSERT INTO favorite_notes (user_id, post_id, note, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, post_id) DO UPDATE SET
                    note = excluded.note, updated_at = CURRENT_TIMESTAMP
            """, (user_id, post_id, note))
        else:
            await db.execute(
                "DELETE FROM favorite_notes WHERE user_id = ? AND post_id = ?",
                (user_id, post_id),
            )
        await db.commit()
        return True


async def get_favorite_note(user_id: int, post_id: int) -> str:
    async with connect_db() as db:
        cursor = await db.execute(
            "SELECT note FROM favorite_notes WHERE user_id = ? AND post_id = ?",
            (user_id, post_id),
        )
        row = await cursor.fetchone()
        return row[0] if row else ""


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
            ORDER BY added_at DESC, f.post_id DESC
            {limit_clause}
        """, tuple(params))
        rows = await cursor.fetchall()
        posts = []
        for row in rows:
            post = _post_from_row(row)
            post["added_at"] = row[7]
            posts.append(post)
        return posts


async def get_favorite_by_index(
    user_id: int,
    index: int,
    tag_filter: str = "",
) -> Optional[Dict[str, Any]]:
    posts = await get_favorites(
        user_id,
        limit=1,
        offset=max(0, index),
        tag_filter=tag_filter,
    )
    return posts[0] if posts else None


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
    user_id: int,
    query: str,
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    params: list[Any] = [user_id, query.strip()]
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT ? OFFSET ?"
        params.extend([limit, offset])

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
            ORDER BY sp.sent_at DESC, sp.post_id DESC
            {limit_clause}
        """, tuple(params))
        rows = await cursor.fetchall()
        posts = []
        for row in rows:
            post = _post_from_row(row)
            post["sent_at"] = row[7]
            posts.append(post)
        return posts


async def count_subscription_posts(user_id: int, query: str) -> int:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT COUNT(*)
            FROM subscription_posts sp
            INNER JOIN favorites f
                ON f.user_id = sp.user_id
                AND f.post_id = sp.post_id
            WHERE sp.user_id = ? AND sp.query = ?
        """, (user_id, query.strip()))
        row = await cursor.fetchone()
        return int(row[0] or 0)


async def get_subscription_queries_for_post(user_id: int, post_id: int) -> List[str]:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT DISTINCT sc.query
            FROM subscription_cache sc
            INNER JOIN subscriptions s
                ON s.user_id = sc.user_id
                AND s.query = sc.query
            WHERE sc.user_id = ?
              AND sc.post_id = ?
            ORDER BY sc.query
        """, (user_id, post_id))
        rows = await cursor.fetchall()
        return [row[0] for row in rows]


async def get_subscription_post_by_index(
    user_id: int,
    query: str,
    index: int,
) -> Optional[Dict[str, Any]]:
    posts = await get_subscription_posts(
        user_id,
        query,
        limit=1,
        offset=max(0, index),
    )
    return posts[0] if posts else None


async def remove_subscription_post(user_id: int, query: str, post_id: int) -> bool:
    async with connect_db() as db:
        cursor = await db.execute("""
            DELETE FROM subscription_posts
            WHERE user_id = ? AND query = ? AND post_id = ?
        """, (user_id, query.strip(), post_id))
        await db.commit()
        return cursor.rowcount > 0


async def get_user_activity_stats(user_id: int) -> Dict[str, Any]:
    async with connect_db() as db:
        async def scalar(sql: str, params=()) -> int:
            cursor = await db.execute(sql, params)
            row = await cursor.fetchone()
            return int(row[0] or 0)

        stats = {
            "viewed_total": await scalar(
                "SELECT COUNT(*) FROM sent_posts WHERE user_id = ?", (user_id,)
            ),
            "favorites_total": await scalar(
                "SELECT COUNT(*) FROM favorites WHERE user_id = ?", (user_id,)
            ),
            "searches_total": await scalar(
                "SELECT COUNT(*) FROM search_history WHERE user_id = ?", (user_id,)
            ),
            "subscriptions_active": await scalar(
                "SELECT COUNT(*) FROM subscriptions WHERE user_id = ? AND is_active = 1",
                (user_id,),
            ),
            "viewed_week": await scalar(
                "SELECT COUNT(*) FROM sent_posts WHERE user_id = ? AND sent_at >= datetime('now', '-7 days')",
                (user_id,),
            ),
            "favorites_week": await scalar(
                "SELECT COUNT(*) FROM favorites WHERE user_id = ? AND added_at >= datetime('now', '-7 days')",
                (user_id,),
            ),
            "favorites_month": await scalar(
                "SELECT COUNT(*) FROM favorites WHERE user_id = ? AND added_at >= datetime('now', '-30 days')",
                (user_id,),
            ),
            "searches_week": await scalar(
                "SELECT COUNT(*) FROM search_history WHERE user_id = ? AND searched_at >= datetime('now', '-7 days')",
                (user_id,),
            ),
            "searches_month": await scalar(
                "SELECT COUNT(*) FROM search_history WHERE user_id = ? AND searched_at >= datetime('now', '-30 days')",
                (user_id,),
            ),
            "viewed_month": await scalar(
                "SELECT COUNT(*) FROM sent_posts WHERE user_id = ? AND sent_at >= datetime('now', '-30 days')",
                (user_id,),
            ),
        }
        event_windows = {
            "viewed_total": ("viewed", ""),
            "viewed_week": ("viewed", "AND created_at >= datetime('now', '-7 days')"),
            "viewed_month": ("viewed", "AND created_at >= datetime('now', '-30 days')"),
            "searches_total": ("search", ""),
            "searches_week": ("search", "AND created_at >= datetime('now', '-7 days')"),
            "searches_month": ("search", "AND created_at >= datetime('now', '-30 days')"),
        }
        for key, (event_type, time_where) in event_windows.items():
            event_count = await scalar(
                f"SELECT COUNT(*) FROM bot_events WHERE user_id = ? AND event_type = ? {time_where}",
                (user_id, event_type),
            )
            stats[key] = max(stats[key], event_count)
        cursor = await db.execute("""
            SELECT query, COUNT(*) AS uses
            FROM search_history
            WHERE user_id = ? AND query <> ''
            GROUP BY query ORDER BY uses DESC, MAX(searched_at) DESC LIMIT 5
        """, (user_id,))
        stats["top_queries"] = await cursor.fetchall()
        cursor = await db.execute("""
            SELECT COALESCE(NULLIF(pc.tags, ''), f.tags)
            FROM favorites f LEFT JOIN post_cache pc ON pc.post_id = f.post_id
            WHERE f.user_id = ?
        """, (user_id,))
        tag_counts: Dict[str, int] = {}
        for row in await cursor.fetchall():
            for tag in (row[0] or "").split():
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        stats["top_tags"] = sorted(
            tag_counts.items(), key=lambda item: (-item[1], item[0])
        )[:8]
        return stats


async def clear_user_activity_stats(user_id: int):
    async with connect_db() as db:
        await db.execute("DELETE FROM sent_posts WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM search_history WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM bot_events WHERE user_id = ?", (user_id,))
        await db.commit()


async def save_delivery_failure(
    user_id: int,
    post: Dict[str, Any],
    caption: str = "",
    error: str = "delivery failed",
):
    normalized = _normalize_post(post)
    if normalized is None:
        return
    post_id = normalized[0]
    safe_post = {
        "id": post_id,
        "file_url": normalized[1],
        "sample_url": normalized[2],
        "preview_url": normalized[3],
        "tags": normalized[4],
        "rating": normalized[5],
        "score": normalized[6],
    }
    async with connect_db() as db:
        await db.execute("""
            INSERT INTO delivery_failures
                (user_id, post_id, post_json, caption, last_error)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, post_id) DO UPDATE SET
                post_json = excluded.post_json,
                caption = excluded.caption,
                attempts = delivery_failures.attempts + 1,
                last_error = excluded.last_error,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, post_id, json.dumps(safe_post), caption[:1024], error[:500]))
        await db.commit()


async def get_delivery_failures(limit: int = 20) -> List[Dict[str, Any]]:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT failure_id, user_id, post_id, post_json, caption, attempts,
                   last_error, created_at, updated_at
            FROM delivery_failures ORDER BY updated_at ASC LIMIT ?
        """, (max(1, min(limit, 100)),))
        result = []
        for row in await cursor.fetchall():
            try:
                post = json.loads(row[3])
            except json.JSONDecodeError:
                post = {"id": row[2]}
            result.append({
                "id": row[0], "user_id": row[1], "post_id": row[2],
                "post": post, "caption": row[4], "attempts": row[5],
                "last_error": row[6], "created_at": row[7], "updated_at": row[8],
            })
        return result


async def delete_delivery_failure(failure_id: int):
    async with connect_db() as db:
        await db.execute("DELETE FROM delivery_failures WHERE failure_id = ?", (failure_id,))
        await db.commit()


async def get_admin_database_stats() -> Dict[str, Any]:
    async with connect_db() as db:
        cursor = await db.execute("PRAGMA quick_check")
        quick_check = (await cursor.fetchone())[0]
        counts = {}
        for table in (
            "users", "favorites", "subscriptions", "sent_posts",
            "delivery_failures", "bot_events",
        ):
            cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = int((await cursor.fetchone())[0] or 0)
        return {"quick_check": quick_check, "counts": counts}
