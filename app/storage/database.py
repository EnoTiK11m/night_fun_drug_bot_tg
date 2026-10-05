import sys
from app.storage import migrations as migrations_repository
from app.storage import cache as cache_repository
from app.storage import users as users_repository
from app.storage import translations as translations_repository
from app.storage import subscriptions as subscriptions_repository
from app.storage import favorites as favorites_repository
from app.storage import delivery_failures as delivery_failures_repository
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import aiosqlite
from app.observability.logic_trace import trace_event, trace_error, enabled as trace_enabled, safe_hash, add_timing, annotate

from app.config import (
    DB_PATH,
    SUBSCRIPTION_MIN_INTERVAL_SECONDS,
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


def _json_object(raw: str) -> Dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid settings JSON; using defaults")
        return {}
    if not isinstance(value, dict):
        logger.warning("Settings JSON must be an object; using defaults")
        return {}
    return value

SUBSCRIPTION_EMPTY_BACKOFF_MINUTES = (60, 120, 240, 480, 720)
SENT_POSTS_RETENTION_PER_USER = 5000
SUBSCRIPTION_USER_HISTORY_RETENTION_PER_USER = 10000
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
USER_SETTING_FIELDS = frozenset(DEFAULT_USER_SETTINGS) | {
    "recommendation_excluded_tags",
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
    started = time.monotonic() if trace_enabled() else 0
    db = None
    try:
        db = await aiosqlite.connect(DB_PATH, timeout=30)
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=30000")
        await db.execute("PRAGMA foreign_keys=ON")
        yield db
    except Exception as exc:
        trace_error(exc, stage='sqlite', event='db.error')
        if 'locked' in str(exc).lower():
            trace_event('db.lock_wait', type=type(exc).__name__)
        raise
    finally:
        if db is not None:
            await db.close()
        if started:
            duration = (time.monotonic() - started) * 1000
            add_timing('db', duration)
            trace_event('db.slow_operation' if duration > 250 else 'db.operation', level='normal' if duration > 250 else 'verbose', duration_ms=duration)


async def ensure_subscription_columns(db):
    return await migrations_repository.ensure_subscription_columns(sys.modules[__name__], db)


async def ensure_subscription_cache_columns(db):
    return await migrations_repository.ensure_subscription_cache_columns(sys.modules[__name__], db)


async def ensure_media_post_columns(db, table_name: str):
    return await migrations_repository.ensure_media_post_columns(sys.modules[__name__], db, table_name)


async def ensure_blacklist_columns(db):
    return await migrations_repository.ensure_blacklist_columns(sys.modules[__name__], db)


async def apply_versioned_migrations(db):
    """Apply small, restart-safe schema migrations after the base schema commit."""
    return await migrations_repository.apply_versioned_migrations(sys.modules[__name__], db)


def get_empty_backoff_minutes(empty_count: int, interval_minutes: int) -> int:
    index = max(0, min(empty_count - 1, len(SUBSCRIPTION_EMPTY_BACKOFF_MINUTES) - 1))
    return max(interval_minutes, SUBSCRIPTION_EMPTY_BACKOFF_MINUTES[index])


async def init_db():
    return await migrations_repository.init_db(sys.modules[__name__])


def _deleted_row_count(cursor: aiosqlite.Cursor) -> int:
    return cache_repository._deleted_row_count(sys.modules[__name__], cursor)


async def _cleanup_expired_caches_in_connection(db: aiosqlite.Connection, *, subscription_ttl_minutes: int, subscription_max_per_query: int, subscription_max_rows: int, post_ttl_hours: int, post_max_rows: int, batch_size: int) -> CacheCleanupResult:
    return await cache_repository._cleanup_expired_caches_in_connection(sys.modules[__name__], db, subscription_ttl_minutes=subscription_ttl_minutes, subscription_max_per_query=subscription_max_per_query, subscription_max_rows=subscription_max_rows, post_ttl_hours=post_ttl_hours, post_max_rows=post_max_rows, batch_size=batch_size)


async def cleanup_expired_caches(*, subscription_ttl_minutes: int=SUBSCRIPTION_CACHE_TTL_MINUTES, subscription_max_per_query: int=SUBSCRIPTION_CACHE_MAX_PER_QUERY, subscription_max_rows: int=SUBSCRIPTION_CACHE_MAX_ROWS, post_ttl_hours: int=POST_CACHE_TTL_HOURS, post_max_rows: int=POST_CACHE_MAX_ROWS, batch_size: int=SUBSCRIPTION_CACHE_CLEANUP_BATCH_SIZE, db: aiosqlite.Connection | None=None) -> CacheCleanupResult:
    """Delete a bounded cache batch in one short transaction."""
    return await cache_repository.cleanup_expired_caches(sys.modules[__name__], subscription_ttl_minutes=subscription_ttl_minutes, subscription_max_per_query=subscription_max_per_query, subscription_max_rows=subscription_max_rows, post_ttl_hours=post_ttl_hours, post_max_rows=post_max_rows, batch_size=batch_size, db=db)


async def get_cache_storage_stats() -> Dict[str, int]:
    return await cache_repository.get_cache_storage_stats(sys.modules[__name__])


async def get_user_blacklist(user_id: int) -> Set[str]:
    return await users_repository.get_user_blacklist(sys.modules[__name__], user_id)


def _normalize_translation_tags(tags) -> List[str]:
    return translations_repository._normalize_translation_tags(sys.modules[__name__], tags)


async def queue_tag_translations(tags, source: str='queue') -> int:
    return await translations_repository.queue_tag_translations(sys.modules[__name__], tags, source)


async def get_tag_translations(tags) -> Dict[str, str]:
    return await translations_repository.get_tag_translations(sys.modules[__name__], tags)


async def get_tag_translation_states(tags) -> Dict[str, str]:
    return await translations_repository.get_tag_translation_states(sys.modules[__name__], tags)


async def get_pending_tag_translations(limit: int=20) -> List[str]:
    return await translations_repository.get_pending_tag_translations(sys.modules[__name__], limit)


async def save_tag_translations_bulk(translations: Dict[str, str], source: str='google') -> None:
    return await translations_repository.save_tag_translations_bulk(sys.modules[__name__], translations, source)


async def mark_tag_translations_failed(tags) -> None:
    return await translations_repository.mark_tag_translations_failed(sys.modules[__name__], tags)


async def seed_tag_translation_queue() -> int:
    """Collect known tags without delaying database initialization."""
    return await translations_repository.seed_tag_translation_queue(sys.modules[__name__])


async def add_to_blacklist(user_id: int, tag: str) -> bool:
    return await users_repository.add_to_blacklist(sys.modules[__name__], user_id, tag)


async def add_temporary_blacklist_tag(user_id: int, tag: str, minutes: int) -> bool:
    return await users_repository.add_temporary_blacklist_tag(sys.modules[__name__], user_id, tag, minutes)


async def get_blacklist_entries(user_id: int) -> List[Dict[str, Any]]:
    return await users_repository.get_blacklist_entries(sys.modules[__name__], user_id)


async def apply_blacklist_preset(user_id: int, preset: str) -> int:
    return await users_repository.apply_blacklist_preset(sys.modules[__name__], user_id, preset)


async def remove_blacklist_preset(user_id: int, preset: str) -> int:
    return await users_repository.remove_blacklist_preset(sys.modules[__name__], user_id, preset)


async def replace_user_blacklist(user_id: int, tags: Set[str]) -> int:
    return await users_repository.replace_user_blacklist(sys.modules[__name__], user_id, tags)


async def remove_from_blacklist(user_id: int, tag: str) -> bool:
    return await users_repository.remove_from_blacklist(sys.modules[__name__], user_id, tag)


async def save_user_query(user_id: int, query: str, pid: int=0):
    return await users_repository.save_user_query(sys.modules[__name__], user_id, query, pid)


async def get_user_query(user_id: int) -> Optional[tuple]:
    return await users_repository.get_user_query(sys.modules[__name__], user_id)


async def get_sent_post_ids(user_id: int) -> Set[int]:
    return await users_repository.get_sent_post_ids(sys.modules[__name__], user_id)


async def mark_post_sent(user_id: int, post_id: int):
    return await users_repository.mark_post_sent(sys.modules[__name__], user_id, post_id)


def _post_from_row(row) -> Dict[str, Any]:
    return users_repository._post_from_row(sys.modules[__name__], row)


def _subscription_post_from_row(row) -> Dict[str, Any]:
    return users_repository._subscription_post_from_row(sys.modules[__name__], row)


def _normalize_post(post: Dict[str, Any]) -> Optional[tuple[int, str, str, str, str, str, int]]:
    return users_repository._normalize_post(sys.modules[__name__], post)


async def cache_post(post: Dict[str, Any]) -> bool:
    return await cache_repository.cache_post(sys.modules[__name__], post)


async def get_cached_post(post_id: int) -> Optional[Dict[str, Any]]:
    return await cache_repository.get_cached_post(sys.modules[__name__], post_id)


async def get_subscription_cache(user_id: int, query: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    return await cache_repository.get_subscription_cache(sys.modules[__name__], user_id, query)


async def is_subscription_cache_stale(user_id: int, query: str) -> bool:
    return await cache_repository.is_subscription_cache_stale(sys.modules[__name__], user_id, query)


async def replace_subscription_cache(user_id: int, query: str, posts: List[Dict[str, Any]]) -> Dict[str, int]:
    return await cache_repository.replace_subscription_cache(sys.modules[__name__], user_id, query, posts)


async def get_search_history(user_id: int, limit: int=10) -> List[str]:
    return await users_repository.get_search_history(sys.modules[__name__], user_id, limit)


async def get_user_settings(user_id: int) -> Dict[str, Any]:
    return await users_repository.get_user_settings(sys.modules[__name__], user_id)


async def save_user_settings(user_id: int, settings: Dict[str, Any]):
    """Atomically merge a whitelisted partial settings patch."""
    return await users_repository.save_user_settings(sys.modules[__name__], user_id, settings)


async def update_user_setting(user_id: int, setting_name: str, value: Any):
    return await users_repository.update_user_setting(sys.modules[__name__], user_id, setting_name, value)


def _parse_sqlite_timestamp(value: Optional[str]) -> Optional[datetime]:
    return users_repository._parse_sqlite_timestamp(sys.modules[__name__], value)


async def _get_settings_json(db, user_id: int) -> Dict[str, Any]:
    return await users_repository._get_settings_json(sys.modules[__name__], db, user_id)


async def _save_settings_json(db, user_id: int, settings_json: Dict[str, Any]):
    return await users_repository._save_settings_json(sys.modules[__name__], db, user_id, settings_json)


async def _get_active_subscription_pause_until(db, user_id: int) -> Optional[str]:
    return await users_repository._get_active_subscription_pause_until(sys.modules[__name__], db, user_id)


async def get_subscription_pause_until(user_id: int) -> Optional[str]:
    return await subscriptions_repository.get_subscription_pause_until(sys.modules[__name__], user_id)


async def _subscription_counts(db, user_id: int) -> tuple[int, int]:
    return await subscriptions_repository._subscription_counts(sys.modules[__name__], db, user_id)


async def get_subscription_usage(user_id: int) -> tuple[int, int]:
    return await subscriptions_repository.get_subscription_usage(sys.modules[__name__], user_id)


async def add_subscription(user_id: int, query: str, interval_minutes: int=10, *, interval_seconds: int | None=None, total_limit: int | None=None, active_limit: int | None=None, cooldown_seconds: int | None=None) -> SubscriptionAddResult:
    return await subscriptions_repository.add_subscription(sys.modules[__name__], user_id, query, interval_minutes, interval_seconds=interval_seconds, total_limit=total_limit, active_limit=active_limit, cooldown_seconds=cooldown_seconds)


async def remove_subscription(user_id: int, query: str) -> bool:
    return await subscriptions_repository.remove_subscription(sys.modules[__name__], user_id, query)


async def get_user_subscriptions(user_id: int) -> List[Tuple[str, int]]:
    return await subscriptions_repository.get_user_subscriptions(sys.modules[__name__], user_id)


async def get_all_user_subscriptions(user_id: int) -> List[Tuple[str, int, bool, int, Optional[str]]]:
    return await subscriptions_repository.get_all_user_subscriptions(sys.modules[__name__], user_id)


async def update_subscription_time(user_id: int, query: str, processing_token: Optional[str]=None) -> bool:
    return await subscriptions_repository.update_subscription_time(sys.modules[__name__], user_id, query, processing_token)


async def mark_subscription_empty(user_id: int, query: str, processing_token: Optional[str]=None) -> Tuple[int, int, bool]:
    return await subscriptions_repository.mark_subscription_empty(sys.modules[__name__], user_id, query, processing_token)


async def update_subscription_interval(user_id: int, query: str, interval_minutes: int=10, *, interval_seconds: int | None=None) -> bool:
    return await subscriptions_repository.update_subscription_interval(sys.modules[__name__], user_id, query, interval_minutes, interval_seconds=interval_seconds)


async def pause_all_active_subscriptions(user_id: int, pause_minutes: int) -> int:
    return await subscriptions_repository.pause_all_active_subscriptions(sys.modules[__name__], user_id, pause_minutes)


async def resume_all_active_subscriptions(user_id: int) -> int:
    return await subscriptions_repository.resume_all_active_subscriptions(sys.modules[__name__], user_id)


async def get_due_subscriptions() -> List[Tuple[int, str, int, int]]:
    return await subscriptions_repository.get_due_subscriptions(sys.modules[__name__])


async def claim_due_subscription(user_id: int, query: str) -> Optional[str]:
    return await subscriptions_repository.claim_due_subscription(sys.modules[__name__], user_id, query)


async def defer_subscription_after_transient_failure(user_id: int, query: str, processing_token: str, backoff_seconds: int=60) -> bool:
    """Persist a short retry delay and release only the caller's live claim."""
    return await subscriptions_repository.defer_subscription_after_transient_failure(sys.modules[__name__], user_id, query, processing_token, backoff_seconds)


async def is_subscription_claim_active(user_id: int, query: str, processing_token: str) -> bool:
    """Check that a subscription claim is live, active, and not globally paused."""
    return await subscriptions_repository.is_subscription_claim_active(sys.modules[__name__], user_id, query, processing_token)


async def release_subscription_claim(user_id: int, query: str, processing_token: str):
    return await subscriptions_repository.release_subscription_claim(sys.modules[__name__], user_id, query, processing_token)


async def release_stale_subscription_claims():
    return await subscriptions_repository.release_stale_subscription_claims(sys.modules[__name__])


async def toggle_subscription(user_id: int, query: str, *, active_limit: int | None=None, total_limit: int | None=None) -> SubscriptionToggleResult:
    return await subscriptions_repository.toggle_subscription(sys.modules[__name__], user_id, query, active_limit=active_limit, total_limit=total_limit)


async def add_favorite(user_id: int, post: Dict[str, Any]) -> bool:
    return await favorites_repository.add_favorite(sys.modules[__name__], user_id, post)


async def remove_favorite(user_id: int, post_id: int) -> bool:
    return await favorites_repository.remove_favorite(sys.modules[__name__], user_id, post_id)


def _normalize_collection_name(name: str) -> str:
    return favorites_repository._normalize_collection_name(sys.modules[__name__], name)


async def create_favorite_collection(user_id: int, name: str) -> Optional[int]:
    return await favorites_repository.create_favorite_collection(sys.modules[__name__], user_id, name)


async def get_favorite_collections(user_id: int) -> List[Dict[str, Any]]:
    return await favorites_repository.get_favorite_collections(sys.modules[__name__], user_id)


async def get_favorite_collection(user_id: int, collection_id: int) -> Optional[Dict[str, Any]]:
    return await favorites_repository.get_favorite_collection(sys.modules[__name__], user_id, collection_id)


async def rename_favorite_collection(user_id: int, collection_id: int, name: str) -> bool:
    return await favorites_repository.rename_favorite_collection(sys.modules[__name__], user_id, collection_id, name)


async def delete_favorite_collection(user_id: int, collection_id: int) -> bool:
    return await favorites_repository.delete_favorite_collection(sys.modules[__name__], user_id, collection_id)


async def add_favorite_to_collection(user_id: int, collection_id: int, post_id: int) -> bool:
    return await favorites_repository.add_favorite_to_collection(sys.modules[__name__], user_id, collection_id, post_id)


async def remove_favorite_from_collection(user_id: int, collection_id: int, post_id: int) -> bool:
    return await favorites_repository.remove_favorite_from_collection(sys.modules[__name__], user_id, collection_id, post_id)


async def get_collection_favorites(user_id: int, collection_id: int, limit: Optional[int]=10, offset: int=0) -> List[Dict[str, Any]]:
    return await favorites_repository.get_collection_favorites(sys.modules[__name__], user_id, collection_id, limit, offset)


async def count_collection_favorites(user_id: int, collection_id: int) -> int:
    return await favorites_repository.count_collection_favorites(sys.modules[__name__], user_id, collection_id)


async def set_favorite_note(user_id: int, post_id: int, note: str) -> bool:
    return await favorites_repository.set_favorite_note(sys.modules[__name__], user_id, post_id, note)


async def get_favorite_note(user_id: int, post_id: int) -> str:
    return await favorites_repository.get_favorite_note(sys.modules[__name__], user_id, post_id)


async def get_favorites(user_id: int, limit: Optional[int]=10, offset: int=0, tag_filter: str='') -> List[Dict[str, Any]]:
    return await favorites_repository.get_favorites(sys.modules[__name__], user_id, limit, offset, tag_filter)


async def get_favorite_by_index(user_id: int, index: int, tag_filter: str='') -> Optional[Dict[str, Any]]:
    return await favorites_repository.get_favorite_by_index(sys.modules[__name__], user_id, index, tag_filter)


async def get_favorite(user_id: int, post_id: int) -> Optional[Dict[str, Any]]:
    return await favorites_repository.get_favorite(sys.modules[__name__], user_id, post_id)


async def count_favorites(user_id: int, tag_filter: str='') -> int:
    return await favorites_repository.count_favorites(sys.modules[__name__], user_id, tag_filter)


async def add_subscription_post(user_id: int, query: str, post: Dict[str, Any]) -> bool:
    return await subscriptions_repository.add_subscription_post(sys.modules[__name__], user_id, query, post)


async def get_subscription_posts(user_id: int, query: str, limit: Optional[int]=None, offset: int=0) -> List[Dict[str, Any]]:
    return await subscriptions_repository.get_subscription_posts(sys.modules[__name__], user_id, query, limit, offset)


async def count_subscription_posts(user_id: int, query: str) -> int:
    return await subscriptions_repository.count_subscription_posts(sys.modules[__name__], user_id, query)


async def get_subscription_queries_for_post(user_id: int, post_id: int) -> List[str]:
    return await subscriptions_repository.get_subscription_queries_for_post(sys.modules[__name__], user_id, post_id)


async def get_subscription_post_by_index(user_id: int, query: str, index: int) -> Optional[Dict[str, Any]]:
    return await subscriptions_repository.get_subscription_post_by_index(sys.modules[__name__], user_id, query, index)


async def remove_subscription_post(user_id: int, query: str, post_id: int) -> bool:
    return await subscriptions_repository.remove_subscription_post(sys.modules[__name__], user_id, query, post_id)


async def get_user_activity_stats(user_id: int) -> Dict[str, Any]:
    return await users_repository.get_user_activity_stats(sys.modules[__name__], user_id)


async def clear_user_activity_stats(user_id: int):
    return await users_repository.clear_user_activity_stats(sys.modules[__name__], user_id)


async def save_delivery_failure(user_id: int, post: Dict[str, Any], caption: str='', error: str='delivery failed'):
    return await delivery_failures_repository.save_delivery_failure(sys.modules[__name__], user_id, post, caption, error)


async def get_delivery_failures(limit: int=20) -> List[Dict[str, Any]]:
    return await delivery_failures_repository.get_delivery_failures(sys.modules[__name__], limit)


async def claim_delivery_failures(limit: int=20, lease_minutes: int=DELIVERY_FAILURE_CLAIM_MINUTES) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Atomically lease delivery failures for one retry worker."""
    return await delivery_failures_repository.claim_delivery_failures(sys.modules[__name__], limit, lease_minutes)


async def release_delivery_failure_claim(claim_token: str) -> int:
    return await delivery_failures_repository.release_delivery_failure_claim(sys.modules[__name__], claim_token)


async def renew_delivery_failure_claim_for_post(user_id: int, post_id: int, claim_token: str, lease_minutes: int=DELIVERY_FAILURE_CLAIM_MINUTES) -> bool:
    """Extend a still-owned item lease immediately before external delivery."""
    return await delivery_failures_repository.renew_delivery_failure_claim_for_post(sys.modules[__name__], user_id, post_id, claim_token, lease_minutes)


async def delete_delivery_failure_for_post(user_id: int, post_id: int, claim_token: str) -> bool:
    """Acknowledge confirmed delivery only when the retry lease is still owned."""
    return await delivery_failures_repository.delete_delivery_failure_for_post(sys.modules[__name__], user_id, post_id, claim_token)


async def clear_delivery_failure_for_post(user_id: int, post_id: int) -> bool:
    """Remove stale failure bookkeeping after any independently confirmed send."""
    return await delivery_failures_repository.clear_delivery_failure_for_post(sys.modules[__name__], user_id, post_id)


async def delete_delivery_failure(failure_id: int):
    return await delivery_failures_repository.delete_delivery_failure(sys.modules[__name__], failure_id)


async def get_admin_database_stats() -> Dict[str, Any]:
    return await users_repository.get_admin_database_stats(sys.modules[__name__])


async def create_search_preset(user_id: int, name: str, query: str, settings: Dict[str, Any]) -> Optional[int]:
    return await users_repository.create_search_preset(sys.modules[__name__], user_id, name, query, settings)


async def get_search_presets(user_id: int) -> List[Dict[str, Any]]:
    return await users_repository.get_search_presets(sys.modules[__name__], user_id)


async def get_search_preset(user_id: int, preset_id: int) -> Optional[Dict[str, Any]]:
    return await users_repository.get_search_preset(sys.modules[__name__], user_id, preset_id)


async def delete_search_preset(user_id: int, preset_id: int) -> bool:
    return await users_repository.delete_search_preset(sys.modules[__name__], user_id, preset_id)


async def mark_delivery_failure_permanent(user_id: int, post_id: int, token: str, error: str):
    return await delivery_failures_repository.mark_delivery_failure_permanent(sys.modules[__name__], user_id, post_id, token, error)


async def get_subscription_options(user_id: int, query: str) -> Dict[str, Any]:
    return await subscriptions_repository.get_subscription_options(sys.modules[__name__], user_id, query)


async def update_subscription_options(user_id: int, query: str, options: Dict[str, Any]) -> bool:
    return await subscriptions_repository.update_subscription_options(sys.modules[__name__], user_id, query, options)


async def add_read_later(user_id: int, post: Dict[str, Any], retention_days: int=30) -> bool:
    return await favorites_repository.add_read_later(sys.modules[__name__], user_id, post, retention_days)


async def get_read_later(user_id: int, limit: int=50) -> List[Dict[str, Any]]:
    return await favorites_repository.get_read_later(sys.modules[__name__], user_id, limit)


async def remove_read_later(user_id: int, post_id: int) -> bool:
    return await favorites_repository.remove_read_later(sys.modules[__name__], user_id, post_id)


async def enqueue_subscription_digest(user_id: int, query: str, post: Dict[str, Any]) -> bool:
    return await subscriptions_repository.enqueue_subscription_digest(sys.modules[__name__], user_id, query, post)


async def count_subscription_digest(user_id: int) -> int:
    return await subscriptions_repository.count_subscription_digest(sys.modules[__name__], user_id)


async def claim_subscription_digest(user_id: int, limit: int=10, lease_minutes: int=DIGEST_CLAIM_MINUTES) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Atomically lease a stable digest batch without deleting it."""
    return await subscriptions_repository.claim_subscription_digest(sys.modules[__name__], user_id, limit, lease_minutes)


async def release_subscription_digest_claim(user_id: int, claim_token: str) -> int:
    return await subscriptions_repository.release_subscription_digest_claim(sys.modules[__name__], user_id, claim_token)


async def renew_subscription_digest_claim(user_id: int, claim_token: str, lease_minutes: int=DIGEST_CLAIM_MINUTES) -> bool:
    """Extend a live digest lease; false means the batch is no longer owned."""
    return await subscriptions_repository.renew_subscription_digest_claim(sys.modules[__name__], user_id, claim_token, lease_minutes)


async def get_subscription_digest_claim_keys(user_id: int, claim_token: str) -> set[Tuple[str, int]]:
    """Return only still-owned items whose source subscription still exists."""
    return await subscriptions_repository.get_subscription_digest_claim_keys(sys.modules[__name__], user_id, claim_token)


async def finish_subscription_digest_claim(user_id: int, claim_token: str, delivered_keys, ambiguous_keys=(), ambiguous_backoff_seconds: int=1800) -> Tuple[int, int]:
    """Delete confirmed items, defer ambiguous items, and release the remainder."""
    return await subscriptions_repository.finish_subscription_digest_claim(sys.modules[__name__], user_id, claim_token, delivered_keys, ambiguous_keys, ambiguous_backoff_seconds)


async def get_due_digest_users() -> List[int]:
    return await subscriptions_repository.get_due_digest_users(sys.modules[__name__])


async def get_favorite_tag_profile(user_id: int, limit: int=10) -> List[Tuple[str, int]]:
    return await favorites_repository.get_favorite_tag_profile(sys.modules[__name__], user_id, limit)


async def search_favorites(user_id: int, query: str, limit: int=20) -> List[Dict[str, Any]]:
    return await favorites_repository.search_favorites(sys.modules[__name__], user_id, query, limit)


async def get_user_storage_stats(user_id: int) -> Dict[str, int]:
    return await favorites_repository.get_user_storage_stats(sys.modules[__name__], user_id)


async def cleanup_empty_collections(user_id: int) -> int:
    return await favorites_repository.cleanup_empty_collections(sys.modules[__name__], user_id)


async def cleanup_user_storage(user_id: int, days: int=90) -> Dict[str, int]:
    return await favorites_repository.cleanup_user_storage(sys.modules[__name__], user_id, days)
