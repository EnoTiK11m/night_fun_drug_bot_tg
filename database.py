import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import aiosqlite

from config import (
    DB_PATH,
    POST_CACHE_MAX_ROWS,
    POST_CACHE_TTL_HOURS,
    SUBSCRIPTION_CACHE_CLEANUP_BATCH_SIZE,
    SUBSCRIPTION_CACHE_MAX_PER_QUERY,
    SUBSCRIPTION_CACHE_MAX_ROWS,
    SUBSCRIPTION_CREATE_COOLDOWN_SECONDS,
    SUBSCRIPTION_MAX_ACTIVE,
    SUBSCRIPTION_MAX_TOTAL,
    SUBSCRIPTION_QUERY_MAX_LENGTH,
    SUBSCRIPTION_QUERY_MAX_TAGS,
)

logger = logging.getLogger(__name__)

SUBSCRIPTION_EMPTY_BACKOFF_MINUTES = (60, 120, 240, 480, 720)
SENT_POSTS_RETENTION_PER_USER = 5000
SEARCH_HISTORY_RETENTION_PER_USER = 200
SUBSCRIPTION_CLAIM_MINUTES = 5
DIGEST_CLAIM_MINUTES = 10
DELIVERY_FAILURE_CLAIM_MINUTES = 5
DIGEST_CLAIM_MIGRATION_VERSION = 1
SUBSCRIPTION_QUOTA_MIGRATION_VERSION = 2
CACHE_RETENTION_MIGRATION_VERSION = 3
DIGEST_RETRY_MIGRATION_VERSION = 4
DELIVERY_FAILURE_CLAIM_MIGRATION_VERSION = 5
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
    "spoiler_mode": "off",
    "read_later_days": 30,
    "interface_mode": "simple",
}

BLACKLIST_PRESETS = {
    "animated": {"animated", "gif", "webm"},
    "male": {"male", "1boy", "multiple_boys"},
    "extreme": {"gore", "scat", "guro"},
}


@dataclass(frozen=True)
class SubscriptionAddResult:
    status: str
    total_count: int = 0
    active_count: int = 0
    total_limit: int = SUBSCRIPTION_MAX_TOTAL
    active_limit: int = SUBSCRIPTION_MAX_ACTIVE
    retry_after_seconds: int = 0

    def __bool__(self) -> bool:
        return self.status in {"created", "reactivated", "updated"}


@dataclass(frozen=True)
class SubscriptionToggleResult:
    status: str
    is_active: Optional[bool] = None
    active_count: int = 0
    active_limit: int = SUBSCRIPTION_MAX_ACTIVE
    total_count: int = 0
    total_limit: int = SUBSCRIPTION_MAX_TOTAL


@dataclass(frozen=True)
class CacheCleanupResult:
    subscription_cache_deleted: int
    post_cache_deleted: int
    subscription_cache_remaining: int
    post_cache_remaining: int
    elapsed_ms: float


def normalize_subscription_query(query: Any) -> str:
    if not isinstance(query, str):
        return ""
    return " ".join(query.strip().split())


def validate_subscription_query(query: Any) -> tuple[str, str]:
    if not isinstance(query, str) or any(ord(character) < 32 for character in query):
        return normalize_subscription_query(query), "invalid_query"
    normalized = normalize_subscription_query(query)
    if not normalized:
        return normalized, "invalid_query"
    if len(normalized) > SUBSCRIPTION_QUERY_MAX_LENGTH:
        return normalized, "query_too_long"
    tokens = normalized.split()
    if len(tokens) > SUBSCRIPTION_QUERY_MAX_TAGS:
        return normalized, "too_many_tags"
    if any(token in {"-", "+"} for token in tokens):
        return normalized, "invalid_query"
    return normalized, ""


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
        "settings_json": "TEXT DEFAULT '{}'",
        "digest_mode": "TEXT DEFAULT 'instant'",
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
        "width": "INTEGER DEFAULT 0",
        "height": "INTEGER DEFAULT 0",
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


async def apply_versioned_migrations(db):
    """Apply small, restart-safe schema migrations after the base schema commit."""
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (DIGEST_CLAIM_MIGRATION_VERSION,),
        )
        already_applied = await cursor.fetchone() is not None
        cursor = await db.execute("PRAGMA table_info(subscription_digest_queue)")
        columns = {row[1] for row in await cursor.fetchall()}
        required_columns = {
            "claim_token": "TEXT",
            "claimed_at": "TIMESTAMP",
            "claim_until": "TIMESTAMP",
        }

        if not already_applied:
            for column, definition in required_columns.items():
                if column not in columns:
                    await db.execute(
                        f"ALTER TABLE subscription_digest_queue "
                        f"ADD COLUMN {column} {definition}"
                    )
            await db.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (DIGEST_CLAIM_MIGRATION_VERSION,),
            )
        elif not required_columns.keys() <= columns:
            missing = sorted(required_columns.keys() - columns)
            raise RuntimeError(
                "Digest claim migration is recorded but columns are missing: "
                + ", ".join(missing)
            )

        cursor = await db.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (SUBSCRIPTION_QUOTA_MIGRATION_VERSION,),
        )
        quota_migration_applied = await cursor.fetchone() is not None
        cursor = await db.execute("""
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'subscription_creation_state'
        """)
        quota_table_exists = await cursor.fetchone() is not None
        if not quota_migration_applied:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS subscription_creation_state (
                    user_id INTEGER PRIMARY KEY,
                    last_created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (SUBSCRIPTION_QUOTA_MIGRATION_VERSION,),
            )
        elif not quota_table_exists:
            raise RuntimeError(
                "Subscription quota migration is recorded but state table is missing"
            )

        cache_indexes = {
            "idx_subscription_cache_lookup": (
                """
                    CREATE INDEX IF NOT EXISTS idx_subscription_cache_lookup
                    ON subscription_cache (user_id, query, cached_at)
                """,
                ("user_id", "query", "cached_at"),
            ),
            "idx_subscription_cache_cached": (
                """
                    CREATE INDEX IF NOT EXISTS idx_subscription_cache_cached
                    ON subscription_cache (cached_at)
                """,
                ("cached_at",),
            ),
            "idx_post_cache_cached": (
                """
                    CREATE INDEX IF NOT EXISTS idx_post_cache_cached
                    ON post_cache (cached_at)
                """,
                ("cached_at",),
            ),
        }
        cursor = await db.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (CACHE_RETENTION_MIGRATION_VERSION,),
        )
        cache_migration_applied = await cursor.fetchone() is not None
        if not cache_migration_applied:
            for statement, _expected_columns in cache_indexes.values():
                await db.execute(statement)
            await db.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (CACHE_RETENTION_MIGRATION_VERSION,),
            )
        else:
            invalid_indexes = []
            for index_name, (_statement, expected_columns) in cache_indexes.items():
                cursor = await db.execute(f"PRAGMA index_info({index_name})")
                actual_columns = tuple(row[2] for row in await cursor.fetchall())
                if actual_columns != expected_columns:
                    invalid_indexes.append(index_name)
            if invalid_indexes:
                raise RuntimeError(
                    "Cache retention migration is recorded but indexes are invalid: "
                    + ", ".join(sorted(invalid_indexes))
                )

        for version, table_name, required_columns in (
            (
                DIGEST_RETRY_MIGRATION_VERSION,
                "subscription_digest_queue",
                {
                    "delivery_state": "TEXT NOT NULL DEFAULT 'pending'",
                    "retry_after": "TIMESTAMP",
                },
            ),
            (
                DELIVERY_FAILURE_CLAIM_MIGRATION_VERSION,
                "delivery_failures",
                {
                    "claim_token": "TEXT",
                    "claimed_at": "TIMESTAMP",
                    "claim_until": "TIMESTAMP",
                },
            ),
        ):
            cursor = await db.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
            )
            migration_applied = await cursor.fetchone() is not None
            cursor = await db.execute(f"PRAGMA table_info({table_name})")
            columns = {row[1] for row in await cursor.fetchall()}
            if not migration_applied:
                for column, definition in required_columns.items():
                    if column not in columns:
                        await db.execute(
                            f"ALTER TABLE {table_name} ADD COLUMN {column} {definition}"
                        )
                await db.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                )
            elif not required_columns.keys() <= columns:
                missing = sorted(required_columns.keys() - columns)
                raise RuntimeError(
                    f"Migration {version} for {table_name} is recorded but columns "
                    "are missing: " + ", ".join(missing)
                )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


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
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
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
                claim_token TEXT,
                claimed_at TIMESTAMP,
                claim_until TIMESTAMP,
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
            CREATE TABLE IF NOT EXISTS search_presets (
                preset_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE,
                query TEXT NOT NULL,
                settings_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS read_later (
                user_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                post_json TEXT NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                PRIMARY KEY(user_id, post_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscription_digest_queue (
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                post_id INTEGER NOT NULL,
                post_json TEXT NOT NULL,
                queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                claim_token TEXT,
                claimed_at TIMESTAMP,
                claim_until TIMESTAMP,
                delivery_state TEXT NOT NULL DEFAULT 'pending',
                retry_after TIMESTAMP,
                PRIMARY KEY(user_id, query, post_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tag_translations (
                tag TEXT PRIMARY KEY,
                translation_ru TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                source TEXT NOT NULL DEFAULT 'queue',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            CREATE INDEX IF NOT EXISTS idx_read_later_expiry
            ON read_later (user_id, expires_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_digest_queue_user
            ON subscription_digest_queue (user_id, queued_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tag_translations_pending
            ON tag_translations (status, next_retry_at, updated_at)
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
        await apply_versioned_migrations(db)


def _deleted_row_count(cursor: aiosqlite.Cursor) -> int:
    return max(0, int(cursor.rowcount or 0))


async def _cleanup_expired_caches_in_connection(
    db: aiosqlite.Connection,
    *,
    subscription_ttl_minutes: int,
    subscription_max_per_query: int,
    subscription_max_rows: int,
    post_ttl_hours: int,
    post_max_rows: int,
    batch_size: int,
) -> CacheCleanupResult:
    started_at = time.perf_counter()
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM subscription_cache")
        subscription_initial = int((await cursor.fetchone())[0] or 0)
        cursor = await db.execute("SELECT COUNT(*) FROM post_cache")
        post_initial = int((await cursor.fetchone())[0] or 0)

        subscription_deleted = 0
        cursor = await db.execute("""
            DELETE FROM subscription_cache
            WHERE rowid IN (
                SELECT sc.rowid
                FROM subscription_cache sc
                WHERE datetime(COALESCE(sc.cached_at, '1970-01-01 00:00:00'))
                      < datetime('now', '-' || ? || ' minutes')
                  AND NOT EXISTS (
                      SELECT 1 FROM subscriptions s
                      WHERE s.user_id = sc.user_id
                        AND s.query = sc.query
                        AND s.processing_token IS NOT NULL
                        AND s.processing_until IS NOT NULL
                        AND datetime(s.processing_until) > datetime('now')
                  )
                ORDER BY datetime(COALESCE(sc.cached_at, '1970-01-01 00:00:00')), sc.rowid
                LIMIT ?
            )
        """, (max(1, int(subscription_ttl_minutes)), batch_size))
        subscription_deleted += _deleted_row_count(cursor)

        subscription_budget = max(0, batch_size - subscription_deleted)
        if subscription_budget:
            cursor = await db.execute("""
                DELETE FROM subscription_cache
                WHERE rowid IN (
                    SELECT candidate_rowid
                    FROM (
                        SELECT
                            sc.rowid AS candidate_rowid,
                            sc.cached_at AS candidate_cached_at,
                            ROW_NUMBER() OVER (
                                PARTITION BY sc.user_id, sc.query
                                ORDER BY datetime(COALESCE(sc.cached_at, '1970-01-01 00:00:00')) DESC,
                                         sc.rowid DESC
                            ) AS position
                        FROM subscription_cache sc
                        WHERE NOT EXISTS (
                            SELECT 1 FROM subscriptions s
                            WHERE s.user_id = sc.user_id
                              AND s.query = sc.query
                              AND s.processing_token IS NOT NULL
                              AND s.processing_until IS NOT NULL
                              AND datetime(s.processing_until) > datetime('now')
                        )
                    ) ranked
                    WHERE position > ?
                    ORDER BY datetime(COALESCE(candidate_cached_at, '1970-01-01 00:00:00')),
                             candidate_rowid
                    LIMIT ?
                )
            """, (subscription_max_per_query, subscription_budget))
            subscription_deleted += _deleted_row_count(cursor)

        subscription_budget = max(0, batch_size - subscription_deleted)
        subscription_overflow = max(
            0, subscription_initial - subscription_deleted - subscription_max_rows
        )
        if subscription_budget and subscription_overflow:
            cursor = await db.execute("""
                DELETE FROM subscription_cache
                WHERE rowid IN (
                    SELECT sc.rowid
                    FROM subscription_cache sc
                    WHERE NOT EXISTS (
                        SELECT 1 FROM subscriptions s
                        WHERE s.user_id = sc.user_id
                          AND s.query = sc.query
                          AND s.processing_token IS NOT NULL
                          AND s.processing_until IS NOT NULL
                          AND datetime(s.processing_until) > datetime('now')
                    )
                    ORDER BY datetime(COALESCE(sc.cached_at, '1970-01-01 00:00:00')), sc.rowid
                    LIMIT ?
                )
            """, (min(subscription_budget, subscription_overflow),))
            subscription_deleted += _deleted_row_count(cursor)

        post_deleted = 0
        cursor = await db.execute("""
            DELETE FROM post_cache
            WHERE rowid IN (
                SELECT rowid FROM post_cache
                WHERE datetime(COALESCE(cached_at, '1970-01-01 00:00:00'))
                      < datetime('now', '-' || ? || ' hours')
                ORDER BY datetime(COALESCE(cached_at, '1970-01-01 00:00:00')), rowid
                LIMIT ?
            )
        """, (max(1, int(post_ttl_hours)), batch_size))
        post_deleted += _deleted_row_count(cursor)

        post_budget = max(0, batch_size - post_deleted)
        post_overflow = max(0, post_initial - post_deleted - post_max_rows)
        if post_budget and post_overflow:
            cursor = await db.execute("""
                DELETE FROM post_cache
                WHERE rowid IN (
                    SELECT rowid FROM post_cache
                    ORDER BY datetime(COALESCE(cached_at, '1970-01-01 00:00:00')), rowid
                    LIMIT ?
                )
            """, (min(post_budget, post_overflow),))
            post_deleted += _deleted_row_count(cursor)

        await db.commit()
    except BaseException:
        await db.rollback()
        raise

    return CacheCleanupResult(
        subscription_cache_deleted=subscription_deleted,
        post_cache_deleted=post_deleted,
        subscription_cache_remaining=max(0, subscription_initial - subscription_deleted),
        post_cache_remaining=max(0, post_initial - post_deleted),
        elapsed_ms=(time.perf_counter() - started_at) * 1000,
    )


async def cleanup_expired_caches(
    *,
    subscription_ttl_minutes: int = SUBSCRIPTION_CACHE_TTL_MINUTES,
    subscription_max_per_query: int = SUBSCRIPTION_CACHE_MAX_PER_QUERY,
    subscription_max_rows: int = SUBSCRIPTION_CACHE_MAX_ROWS,
    post_ttl_hours: int = POST_CACHE_TTL_HOURS,
    post_max_rows: int = POST_CACHE_MAX_ROWS,
    batch_size: int = SUBSCRIPTION_CACHE_CLEANUP_BATCH_SIZE,
    db: aiosqlite.Connection | None = None,
) -> CacheCleanupResult:
    """Delete a bounded cache batch in one short transaction."""
    normalized_batch = max(1, int(batch_size))
    options = {
        "subscription_ttl_minutes": max(1, int(subscription_ttl_minutes)),
        "subscription_max_per_query": max(1, int(subscription_max_per_query)),
        "subscription_max_rows": max(1, int(subscription_max_rows)),
        "post_ttl_hours": max(1, int(post_ttl_hours)),
        "post_max_rows": max(1, int(post_max_rows)),
        "batch_size": normalized_batch,
    }
    if db is not None:
        return await _cleanup_expired_caches_in_connection(db, **options)
    async with connect_db() as owned_db:
        return await _cleanup_expired_caches_in_connection(owned_db, **options)


async def get_cache_storage_stats() -> Dict[str, int]:
    async with connect_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM subscription_cache")
        subscription_rows = int((await cursor.fetchone())[0] or 0)
        cursor = await db.execute("SELECT COUNT(*) FROM post_cache")
        post_rows = int((await cursor.fetchone())[0] or 0)
    return {
        "subscription_cache_rows": subscription_rows,
        "post_cache_rows": post_rows,
    }


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


def _normalize_translation_tags(tags) -> List[str]:
    if isinstance(tags, str):
        tags = tags.split()
    return sorted({
        str(tag).strip().lower()
        for tag in tags
        if tag is not None and str(tag).strip()
    })


async def queue_tag_translations(tags, source: str = "queue") -> int:
    normalized = _normalize_translation_tags(tags)
    if not normalized:
        return 0
    async with connect_db() as db:
        before = db.total_changes
        await db.executemany("""
            INSERT OR IGNORE INTO tag_translations (tag, source)
            VALUES (?, ?)
        """, [(tag, source[:30]) for tag in normalized])
        await db.commit()
        return db.total_changes - before


async def get_tag_translations(tags) -> Dict[str, str]:
    normalized = _normalize_translation_tags(tags)
    if not normalized:
        return {}
    result: Dict[str, str] = {}
    async with connect_db() as db:
        for offset in range(0, len(normalized), 500):
            chunk = normalized[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            cursor = await db.execute(f"""
                SELECT tag, translation_ru
                FROM tag_translations
                WHERE tag IN ({placeholders})
                  AND status = 'ready'
                  AND translation_ru <> ''
            """, tuple(chunk))
            result.update(await cursor.fetchall())
    return result


async def get_tag_translation_states(tags) -> Dict[str, str]:
    normalized = _normalize_translation_tags(tags)
    if not normalized:
        return {}
    result: Dict[str, str] = {}
    async with connect_db() as db:
        for offset in range(0, len(normalized), 500):
            chunk = normalized[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            cursor = await db.execute(f"""
                SELECT tag, status
                FROM tag_translations
                WHERE tag IN ({placeholders})
                  AND (
                    status IN ('ready', 'unchanged')
                    OR (status = 'failed' AND next_retry_at > CURRENT_TIMESTAMP)
                  )
            """, tuple(chunk))
            result.update(await cursor.fetchall())
    return result


async def get_pending_tag_translations(limit: int = 20) -> List[str]:
    limit = max(1, min(int(limit), 100))
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT tag FROM tag_translations
            WHERE status = 'pending'
               OR (status = 'failed' AND (
                    next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP
               ))
            ORDER BY CASE source
                        WHEN 'blacklist' THEN 0
                        WHEN 'display' THEN 1
                        ELSE 2
                     END,
                     CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                     updated_at ASC, tag ASC
            LIMIT ?
        """, (limit,))
        return [row[0] for row in await cursor.fetchall()]


async def save_tag_translations_bulk(
    translations: Dict[str, str], source: str = "google"
) -> None:
    rows = []
    for tag, translation in translations.items():
        normalized_tag = str(tag).strip().lower()
        normalized_translation = " ".join(str(translation).split()).strip()
        if normalized_tag:
            rows.append((
                normalized_tag,
                normalized_translation,
                "ready" if normalized_translation else "unchanged",
                source[:30],
            ))
    if not rows:
        return
    async with connect_db() as db:
        await db.executemany("""
            INSERT INTO tag_translations
                (tag, translation_ru, status, source, attempts, next_retry_at, updated_at)
            VALUES (?, ?, ?, ?, 1, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT(tag) DO UPDATE SET
                translation_ru = excluded.translation_ru,
                status = excluded.status,
                source = excluded.source,
                attempts = tag_translations.attempts + 1,
                next_retry_at = NULL,
                updated_at = CURRENT_TIMESTAMP
        """, rows)
        await db.commit()


async def mark_tag_translations_failed(tags) -> None:
    normalized = _normalize_translation_tags(tags)
    if not normalized:
        return
    async with connect_db() as db:
        await db.executemany("""
            INSERT INTO tag_translations
                (tag, status, source, attempts, next_retry_at, updated_at)
            VALUES (?, 'failed', 'network', 1, datetime('now', '+6 hours'), CURRENT_TIMESTAMP)
            ON CONFLICT(tag) DO UPDATE SET
                status = 'failed',
                source = 'network',
                attempts = tag_translations.attempts + 1,
                next_retry_at = datetime(
                    'now', '+' || MIN(24, 6 * (tag_translations.attempts + 1)) || ' hours'
                ),
                updated_at = CURRENT_TIMESTAMP
        """, [(tag,) for tag in normalized])
        await db.commit()


async def seed_tag_translation_queue() -> int:
    """Collect known tags without delaying database initialization."""
    inserted = 0
    async with connect_db() as db:
        cursor = await db.execute("SELECT DISTINCT tag FROM blacklist WHERE tag <> ''")
        blacklist_tags = [row[0] for row in await cursor.fetchall()]
    inserted += await queue_tag_translations(blacklist_tags, source="blacklist")

    for table in ("post_cache", "subscription_cache", "subscription_posts", "favorites"):
        async with connect_db() as db:
            cursor = await db.execute(
                f"SELECT tags FROM {table} WHERE COALESCE(tags, '') <> ''"
            )
            while True:
                rows = await cursor.fetchmany(250)
                if not rows:
                    break
                batch = {
                    tag
                    for row in rows
                    for tag in str(row[0] or "").split()
                    if tag
                }
                inserted += await queue_tag_translations(batch, source=table)
    return inserted


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


def _subscription_post_from_row(row) -> Dict[str, Any]:
    post = _post_from_row(row)
    post["width"] = int(row[7] or 0)
    post["height"] = int(row[8] or 0)
    return post


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
            SELECT post_id, file_url, sample_url, preview_url, tags, rating, score,
                   width, height
            FROM subscription_cache
            WHERE user_id = ? AND query = ?
        """, (user_id, query.strip()))
        posts = [_subscription_post_from_row(row) for row in await cursor.fetchall()]

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
            int(post.get("width") or 0),
            int(post.get("height") or 0),
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
                (user_id, query, post_id, file_url, sample_url, preview_url, tags, rating, score,
                 width, height)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    _width,
                    _height,
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
        await db.execute("BEGIN IMMEDIATE")
        try:
            merged_settings = DEFAULT_USER_SETTINGS.copy()
            cursor = await db.execute(
                "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
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
                INSERT INTO user_settings
                (user_id, show_caption, show_search_query, show_subscription_label,
                 show_id, show_score, show_rating, show_tags, settings_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    show_caption = excluded.show_caption,
                    show_search_query = excluded.show_search_query,
                    show_subscription_label = excluded.show_subscription_label,
                    show_id = excluded.show_id,
                    show_score = excluded.show_score,
                    show_rating = excluded.show_rating,
                    show_tags = excluded.show_tags,
                    settings_json = excluded.settings_json
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
        except BaseException:
            await db.rollback()
            raise


async def update_user_setting(user_id: int, setting_name: str, value: Any):
    await save_user_settings(user_id, {setting_name: value})


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
        await db.execute("BEGIN IMMEDIATE")
        try:
            pause_until = await _get_active_subscription_pause_until(db, user_id)
            await db.commit()
            return pause_until
        except BaseException:
            await db.rollback()
            raise


async def _subscription_counts(db, user_id: int) -> tuple[int, int]:
    cursor = await db.execute("""
        SELECT COUNT(*),
               COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0)
        FROM subscriptions WHERE user_id = ?
    """, (user_id,))
    row = await cursor.fetchone()
    return int(row[0] or 0), int(row[1] or 0)


async def get_subscription_usage(user_id: int) -> tuple[int, int]:
    async with connect_db() as db:
        return await _subscription_counts(db, user_id)


async def add_subscription(
    user_id: int,
    query: str,
    interval_minutes: int = 10,
    *,
    total_limit: int | None = None,
    active_limit: int | None = None,
    cooldown_seconds: int | None = None,
) -> SubscriptionAddResult:
    stripped_query = query.strip() if isinstance(query, str) else ""
    normalized_query, validation_error = validate_subscription_query(query)
    configured_total = max(1, int(
        SUBSCRIPTION_MAX_TOTAL if total_limit is None else total_limit
    ))
    configured_active = max(1, min(configured_total, int(
        SUBSCRIPTION_MAX_ACTIVE if active_limit is None else active_limit
    )))
    configured_cooldown = max(0, int(
        SUBSCRIPTION_CREATE_COOLDOWN_SECONDS
        if cooldown_seconds is None else cooldown_seconds
    ))
    if validation_error:
        return SubscriptionAddResult(
            validation_error,
            total_limit=configured_total,
            active_limit=configured_active,
        )

    try:
        async with connect_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute("""
                    SELECT query, is_active FROM subscriptions
                    WHERE user_id = ? AND query = ?
                """, (user_id, stripped_query))
                existing = await cursor.fetchone()
                if existing is None:
                    cursor = await db.execute("""
                        SELECT query, is_active FROM subscriptions
                        WHERE user_id = ? ORDER BY query COLLATE BINARY
                    """, (user_id,))
                    canonical_matches = [
                        row for row in await cursor.fetchall()
                        if normalize_subscription_query(row[0]) == normalized_query
                    ]
                    if len(canonical_matches) > 1:
                        await db.rollback()
                        return SubscriptionAddResult(
                            "ambiguous_query",
                            total_limit=configured_total,
                            active_limit=configured_active,
                        )
                    existing = canonical_matches[0] if canonical_matches else None
                total_count, active_count = await _subscription_counts(db, user_id)
                stored_query = existing[0] if existing is not None else normalized_query

                if existing is not None and bool(existing[1]):
                    await db.execute("""
                        UPDATE subscriptions SET interval_minutes = ?
                        WHERE user_id = ? AND query = ?
                    """, (interval_minutes, user_id, stored_query))
                    await db.commit()
                    return SubscriptionAddResult(
                        "updated", total_count, active_count,
                        configured_total, configured_active,
                    )

                if existing is not None:
                    if total_count > configured_total:
                        await db.rollback()
                        return SubscriptionAddResult(
                            "total_limit_reached", total_count, active_count,
                            configured_total, configured_active,
                        )
                    if active_count >= configured_active:
                        await db.rollback()
                        return SubscriptionAddResult(
                            "active_limit_reached", total_count, active_count,
                            configured_total, configured_active,
                        )
                    await db.execute("""
                        UPDATE subscriptions
                        SET interval_minutes = ?, is_active = 1
                        WHERE user_id = ? AND query = ? AND is_active = 0
                    """, (interval_minutes, user_id, stored_query))
                    await db.commit()
                    return SubscriptionAddResult(
                        "reactivated", total_count, active_count + 1,
                        configured_total, configured_active,
                    )

                if total_count >= configured_total:
                    await db.rollback()
                    return SubscriptionAddResult(
                        "total_limit_reached", total_count, active_count,
                        configured_total, configured_active,
                    )
                if active_count >= configured_active:
                    await db.rollback()
                    return SubscriptionAddResult(
                        "active_limit_reached", total_count, active_count,
                        configured_total, configured_active,
                    )

                cursor = await db.execute("""
                    SELECT MAX(
                        0,
                        ? - (
                            CAST(strftime('%s', 'now') AS INTEGER)
                            - CAST(strftime('%s', last_created_at) AS INTEGER)
                        )
                    )
                    FROM subscription_creation_state WHERE user_id = ?
                """, (configured_cooldown, user_id))
                row = await cursor.fetchone()
                retry_after = int((row[0] if row else 0) or 0)
                if retry_after > 0:
                    await db.rollback()
                    return SubscriptionAddResult(
                        "cooldown", total_count, active_count,
                        configured_total, configured_active, retry_after,
                    )

                pause_until = await _get_active_subscription_pause_until(db, user_id)
                await db.execute("""
                    INSERT INTO subscriptions
                    (
                        user_id, query, interval_minutes, is_active, last_sent,
                        no_new_posts_count, last_empty_at, next_check_at,
                        exhausted_notified, processing_until, processing_token
                    )
                    VALUES (
                        ?, ?, ?, 1, datetime('now', '-1 hour'), 0, NULL,
                        COALESCE(?, datetime('now')), 0, NULL, NULL
                    )
                """, (user_id, normalized_query, interval_minutes, pause_until))
                await db.execute("""
                    INSERT INTO subscription_creation_state(user_id, last_created_at)
                    VALUES (?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id) DO UPDATE SET
                        last_created_at = CURRENT_TIMESTAMP
                """, (user_id,))
                await db.commit()
                return SubscriptionAddResult(
                    "created", total_count + 1, active_count + 1,
                    configured_total, configured_active,
                )
            except BaseException:
                await db.rollback()
                raise
    except Exception as exc:
        logger.exception("Error adding subscription: %s", type(exc).__name__)
        return SubscriptionAddResult(
            "internal_error",
            total_limit=configured_total,
            active_limit=configured_active,
        )


async def remove_subscription(user_id: int, query: str) -> bool:
    async with connect_db() as db:
        normalized_query = query.strip()
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Pending and claimed digest rows belong to a deleted subscription.
            # Deactivation, in contrast, preserves them until reactivation.
            await db.execute(
                "DELETE FROM subscription_digest_queue WHERE user_id = ? AND query = ?",
                (user_id, normalized_query),
            )
            cursor = await db.execute(
                "DELETE FROM subscriptions WHERE user_id = ? AND query = ?",
                (user_id, normalized_query)
            )
            await db.commit()
            return cursor.rowcount > 0
        except BaseException:
            await db.rollback()
            raise


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

        cursor = await db.execute(f"""
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
        if cursor.rowcount != 1:
            return 0, 0, False
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
        await db.execute("BEGIN IMMEDIATE")
        try:
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
        except BaseException:
            await db.rollback()
            raise


async def resume_all_active_subscriptions(user_id: int) -> int:
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
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
        except BaseException:
            await db.rollback()
            raise


async def get_due_subscriptions() -> List[Tuple[int, str, int, int]]:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT s.user_id, s.query, s.interval_minutes, s.no_new_posts_count
            FROM subscriptions s
            WHERE s.is_active = 1
            AND datetime(COALESCE(next_check_at, last_sent)) <= datetime('now')
            AND (
                processing_until IS NULL
                OR datetime(processing_until) <= datetime('now')
            )
            AND NOT EXISTS (
                SELECT 1 FROM user_settings us
                WHERE us.user_id = s.user_id
                  AND datetime(json_extract(
                      CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                           THEN us.settings_json ELSE '{}' END,
                      '$.subscription_pause_until'
                  )) > datetime('now')
            )
            ORDER BY datetime(COALESCE(s.next_check_at, s.last_sent)),
                     s.user_id, s.query COLLATE BINARY
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
              AND NOT EXISTS (
                  SELECT 1 FROM user_settings us
                  WHERE us.user_id = subscriptions.user_id
                    AND datetime(json_extract(
                        CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                             THEN us.settings_json ELSE '{}' END,
                        '$.subscription_pause_until'
                    )) > datetime('now')
              )
        """, (SUBSCRIPTION_CLAIM_MINUTES, token, user_id, query.strip()))
        await db.commit()
        return token if cursor.rowcount == 1 else None


async def defer_subscription_after_transient_failure(
    user_id: int,
    query: str,
    processing_token: str,
    backoff_seconds: int = 60,
) -> bool:
    """Persist a short retry delay and release only the caller's live claim."""
    bounded_backoff = max(1, min(int(backoff_seconds), 3600))
    async with connect_db() as db:
        cursor = await db.execute("""
            UPDATE subscriptions
            SET next_check_at = datetime('now', '+' || ? || ' seconds'),
                processing_until = NULL,
                processing_token = NULL
            WHERE user_id = ? AND query = ? AND processing_token = ?
        """, (bounded_backoff, user_id, query.strip(), processing_token))
        await db.commit()
        return cursor.rowcount == 1


async def is_subscription_claim_active(
    user_id: int, query: str, processing_token: str
) -> bool:
    """Check that a subscription claim is live, active, and not globally paused."""
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT 1 FROM subscriptions s
            WHERE s.user_id = ? AND s.query = ?
              AND s.processing_token = ?
              AND s.processing_until IS NOT NULL
              AND datetime(s.processing_until) > datetime('now')
              AND s.is_active = 1
              AND NOT EXISTS (
                  SELECT 1 FROM user_settings us
                  WHERE us.user_id = s.user_id
                    AND datetime(json_extract(
                        CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                             THEN us.settings_json ELSE '{}' END,
                        '$.subscription_pause_until'
                    )) > datetime('now')
              )
        """, (user_id, query.strip(), processing_token))
        return await cursor.fetchone() is not None


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


async def toggle_subscription(
    user_id: int,
    query: str,
    *,
    active_limit: int | None = None,
    total_limit: int | None = None,
) -> SubscriptionToggleResult:
    configured_total = max(1, int(
        SUBSCRIPTION_MAX_TOTAL if total_limit is None else total_limit
    ))
    configured_active = max(1, min(configured_total, int(
        SUBSCRIPTION_MAX_ACTIVE if active_limit is None else active_limit
    )))
    stored_query = query.strip() if isinstance(query, str) else ""
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT is_active FROM subscriptions WHERE user_id = ? AND query = ?",
                (user_id, stored_query)
            )
            row = await cursor.fetchone()
            if not row:
                await db.rollback()
                return SubscriptionToggleResult(
                    "not_found", active_limit=configured_active,
                    total_limit=configured_total,
                )

            current_state = bool(row[0])
            total_count, active_count = await _subscription_counts(db, user_id)
            if not current_state and total_count > configured_total:
                await db.rollback()
                return SubscriptionToggleResult(
                    "total_limit_reached", False, active_count,
                    configured_active, total_count, configured_total,
                )
            if not current_state and active_count >= configured_active:
                await db.rollback()
                return SubscriptionToggleResult(
                    "active_limit_reached", False, active_count,
                    configured_active, total_count, configured_total,
                )

            new_state = not current_state
            await db.execute("""
                UPDATE subscriptions
                SET is_active = ?,
                    next_check_at = CASE
                        WHEN ? = 1 THEN datetime('now')
                        ELSE next_check_at
                    END
                WHERE user_id = ? AND query = ?
            """, (
                new_state,
                int(new_state),
                user_id,
                stored_query,
            ))
            await db.commit()
            return SubscriptionToggleResult(
                "reactivated" if new_state else "deactivated",
                new_state,
                active_count + (1 if new_state else -1),
                configured_active,
                total_count,
                configured_total,
            )
        except BaseException:
            await db.rollback()
            raise


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


async def claim_delivery_failures(
    limit: int = 20,
    lease_minutes: int = DELIVERY_FAILURE_CLAIM_MINUTES,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Atomically lease delivery failures for one retry worker."""
    token = uuid.uuid4().hex
    bounded_limit = max(1, min(int(limit), 100))
    bounded_lease = max(1, min(int(lease_minutes), 60))
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("""
                SELECT failure_id FROM delivery_failures
                WHERE claim_token IS NULL OR claim_until IS NULL
                   OR datetime(claim_until) <= datetime('now')
                ORDER BY updated_at, failure_id
                LIMIT ?
            """, (bounded_limit,))
            failure_ids = [int(row[0]) for row in await cursor.fetchall()]
            if failure_ids:
                await db.executemany("""
                    UPDATE delivery_failures
                    SET claim_token = ?, claimed_at = CURRENT_TIMESTAMP,
                        claim_until = datetime('now', '+' || ? || ' minutes')
                    WHERE failure_id = ?
                      AND (claim_token IS NULL OR claim_until IS NULL
                           OR datetime(claim_until) <= datetime('now'))
                """, [(token, bounded_lease, failure_id) for failure_id in failure_ids])
            cursor = await db.execute("""
                SELECT failure_id, user_id, post_id, post_json, caption, attempts,
                       last_error, created_at, updated_at
                FROM delivery_failures
                WHERE claim_token = ?
                ORDER BY updated_at, failure_id
            """, (token,))
            rows = await cursor.fetchall()
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    failures = []
    for row in rows:
        try:
            post = json.loads(row[3])
        except json.JSONDecodeError:
            post = {"id": row[2]}
        failures.append({
            "id": row[0], "user_id": row[1], "post_id": row[2],
            "post": post, "caption": row[4], "attempts": row[5],
            "last_error": row[6], "created_at": row[7], "updated_at": row[8],
        })
    return (token if failures else None), failures


async def release_delivery_failure_claim(claim_token: str) -> int:
    async with connect_db() as db:
        cursor = await db.execute("""
            UPDATE delivery_failures
            SET claim_token = NULL, claimed_at = NULL, claim_until = NULL
            WHERE claim_token = ?
        """, (claim_token,))
        await db.commit()
        return max(0, cursor.rowcount)


async def delete_delivery_failure_for_post(
    user_id: int, post_id: int, claim_token: str
) -> bool:
    """Acknowledge confirmed delivery only when the retry lease is still owned."""
    async with connect_db() as db:
        cursor = await db.execute("""
            DELETE FROM delivery_failures
            WHERE user_id = ? AND post_id = ? AND claim_token = ?
              AND claim_until IS NOT NULL
              AND datetime(claim_until) > datetime('now')
        """, (user_id, post_id, claim_token))
        await db.commit()
        return cursor.rowcount == 1


async def clear_delivery_failure_for_post(user_id: int, post_id: int) -> bool:
    """Remove stale failure bookkeeping after any independently confirmed send."""
    async with connect_db() as db:
        cursor = await db.execute("""
            DELETE FROM delivery_failures WHERE user_id = ? AND post_id = ?
        """, (user_id, post_id))
        await db.commit()
        return cursor.rowcount == 1


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
            "delivery_failures", "bot_events", "tag_translations",
        ):
            cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = int((await cursor.fetchone())[0] or 0)
        return {"quick_check": quick_check, "counts": counts}


async def create_search_preset(
    user_id: int, name: str, query: str, settings: Dict[str, Any]
) -> Optional[int]:
    name, query = name.strip()[:40], query.strip()[:500]
    if not name or not query:
        return None
    async with connect_db() as db:
        try:
            cursor = await db.execute("""
                INSERT INTO search_presets (user_id, name, query, settings_json)
                VALUES (?, ?, ?, ?)
            """, (user_id, name, query, json.dumps(settings)))
            await db.commit()
            return int(cursor.lastrowid)
        except aiosqlite.IntegrityError:
            return None


async def get_search_presets(user_id: int) -> List[Dict[str, Any]]:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT preset_id, name, query, settings_json
            FROM search_presets WHERE user_id = ? ORDER BY name COLLATE NOCASE
        """, (user_id,))
        result = []
        for preset_id, name, query, raw_settings in await cursor.fetchall():
            try:
                settings = json.loads(raw_settings or "{}")
            except json.JSONDecodeError:
                settings = {}
            result.append({"id": preset_id, "name": name, "query": query, "settings": settings})
        return result


async def get_search_preset(user_id: int, preset_id: int) -> Optional[Dict[str, Any]]:
    presets = await get_search_presets(user_id)
    return next((item for item in presets if item["id"] == preset_id), None)


async def delete_search_preset(user_id: int, preset_id: int) -> bool:
    async with connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM search_presets WHERE user_id = ? AND preset_id = ?",
            (user_id, preset_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_subscription_options(user_id: int, query: str) -> Dict[str, Any]:
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT settings_json, digest_mode FROM subscriptions
            WHERE user_id = ? AND query = ?
        """, (user_id, query.strip()))
        row = await cursor.fetchone()
        if not row:
            return {"digest_mode": "instant"}
        try:
            options = json.loads(row[0] or "{}")
        except json.JSONDecodeError:
            options = {}
        options["digest_mode"] = row[1] or "instant"
        return options


async def update_subscription_options(
    user_id: int, query: str, options: Dict[str, Any]
) -> bool:
    digest_mode = options.get("digest_mode", "instant")
    if digest_mode not in {"instant", "digest"}:
        digest_mode = "instant"
    stored = {key: value for key, value in options.items() if key != "digest_mode"}
    async with connect_db() as db:
        cursor = await db.execute("""
            UPDATE subscriptions SET settings_json = ?, digest_mode = ?
            WHERE user_id = ? AND query = ?
        """, (json.dumps(stored), digest_mode, user_id, query.strip()))
        await db.commit()
        return cursor.rowcount > 0


async def add_read_later(
    user_id: int, post: Dict[str, Any], retention_days: int = 30
) -> bool:
    normalized = _normalize_post(post)
    if normalized is None:
        return False
    post_id = normalized[0]
    safe_post = dict(post)
    safe_post["id"] = post_id
    days = max(1, min(int(retention_days), 365))
    async with connect_db() as db:
        cursor = await db.execute("""
            INSERT OR IGNORE INTO read_later
            (user_id, post_id, post_json, expires_at)
            VALUES (?, ?, ?, datetime('now', '+' || ? || ' days'))
        """, (user_id, post_id, json.dumps(safe_post), days))
        await db.commit()
        return cursor.rowcount > 0


async def get_read_later(user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    async with connect_db() as db:
        await db.execute(
            "DELETE FROM read_later WHERE expires_at IS NOT NULL AND datetime(expires_at) <= datetime('now')"
        )
        cursor = await db.execute("""
            SELECT post_json FROM read_later WHERE user_id = ?
            ORDER BY added_at DESC LIMIT ?
        """, (user_id, max(1, min(limit, 100))))
        await db.commit()
        result = []
        for (raw_post,) in await cursor.fetchall():
            try:
                result.append(json.loads(raw_post))
            except json.JSONDecodeError:
                continue
        return result


async def remove_read_later(user_id: int, post_id: int) -> bool:
    async with connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM read_later WHERE user_id = ? AND post_id = ?",
            (user_id, post_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def enqueue_subscription_digest(user_id: int, query: str, post: Dict[str, Any]) -> bool:
    normalized = _normalize_post(post)
    if normalized is None:
        return False
    async with connect_db() as db:
        cursor = await db.execute("""
            INSERT OR IGNORE INTO subscription_digest_queue
            (user_id, query, post_id, post_json)
            SELECT ?, ?, ?, ?
            WHERE EXISTS (
                SELECT 1 FROM subscriptions
                WHERE user_id = ? AND query = ? AND is_active = 1
            )
        """, (
            user_id,
            query.strip(),
            normalized[0],
            json.dumps(post),
            user_id,
            query.strip(),
        ))
        await db.commit()
        return cursor.rowcount > 0


async def count_subscription_digest(user_id: int) -> int:
    async with connect_db() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM subscription_digest_queue WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return int(row[0] or 0)


async def claim_subscription_digest(
    user_id: int,
    limit: int = 10,
    lease_minutes: int = DIGEST_CLAIM_MINUTES,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Atomically lease a stable digest batch without deleting it."""
    token = uuid.uuid4().hex
    bounded_limit = max(1, min(int(limit), 10))
    bounded_lease = max(1, int(lease_minutes))
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("""
            SELECT q.query, q.post_id, q.post_json
            FROM subscription_digest_queue q
            INNER JOIN subscriptions s
              ON s.user_id = q.user_id AND s.query = q.query
            WHERE q.user_id = ?
              AND s.is_active = 1
              AND (q.retry_after IS NULL OR datetime(q.retry_after) <= datetime('now'))
              AND (
                  q.claim_token IS NULL
                  OR q.claim_until IS NULL
                  OR datetime(q.claim_until) <= datetime('now')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_settings us
                  WHERE us.user_id = q.user_id
                    AND datetime(json_extract(
                        CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                             THEN us.settings_json ELSE '{}' END,
                        '$.subscription_pause_until'
                    )) > datetime('now')
              )
            ORDER BY q.queued_at, q.query, q.post_id
            LIMIT ?
        """, (user_id, bounded_limit))
            selected = await cursor.fetchall()
            if selected:
                await db.executemany("""
                UPDATE subscription_digest_queue
                SET claim_token = ?,
                    claimed_at = CURRENT_TIMESTAMP,
                    claim_until = datetime('now', '+' || ? || ' minutes'),
                    delivery_state = 'pending',
                    retry_after = NULL
                WHERE user_id = ? AND query = ? AND post_id = ?
                  AND (
                      claim_token IS NULL
                      OR claim_until IS NULL
                      OR datetime(claim_until) <= datetime('now')
                  )
            """, [
                    (token, bounded_lease, user_id, row[0], row[1])
                    for row in selected
                ])
            cursor = await db.execute("""
                SELECT query, post_id, post_json
                FROM subscription_digest_queue q
                WHERE q.user_id = ? AND q.claim_token = ?
                  AND EXISTS (
                      SELECT 1 FROM subscriptions s
                      WHERE s.user_id = q.user_id AND s.query = q.query
                        AND s.is_active = 1
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM user_settings us
                      WHERE us.user_id = q.user_id
                        AND datetime(json_extract(
                            CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                                 THEN us.settings_json ELSE '{}' END,
                            '$.subscription_pause_until'
                        )) > datetime('now')
                  )
                ORDER BY q.queued_at, q.query, q.post_id
            """, (user_id, token))
            rows = await cursor.fetchall()
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

        result = []
        invalid_keys = []
        for query, post_id, raw_post in rows:
            try:
                post = json.loads(raw_post)
                post["subscription_query"] = query
                post["digest_item_key"] = (query, int(post_id))
                result.append(post)
            except json.JSONDecodeError:
                invalid_keys.append((query, int(post_id)))
        if invalid_keys:
            async with connect_db() as cleanup_db:
                for query, post_id in invalid_keys:
                    await cleanup_db.execute("""
                        DELETE FROM subscription_digest_queue
                        WHERE user_id = ? AND query = ? AND post_id = ?
                          AND claim_token = ?
                    """, (user_id, query, post_id, token))
                await cleanup_db.commit()
        return (token if result else None), result


async def release_subscription_digest_claim(user_id: int, claim_token: str) -> int:
    async with connect_db() as db:
        cursor = await db.execute("""
            UPDATE subscription_digest_queue
            SET claim_token = NULL, claimed_at = NULL, claim_until = NULL
            WHERE user_id = ? AND claim_token = ?
        """, (user_id, claim_token))
        await db.commit()
        return cursor.rowcount


async def renew_subscription_digest_claim(
    user_id: int,
    claim_token: str,
    lease_minutes: int = DIGEST_CLAIM_MINUTES,
) -> bool:
    """Extend a live digest lease; false means the batch is no longer owned."""
    bounded_lease = max(1, int(lease_minutes))
    async with connect_db() as db:
        cursor = await db.execute("""
            UPDATE subscription_digest_queue
            SET claim_until = datetime('now', '+' || ? || ' minutes')
            WHERE user_id = ? AND claim_token = ?
              AND claim_until IS NOT NULL
              AND datetime(claim_until) > datetime('now')
              AND EXISTS (
                  SELECT 1 FROM subscriptions s
                  WHERE s.user_id = subscription_digest_queue.user_id
                    AND s.query = subscription_digest_queue.query
                    AND s.is_active = 1
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_settings us
                  WHERE us.user_id = subscription_digest_queue.user_id
                    AND datetime(json_extract(
                        CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                             THEN us.settings_json ELSE '{}' END,
                        '$.subscription_pause_until'
                    )) > datetime('now')
              )
        """, (bounded_lease, user_id, claim_token))
        await db.commit()
        return cursor.rowcount > 0


async def get_subscription_digest_claim_keys(
    user_id: int, claim_token: str
) -> set[Tuple[str, int]]:
    """Return only still-owned items whose source subscription still exists."""
    async with connect_db() as db:
        cursor = await db.execute("""
            SELECT q.query, q.post_id
            FROM subscription_digest_queue q
            WHERE q.user_id = ? AND q.claim_token = ?
              AND q.claim_until IS NOT NULL
              AND datetime(q.claim_until) > datetime('now')
              AND EXISTS (
                  SELECT 1 FROM subscriptions s
                  WHERE s.user_id = q.user_id AND s.query = q.query
                    AND s.is_active = 1
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_settings us
                  WHERE us.user_id = q.user_id
                    AND datetime(json_extract(
                        CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                             THEN us.settings_json ELSE '{}' END,
                        '$.subscription_pause_until'
                    )) > datetime('now')
              )
        """, (user_id, claim_token))
        rows = await cursor.fetchall()
        return {(str(query), int(post_id)) for query, post_id in rows}


async def finish_subscription_digest_claim(
    user_id: int,
    claim_token: str,
    delivered_keys,
    ambiguous_keys=(),
    ambiguous_backoff_seconds: int = 1800,
) -> Tuple[int, int]:
    """Delete confirmed items, defer ambiguous items, and release the remainder."""
    normalized_keys = []
    for query, post_id in delivered_keys:
        try:
            normalized_keys.append((str(query), int(post_id)))
        except (TypeError, ValueError):
            continue
    normalized_ambiguous_keys = []
    for query, post_id in ambiguous_keys:
        try:
            normalized_ambiguous_keys.append((str(query), int(post_id)))
        except (TypeError, ValueError):
            continue
    bounded_backoff = max(1, min(int(ambiguous_backoff_seconds), 86400))

    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            delivered = 0
            for query, post_id in normalized_keys:
                cursor = await db.execute("""
                    DELETE FROM subscription_digest_queue
                    WHERE user_id = ? AND query = ? AND post_id = ?
                      AND claim_token = ?
                """, (user_id, query, post_id, claim_token))
                delivered += max(0, cursor.rowcount)
            for query, post_id in normalized_ambiguous_keys:
                await db.execute("""
                    UPDATE subscription_digest_queue
                    SET delivery_state = 'ambiguous',
                        retry_after = datetime('now', '+' || ? || ' seconds'),
                        claim_token = NULL,
                        claimed_at = NULL,
                        claim_until = NULL
                    WHERE user_id = ? AND query = ? AND post_id = ?
                      AND claim_token = ?
                """, (bounded_backoff, user_id, query, post_id, claim_token))
            cursor = await db.execute("""
                UPDATE subscription_digest_queue
                SET claim_token = NULL, claimed_at = NULL, claim_until = NULL
                WHERE user_id = ? AND claim_token = ?
            """, (user_id, claim_token))
            released = max(0, cursor.rowcount)
            await db.commit()
            return delivered, released
        except BaseException:
            await db.rollback()
            raise


async def get_due_digest_users() -> List[int]:
    async with connect_db() as db:
        cursor = await db.execute("""
                SELECT q.user_id FROM subscription_digest_queue q
                WHERE EXISTS (
                    SELECT 1 FROM subscriptions s
                    WHERE s.user_id = q.user_id
                      AND s.query = q.query
                      AND s.is_active = 1
                )
                  AND (q.retry_after IS NULL OR datetime(q.retry_after) <= datetime('now'))
                  AND (
                    q.claim_token IS NULL
                    OR q.claim_until IS NULL
                    OR datetime(q.claim_until) <= datetime('now')
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM user_settings us
                    WHERE us.user_id = q.user_id
                      AND datetime(json_extract(
                          CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                               THEN us.settings_json ELSE '{}' END,
                          '$.subscription_pause_until'
                      )) > datetime('now')
                  )
                GROUP BY q.user_id
                HAVING COUNT(*) >= 5
                   OR datetime(MIN(q.queued_at)) <= datetime('now', '-6 hours')
                ORDER BY MIN(q.queued_at), q.user_id
                LIMIT 20
        """)
        return [int(row[0]) for row in await cursor.fetchall()]


async def get_favorite_tag_profile(user_id: int, limit: int = 10) -> List[Tuple[str, int]]:
    posts = await get_favorites(user_id, limit=None)
    counts: Dict[str, int] = {}
    ignored = {"solo", "1girl", "1boy", "highres", "absurdres", "explicit", "safe"}
    for post in posts:
        for tag in str(post.get("tags") or "").lower().split():
            if len(tag) > 2 and tag not in ignored:
                counts[tag] = counts.get(tag, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]


async def search_favorites(
    user_id: int, query: str, limit: int = 20
) -> List[Dict[str, Any]]:
    terms = [term.lower() for term in query.split() if term][:8]
    conditions = "".join(
        " AND lower(COALESCE(NULLIF(pc.tags, ''), f.tags) || ' ' || COALESCE(fn.note, '')) LIKE ?"
        for _term in terms
    )
    params: list[Any] = [user_id, *(f"%{term}%" for term in terms), max(1, min(limit, 100))]
    async with connect_db() as db:
        cursor = await db.execute(f"""
            SELECT f.post_id,
                   COALESCE(NULLIF(pc.file_url, ''), f.file_url),
                   COALESCE(NULLIF(pc.sample_url, ''), f.sample_url),
                   COALESCE(NULLIF(pc.preview_url, ''), f.preview_url),
                   COALESCE(NULLIF(pc.tags, ''), f.tags),
                   COALESCE(NULLIF(pc.rating, ''), f.rating),
                   COALESCE(pc.score, f.score), f.added_at
            FROM favorites f
            LEFT JOIN post_cache pc ON pc.post_id = f.post_id
            LEFT JOIN favorite_notes fn ON fn.user_id = f.user_id AND fn.post_id = f.post_id
            WHERE f.user_id = ? {conditions}
            ORDER BY f.added_at DESC LIMIT ?
        """, tuple(params))
        result = []
        for row in await cursor.fetchall():
            post = _post_from_row(row)
            post["added_at"] = row[7]
            result.append(post)
        return result


async def get_user_storage_stats(user_id: int) -> Dict[str, int]:
    async with connect_db() as db:
        counts = {}
        for name, table in (
            ("favorites", "favorites"),
            ("collections", "favorite_collections"),
            ("history", "search_history"),
            ("viewed", "sent_posts"),
            ("read_later", "read_later"),
            ("presets", "search_presets"),
            ("digest", "subscription_digest_queue"),
        ):
            cursor = await db.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user_id,))
            counts[name] = int((await cursor.fetchone())[0] or 0)
        cursor = await db.execute("""
            SELECT COUNT(*) FROM favorite_collections c
            WHERE c.user_id = ? AND NOT EXISTS (
                SELECT 1 FROM favorite_collection_items i
                WHERE i.collection_id = c.collection_id
            )
        """, (user_id,))
        counts["empty_collections"] = int((await cursor.fetchone())[0] or 0)
        cursor = await db.execute("""
            SELECT COUNT(*) FROM favorites f
            LEFT JOIN post_cache pc ON pc.post_id = f.post_id
            WHERE f.user_id = ?
              AND COALESCE(NULLIF(pc.file_url, ''), f.file_url, '') = ''
        """, (user_id,))
        counts["favorites_without_url"] = int((await cursor.fetchone())[0] or 0)
        return counts


async def cleanup_empty_collections(user_id: int) -> int:
    async with connect_db() as db:
        cursor = await db.execute("""
            DELETE FROM favorite_collections
            WHERE user_id = ? AND NOT EXISTS (
                SELECT 1 FROM favorite_collection_items i
                WHERE i.collection_id = favorite_collections.collection_id
            )
        """, (user_id,))
        await db.commit()
        return max(0, cursor.rowcount)


async def cleanup_user_storage(user_id: int, days: int = 90) -> Dict[str, int]:
    days = max(7, min(int(days), 3650))
    async with connect_db() as db:
        removed = {}
        for name, table, column in (
            ("history", "search_history", "searched_at"),
            ("viewed", "sent_posts", "sent_at"),
            ("events", "bot_events", "created_at"),
        ):
            cursor = await db.execute(f"""
                DELETE FROM {table} WHERE user_id = ?
                AND datetime({column}) < datetime('now', '-' || ? || ' days')
            """, (user_id, days))
            removed[name] = max(0, cursor.rowcount)
        await db.commit()
        return removed
