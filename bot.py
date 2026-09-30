import logging
import asyncio
import time
import random
import os
import sys
import json
import shutil
import io
import re
import secrets
import hashlib
from collections import OrderedDict
from functools import wraps
from dataclasses import dataclass, field
from contextlib import asynccontextmanager, contextmanager
from logging.handlers import RotatingFileHandler
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaPhoto,
    InputMediaVideo,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from config import (
    ALLOW_GROUP_CHATS,
    ALLOWED_CHAT_IDS,
    ALLOWED_USER_IDS,
    ADMIN_USER_IDS,
    API_KEY,
    API_USER_ID,
    BOT_TOKEN,
    DB_PATH,
    INSTANCE_LOCK_RETRY_INTERVAL_SECONDS,
    INSTANCE_LOCK_WAIT_SECONDS,
    SEARCH_COOLDOWN_SECONDS,
    SUBSCRIPTION_CHECK_INTERVAL_SECONDS,
    SUBSCRIPTION_MAX_ACTIVE,
    SUBSCRIPTION_MAX_TOTAL,
    SUBSCRIPTION_MAX_POSTS_PER_USER_PASS,
    SUBSCRIPTION_CACHE_CLEANUP_INTERVAL_SECONDS,
    USER_STATE_CLEANUP_INTERVAL_SECONDS,
    USER_STATE_TTL_MINUTES,
    ZIP_EXPORT_MAX_FILES,
    TAG_TRANSLATION_ENABLED,
    validate_config,
)
from bot_formatting import (
    FAVORITES_PAGE_SIZE,
    SQLITE_TIMESTAMP_FORMAT,
    build_caption,
    build_favorites_gallery_caption,
    build_full_tags_messages,
    build_subscription_gallery_caption,
    clamp_page,
    format_pause_duration,
    format_remaining_pause,
    md_code,
    md_text,
    parse_pause_minutes,
    parse_subscription_interval,
)
from bot_keyboards import (
    get_blacklist_keyboard,
    get_caption_settings_keyboard,
    get_favorites_gallery_keyboard,
    get_favorites_album_keyboard,
    get_image_keyboard,
    get_main_keyboard,
    get_data_keyboard,
    get_help_keyboard,
    get_library_keyboard,
    get_search_hub_keyboard,
    get_post_more_keyboard,
    get_random_image_keyboard,
    get_settings_keyboard,
    get_subscription_gallery_keyboard,
    get_subscription_image_keyboard,
    get_subscriptions_keyboard,
    get_gallery_settings_keyboard,
    get_quality_settings_keyboard,
    get_gallery_result_keyboard,
    get_persistent_keyboard,
    get_onboarding_keyboard,
    get_cancel_keyboard,
    PERSISTENT_SEARCH,
    PERSISTENT_GALLERY,
    PERSISTENT_RANDOM,
    PERSISTENT_FAVORITES,
    PERSISTENT_SUBSCRIPTIONS,
    PERSISTENT_MENU,
    LEGACY_PERSISTENT_SEARCH,
    LEGACY_PERSISTENT_RANDOM,
    LEGACY_PERSISTENT_FAVORITES,
    LEGACY_PERSISTENT_MENU,
)
from bot_features import (
    filter_and_sort_posts,
    media_group_compatible_url,
    normalize_feature_settings,
    post_matches_preferences,
    prepare_gallery_album_posts,
    prepare_post_quality,
    runtime_metrics,
)
from bot_media import (
    get_media_url_candidates,
    media_url_path_lower,
    send_post_media as send_post_media_with_retries,
    send_post_media_to_chat as send_post_media_to_chat_with_retries,
    send_text_to_chat,
)
from bot_delivery import execute_telegram_request, telegram_rate_limiter
from bot_user_gate import user_operation_gate
from bot_instance_lock import (
    BotInstanceLifecycle,
    InstanceLockBusy,
    InstanceLockLifecycleError,
    create_instance_lifecycle,
)
from project_update import (
    UpdateCommandError,
    check_for_updates,
    get_version_info,
    notify_update_marker,
    perform_update,
    update_operation_lock,
    write_update_marker,
)
from bot_zip_export import ZipExportManager, ZipExportSource
from bot_state import (
    get_callback_payload,
    get_callback_payload_by_token,
    get_remembered_post,
    minimal_post,
    recent_posts,
    remember_post,
    store_callback_payload,
)
from tag_translation import tag_translation_service
from database import (
    init_db,
    cleanup_expired_caches,
    get_user_blacklist,
    add_to_blacklist,
    remove_from_blacklist,
    save_user_query,
    get_user_query,
    add_subscription,
    remove_subscription,
    get_all_user_subscriptions,
    update_subscription_time,
    mark_subscription_empty,
    update_subscription_interval,
    pause_all_active_subscriptions,
    resume_all_active_subscriptions,
    get_subscription_pause_until,
    get_subscription_usage,
    get_due_subscriptions,
    claim_due_subscription,
    is_subscription_claim_active,
    defer_subscription_after_transient_failure,
    release_subscription_claim,
    release_stale_subscription_claims,
    toggle_subscription,
    get_user_settings,
    save_user_settings,
    get_search_history,
    get_sent_post_ids,
    get_subscription_cache,
    is_subscription_cache_stale,
    cache_post,
    get_cached_post,
    mark_post_sent,
    replace_subscription_cache,
    SUBSCRIPTION_CACHE_MIN_AVAILABLE,
    DEFAULT_USER_SETTINGS,
    add_favorite,
    remove_favorite,
    get_favorite,
    get_favorites,
    get_favorite_by_index,
    count_favorites,
    add_subscription_post,
    get_subscription_posts,
    count_subscription_posts,
    get_subscription_queries_for_post,
    get_subscription_post_by_index,
    remove_subscription_post,
    BLACKLIST_PRESETS,
    add_temporary_blacklist_tag,
    get_blacklist_entries,
    apply_blacklist_preset,
    remove_blacklist_preset,
    replace_user_blacklist,
    create_favorite_collection,
    get_favorite_collections,
    get_favorite_collection,
    rename_favorite_collection,
    delete_favorite_collection,
    add_favorite_to_collection,
    remove_favorite_from_collection,
    get_collection_favorites,
    count_collection_favorites,
    set_favorite_note,
    get_favorite_note,
    get_user_activity_stats,
    clear_user_activity_stats,
    save_delivery_failure,
    claim_delivery_failures,
    release_delivery_failure_claim,
    renew_delivery_failure_claim_for_post,
    delete_delivery_failure_for_post,
    clear_delivery_failure_for_post,
    get_admin_database_stats,
    create_search_preset,
    get_search_presets,
    get_search_preset,
    delete_search_preset,
    get_subscription_options,
    update_subscription_options,
    add_read_later,
    get_read_later,
    remove_read_later,
    enqueue_subscription_digest,
    count_subscription_digest,
    claim_subscription_digest,
    finish_subscription_digest_claim,
    release_subscription_digest_claim,
    renew_subscription_digest_claim,
    get_subscription_digest_claim_keys,
    get_due_digest_users,
    get_favorite_tag_profile,
    search_favorites,
    get_user_storage_stats,
    cleanup_user_storage,
    cleanup_empty_collections,
)
from api_handler import api, APITemporaryError


class RedactingFormatter(logging.Formatter):
    SECRET_PLACEHOLDERS = (
        ("BOT_TOKEN", "<BOT_TOKEN>"),
        ("API_KEY", "<API_KEY>"),
        ("API_USER_ID", "<API_USER_ID>"),
    )

    def format(self, record):
        message = super().format(record)
        for attr_name, placeholder in self.SECRET_PLACEHOLDERS:
            secret = globals().get(attr_name)
            if secret:
                message = message.replace(str(secret), placeholder)
        return message


class ExactLevelFilter(logging.Filter):
    def __init__(self, level):
        super().__init__()
        self.level = level

    def filter(self, record):
        return record.levelno == self.level


def configure_logging():
    os.makedirs("logs", exist_ok=True)
    formatter = RedactingFormatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    info_handler = RotatingFileHandler(
            os.path.join("logs", "info.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    warning_handler = RotatingFileHandler(
            os.path.join("logs", "warnings.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    error_handler = RotatingFileHandler(
            os.path.join("logs", "errors.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )

    info_handler.setLevel(logging.INFO)
    info_handler.addFilter(ExactLevelFilter(logging.INFO))
    warning_handler.setLevel(logging.WARNING)
    warning_handler.addFilter(ExactLevelFilter(logging.WARNING))
    error_handler.setLevel(logging.ERROR)

    handlers = [info_handler, warning_handler, error_handler]
    # Avoid duplicating all application logs into the launcher's redirected
    # stderr file. An interactive terminal still gets normal console output.
    if sys.stderr.isatty():
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        handlers.append(console_handler)
    for handler in handlers:
        handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)
    for handler in handlers:
        root_logger.addHandler(handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


# Состояния пользователей
class TrackedUserStateDict(dict):
    def __init__(self, registry):
        super().__init__()
        self._registry = registry

    def __getitem__(self, key):
        value = super().__getitem__(key)
        if isinstance(key, int):
            self._registry.touch(key)
        return value

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if isinstance(key, int):
            self._registry.touch(key)

    def __contains__(self, key):
        present = super().__contains__(key)
        if present and isinstance(key, int):
            self._registry.touch(key)
        return present

    def get(self, key, default=None):
        if dict.__contains__(self, key):
            value = dict.__getitem__(self, key)
            if isinstance(key, int):
                self._registry.touch(key)
            return value
        return default

    def pop(self, key, *default):
        existed = dict.__contains__(self, key)
        value = super().pop(key, *default)
        if existed and isinstance(key, int):
            self._registry.forget_if_unused(key)
        return value

    def setdefault(self, key, default=None):
        if not dict.__contains__(self, key):
            dict.__setitem__(self, key, default)
        if isinstance(key, int):
            self._registry.touch(key)
        return dict.__getitem__(self, key)

    def clear(self):
        user_ids = tuple(key for key in dict.keys(self) if isinstance(key, int))
        super().clear()
        for user_id in user_ids:
            self._registry.forget_if_unused(user_id)

    def discard_without_touch(self, user_id: int) -> None:
        dict.pop(self, user_id, None)


class TemporaryUserStateRegistry:
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._last_used: dict[int, float] = {}
        self._active: dict[int, int] = {}
        self._generations: dict[int, int] = {}
        self._generation_counter = 0
        self._mappings: list[TrackedUserStateDict] = []
        self._flow_mappings: list[TrackedUserStateDict] = []

    def create_mapping(self, *, flow_state: bool = True) -> TrackedUserStateDict:
        mapping = TrackedUserStateDict(self)
        self._mappings.append(mapping)
        if flow_state:
            self._flow_mappings.append(mapping)
        return mapping

    def touch(self, user_id: int, *, now: float | None = None) -> None:
        self._last_used[user_id] = self._clock() if now is None else now

    def forget_if_unused(self, user_id: int) -> None:
        if self._active.get(user_id, 0):
            return
        if any(dict.__contains__(mapping, user_id) for mapping in self._mappings):
            return
        if user_id not in self._generations:
            self._last_used.pop(user_id, None)

    def generation(self, user_id: int) -> int:
        return self._generations.get(user_id, 0)

    def begin_flow(self, user_id: int) -> int:
        self._generation_counter += 1
        generation = self._generation_counter
        self._generations[user_id] = generation
        self.touch(user_id)
        return generation

    def invalidate_user(self, user_id: int) -> int:
        generation = self.begin_flow(user_id)
        for mapping in self._flow_mappings:
            mapping.discard_without_touch(user_id)
        self.touch(user_id)
        return generation

    @contextmanager
    def activity(self, user_id: int):
        self.touch(user_id)
        self._active[user_id] = self._active.get(user_id, 0) + 1
        try:
            yield
        finally:
            remaining = self._active.get(user_id, 1) - 1
            if remaining > 0:
                self._active[user_id] = remaining
            else:
                self._active.pop(user_id, None)
            if (
                user_id in self._generations
                or any(dict.__contains__(mapping, user_id) for mapping in self._mappings)
            ):
                self.touch(user_id)
            else:
                self._last_used.pop(user_id, None)

    def clear_user(self, user_id: int, *, clear_generation: bool = True) -> None:
        for mapping in self._mappings:
            mapping.discard_without_touch(user_id)
        self._last_used.pop(user_id, None)
        if clear_generation:
            self._generations.pop(user_id, None)

    def cleanup_expired(self, ttl_seconds: float, *, now: float | None = None) -> int:
        current = self._clock() if now is None else now
        expired = [
            user_id
            for user_id, last_used in tuple(self._last_used.items())
            if current - last_used >= ttl_seconds and not self._active.get(user_id, 0)
        ]
        for user_id in expired:
            self.clear_user(user_id, clear_generation=True)
        return len(expired)

    def clear_all(self) -> None:
        for mapping in self._mappings:
            dict.clear(mapping)
        self._last_used.clear()
        self._active.clear()
        self._generations.clear()


temporary_user_state = TemporaryUserStateRegistry()
user_states = temporary_user_state.create_mapping()
search_builders = temporary_user_state.create_mapping()
pending_preset_queries = temporary_user_state.create_mapping()
pending_bulk_posts = temporary_user_state.create_mapping()


@dataclass
class DigestSubscriptionLockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    references: int = 0


DigestSubscriptionLockHandle = tuple[tuple[int, str], DigestSubscriptionLockEntry]
digest_subscription_locks: dict[
    tuple[int, str], DigestSubscriptionLockEntry
] = {}
pending_subscription_options = temporary_user_state.create_mapping()
# Глобальная задача для подписок
subscription_task = None
heartbeat_task = None
tag_translation_task = None
zip_export_manager: ZipExportManager | None = None
user_last_search_at = temporary_user_state.create_mapping(flow_state=False)
maintenance_task = None
cache_cleanup_last_deleted = 0
cache_cleanup_errors = 0
stale_flow_results_discarded = 0
duplicate_callbacks_rejected = 0
instance_lifecycle: BotInstanceLifecycle | None = None
instance_lock_wait_ms = 0
startup_orphans_deleted = 0

@dataclass
class OneShotCallbackEntry:
    owner_id: int
    generation: int
    created_at: float
    logical_action: str
    canonical_payload: str
    flow_scoped: bool = True
    status: str = "issued"


class StaleCallbackIssuer(RuntimeError):
    pass


class OneShotRegistryFull(RuntimeError):
    pass


ONE_SHOT_CALLBACK_MAX_PER_USER = 512
ONE_SHOT_CALLBACK_MAX_GLOBAL = 8192
ONE_SHOT_CALLBACK_TTL_SECONDS = 24 * 60 * 60
ONE_SHOT_CLEANUP_BATCH = 128
ONE_SHOT_PROCESS_EPOCH = secrets.token_hex(3)
issued_one_shot_callbacks: OrderedDict[str, OneShotCallbackEntry] = OrderedDict()


@asynccontextmanager
async def guarded_user_state(user_id: int):
    """Gate order is always temporary-state activity, then the user gate."""
    with temporary_user_state.activity(user_id):
        async with user_operation_gate.hold(user_id):
            yield
MEDIA_SEND_RETRIES = 2
SUBSCRIPTION_CONCURRENCY = 5
HEARTBEAT_INTERVAL_SECONDS = 5 * 60
FAVORITES_EXPORT_COOLDOWN_SECONDS = 5 * 60
POST_TAGS_PAGE_SIZE = 8
FAVORITES_GALLERY_PAGE_SIZE = 10
RESTART_EXIT_CODE = 42
restart_requested = False
RESTART_TEXT_COMMANDS = {"restart", "рестарт"}
upstream_failure_streak = 0
last_admin_alert_at = 0.0
ADMIN_ALERT_COOLDOWN_SECONDS = 15 * 60


async def note_upstream_failure(app, reason: str):
    global upstream_failure_streak, last_admin_alert_at
    upstream_failure_streak += 1
    runtime_metrics.increment("upstream_failures")
    now = time.monotonic()
    if (
        upstream_failure_streak < 5
        or now - last_admin_alert_at < ADMIN_ALERT_COOLDOWN_SECONDS
    ):
        return
    last_admin_alert_at = now
    text = (
        "🚨 Серия ошибок Rule34/сети: "
        f"{upstream_failure_streak} подряд. Последняя: {reason[:300]}"
    )
    for admin_id in ADMIN_USER_IDS:
        try:
            await send_text_to_chat(app.bot, admin_id, text=text)
        except Exception:
            logger.exception("Failed to notify admin %s about upstream outage", admin_id)


def reset_upstream_failure_streak():
    global upstream_failure_streak
    upstream_failure_streak = 0


async def remember_and_cache_post(post: dict):
    remember_post(post)
    await cache_post(post)


async def get_known_post(post_id: int) -> dict | None:
    return get_remembered_post(post_id) or await get_cached_post(post_id)


def is_rate_limited(user_id: int) -> bool:
    if SEARCH_COOLDOWN_SECONDS <= 0:
        return False

    now = time.monotonic()
    last_at = user_last_search_at.get(user_id, 0)
    if now - last_at < SEARCH_COOLDOWN_SECONDS:
        return True

    user_last_search_at[user_id] = now
    return False


async def reserve_search_cooldown(user_id: int) -> bool:
    """Atomically reserve the cooldown immediately before an external search."""
    async with guarded_user_state(user_id):
        return is_rate_limited(user_id)


async def begin_user_flow(
    user_id: int,
    state: str,
    *,
    expected_generation: int | None = None,
    **related_state,
) -> int | None:
    global stale_flow_results_discarded
    async with guarded_user_state(user_id):
        if (
            expected_generation is not None
            and temporary_user_state.generation(user_id) != expected_generation
        ):
            stale_flow_results_discarded += 1
            return None
        generation = temporary_user_state.begin_flow(user_id)
        user_states[user_id] = state
        for mapping, value in related_state.values():
            mapping[user_id] = value
        return generation


async def invalidate_user_flow(user_id: int) -> int:
    async with guarded_user_state(user_id):
        generation = temporary_user_state.invalidate_user(user_id)
        stale_user_one_shot_callbacks(user_id, generation)
        return generation


async def claim_user_message_state(user_id: int):
    """Consume one pending input state and snapshot its related mutable values."""
    async with guarded_user_state(user_id):
        state = user_states.pop(user_id, None)
        generation = temporary_user_state.generation(user_id)
        snapshot = {}
        if state == "waiting_builder_exclude":
            snapshot["builder"] = dict(search_builders.pop(user_id, {}))
        elif state == "waiting_preset_name":
            snapshot["preset_query"] = str(
                pending_preset_queries.pop(user_id, "")
            )
        elif state == "waiting_bulk_collection_name":
            snapshot["bulk_posts"] = tuple(pending_bulk_posts.pop(user_id, ()))
        elif state == "waiting_subscription_blacklist":
            snapshot["subscription_query"] = str(
                pending_subscription_options.pop(user_id, "")
            )
        return state, generation, snapshot


async def commit_flow_if_current(user_id: int, generation: int, commit) -> bool:
    global stale_flow_results_discarded
    async with guarded_user_state(user_id):
        if temporary_user_state.generation(user_id) != generation:
            stale_flow_results_discarded += 1
            return False
        commit()
        return True


ONE_SHOT_CALLBACK_PREFIXES = (
    "act_",
    "sub_create_",
    "sub_toggle_",
    "sub_remove_do_",
    "sub_post_del_",
    "fav_del_",
    "fav_remove_do_",
    "fav_col_pick_",
    "fav_note_",
    "preset_del_",
    "col_delete_do_",
    "col_remove_",
    "col_add_",
    "later_del_",
    "gallery_bulk_fav_",
    "gallery_collection_",
    "gallery_col_add_",
    "gallery_col_add:",
    "gallery_col_new:",
    "storage_cleanup_90_do",
    "storage_empty_collections_do",
    "bl_preset_add_",
    "bl_preset_del_",
    "rec_hide_",
    "tag_block_",
    "later_add_",
    "sub_fav_",
)
ONE_SHOT_CALLBACK_EXACT: set[str] = {
    "stats_clear_do",
    "settings_reset_do",
    "sub_digest_send",
    "settings_resume_subscriptions",
}


def is_one_shot_callback(data: str) -> bool:
    if data in ONE_SHOT_CALLBACK_EXACT or data.startswith(ONE_SHOT_CALLBACK_PREFIXES):
        return True
    return bool(re.fullmatch(r"fav_\d+", data))


def register_one_shot_callback(
    user_id: int,
    data: str,
    *,
    logical_action: str,
    canonical_payload: str,
    expected_generation: int | None = None,
    flow_scoped: bool = True,
) -> str:
    generation = temporary_user_state.generation(user_id)
    if (
        flow_scoped
        and expected_generation is not None
        and generation != expected_generation
    ):
        raise StaleCallbackIssuer("Callback flow generation is no longer current")
    cleanup_one_shot_callbacks(max_removals=ONE_SHOT_CLEANUP_BATCH)
    _make_one_shot_capacity(int(user_id))
    issued_one_shot_callbacks[data] = OneShotCallbackEntry(
        owner_id=int(user_id),
        generation=generation,
        created_at=time.monotonic(),
        logical_action=logical_action,
        canonical_payload=canonical_payload,
        flow_scoped=flow_scoped,
    )
    issued_one_shot_callbacks.move_to_end(data)
    return data


def _one_shot_user_count(user_id: int) -> int:
    return sum(
        entry.owner_id == user_id for entry in issued_one_shot_callbacks.values()
    )


def _evict_one_shot_candidate(*, user_id: int | None = None) -> bool:
    entries = tuple(issued_one_shot_callbacks.items())
    for terminal_only in (True, False):
        for data, entry in entries:
            if user_id is not None and entry.owner_id != user_id:
                continue
            if entry.status == "processing":
                continue
            if terminal_only and entry.status not in {"consumed", "stale"}:
                continue
            issued_one_shot_callbacks.pop(data, None)
            return True
    return False


def _make_one_shot_capacity(user_id: int) -> None:
    while _one_shot_user_count(user_id) >= ONE_SHOT_CALLBACK_MAX_PER_USER:
        if not _evict_one_shot_candidate(user_id=user_id):
            raise OneShotRegistryFull("User callback registry is full of processing entries")
    while len(issued_one_shot_callbacks) >= ONE_SHOT_CALLBACK_MAX_GLOBAL:
        if not _evict_one_shot_candidate():
            raise OneShotRegistryFull("Global callback registry is full of processing entries")


def cleanup_one_shot_callbacks(
    *, now: float | None = None, max_removals: int = ONE_SHOT_CLEANUP_BATCH
) -> int:
    current = time.monotonic() if now is None else now
    removed = 0
    for data, entry in tuple(issued_one_shot_callbacks.items()):
        if removed >= max_removals:
            break
        if (
            entry.status != "processing"
            and current - entry.created_at >= ONE_SHOT_CALLBACK_TTL_SECONDS
        ):
            issued_one_shot_callbacks.pop(data, None)
            removed += 1
    return removed


def ensure_user_generation(user_id: int) -> int:
    generation = temporary_user_state.generation(user_id)
    if generation == 0:
        generation = temporary_user_state.begin_flow(user_id)
    return generation


@dataclass(frozen=True)
class OneShotCallbackIssuer:
    user_id: int
    expected_generation: int
    flow_scoped: bool = True

    def _check_current(self) -> None:
        if (
            self.flow_scoped
            and temporary_user_state.generation(self.user_id)
            != self.expected_generation
        ):
            raise StaleCallbackIssuer("Callback issuer belongs to a stale flow")

    def _token_prefix(self) -> str:
        material = (
            f"{ONE_SHOT_PROCESS_EPOCH}:{self.user_id}:"
            f"{self.expected_generation if self.flow_scoped else 'durable'}"
        )
        digest = hashlib.blake2s(
            material.encode("ascii"), digest_size=5
        ).hexdigest()
        return f"p{digest}"

    def payload(self, action: str, payload: str) -> str:
        self._check_current()
        data = store_callback_payload(
            action,
            payload,
            one_shot=True,
            token_prefix=self._token_prefix(),
        )
        return register_one_shot_callback(
            self.user_id,
            data,
            logical_action=action,
            canonical_payload=payload,
            expected_generation=self.expected_generation,
            flow_scoped=self.flow_scoped,
        )

    def side_effect(self, data: str) -> str:
        self._check_current()
        token = store_callback_payload(
            "act",
            data,
            one_shot=True,
            token_prefix=self._token_prefix(),
        )
        return register_one_shot_callback(
            self.user_id,
            token,
            logical_action="side_effect",
            canonical_payload=data,
            expected_generation=self.expected_generation,
            flow_scoped=self.flow_scoped,
        )

    def __call__(self, data: str) -> str:
        return self.side_effect(data)


def callback_issuer_for(
    user_id: int,
    expected_generation: int | None = None,
    *,
    flow_scoped: bool = True,
) -> OneShotCallbackIssuer:
    if expected_generation is not None:
        generation = expected_generation
    elif flow_scoped:
        generation = ensure_user_generation(user_id)
    else:
        generation = temporary_user_state.generation(user_id)
    if flow_scoped and temporary_user_state.generation(user_id) != generation:
        raise StaleCallbackIssuer("Cannot create issuer for a stale generation")
    return OneShotCallbackIssuer(int(user_id), generation, flow_scoped)


def store_user_one_shot_payload(action: str, payload: str, user_id: int) -> str:
    return callback_issuer_for(user_id).payload(action, payload)


def store_side_effect_callback(data: str, user_id: int) -> str:
    return callback_issuer_for(user_id).side_effect(data)


def side_effect_callback_for(
    user_id: int, expected_generation: int | None = None
) -> OneShotCallbackIssuer:
    return callback_issuer_for(user_id, expected_generation)


def subscription_callback_issuer_for(user_id: int) -> OneShotCallbackIssuer:
    """Issue notification actions that survive unrelated interactive flows."""
    return callback_issuer_for(user_id, flow_scoped=False)


def resolved_callback_data(data: str) -> str:
    entry = issued_one_shot_callbacks.get(data)
    if entry and entry.logical_action == "side_effect":
        return entry.canonical_payload
    return data


def revoke_unsent_keyboard_callbacks(keyboard) -> None:
    """Drop issued one-shot tokens belonging to a keyboard never delivered."""
    for row in getattr(keyboard, "inline_keyboard", ()) or ():
        for button in row:
            data = getattr(button, "callback_data", None)
            entry = issued_one_shot_callbacks.get(data) if data else None
            if entry is not None and entry.status == "issued":
                issued_one_shot_callbacks.pop(data, None)


def stale_user_one_shot_callbacks(user_id: int, current_generation: int) -> None:
    for entry in issued_one_shot_callbacks.values():
        if (
            entry.owner_id == user_id
            and entry.flow_scoped
            and entry.generation != current_generation
            and entry.status in {"issued", "reserved"}
        ):
            entry.status = "stale"


async def reserve_one_shot_callback_result(user_id: int, data: str) -> str:
    global duplicate_callbacks_rejected
    if not is_one_shot_callback(data):
        return "accepted"
    async with guarded_user_state(user_id):
        cleanup_one_shot_callbacks(max_removals=ONE_SHOT_CLEANUP_BATCH)
        generation = temporary_user_state.generation(user_id)
        issued = issued_one_shot_callbacks.get(data)
        if issued is None or issued.owner_id != user_id:
            duplicate_callbacks_rejected += 1
            return "stale"
        if issued.flow_scoped and issued.generation != generation:
            duplicate_callbacks_rejected += 1
            return "stale"
        if issued.status != "issued":
            duplicate_callbacks_rejected += 1
            return "duplicate" if issued.status in {
                "reserved", "processing", "consumed"
            } else "stale"

        logical_key = (
            issued.owner_id,
            issued.generation,
            issued.flow_scoped,
            issued.logical_action,
            issued.canonical_payload,
        )
        for candidate in issued_one_shot_callbacks.values():
            candidate_key = (
                candidate.owner_id,
                candidate.generation,
                candidate.flow_scoped,
                candidate.logical_action,
                candidate.canonical_payload,
            )
            if candidate_key != logical_key or candidate is issued:
                continue
            if candidate.status in {"reserved", "processing"}:
                issued.status = "stale"
                duplicate_callbacks_rejected += 1
                return "duplicate"
        issued.status = "reserved"
        for candidate in issued_one_shot_callbacks.values():
            candidate_key = (
                candidate.owner_id,
                candidate.generation,
                candidate.flow_scoped,
                candidate.logical_action,
                candidate.canonical_payload,
            )
            if candidate is not issued and candidate_key == logical_key:
                if candidate.status == "issued":
                    candidate.status = "stale"
        return "accepted"


async def reserve_one_shot_callback(user_id: int, data: str) -> bool:
    return await reserve_one_shot_callback_result(user_id, data) == "accepted"


async def begin_one_shot_processing(user_id: int, data: str) -> bool:
    if not is_one_shot_callback(data):
        return True
    async with guarded_user_state(user_id):
        entry = issued_one_shot_callbacks.get(data)
        if (
            entry is None
            or entry.owner_id != user_id
            or (
                entry.flow_scoped
                and entry.generation != temporary_user_state.generation(user_id)
            )
            or entry.status != "reserved"
        ):
            return False
        entry.status = "processing"
        return True


async def finish_one_shot_processing(user_id: int, data: str) -> None:
    if not is_one_shot_callback(data):
        return
    async with guarded_user_state(user_id):
        entry = issued_one_shot_callbacks.get(data)
        if entry is not None and entry.owner_id == user_id and entry.status == "processing":
            entry.status = "consumed"


async def consume_one_shot_callback(user_id: int, data: str) -> bool:
    """Compatibility helper used by focused tests: reserve and start processing."""
    if not await reserve_one_shot_callback(user_id, data):
        return False
    return await begin_one_shot_processing(user_id, data)


async def build_main_menu_text(user_id: int) -> str:
    pause_until = await get_subscription_pause_until(user_id)
    remaining = format_remaining_pause(pause_until)
    if remaining:
        return (
            "☰ Главное меню\n\nВыберите нужный раздел.\n\n"
            f"⏸ Подписки приостановлены, осталось: {remaining}."
        )
    return "☰ Главное меню\n\nВыберите нужный раздел."


async def build_subscription_added_text(query: str, interval: int, user_id: int) -> str:
    text = (
        f"✅ Подписка на `{md_code(query)}` активирована!\n\n"
        f"Вы будете получать новые посты каждые {interval} минут."
    )
    remaining = format_remaining_pause(await get_subscription_pause_until(user_id))
    if remaining:
        text += (
            "\n\n⏸ Сейчас подписки приостановлены, осталось: "
            f"{remaining}. Эта подписка тоже начнёт работать после паузы."
        )
    return text


def get_subscription_preview(
    query: str,
    interval: int,
    user_id: int,
    *,
    issuer: OneShotCallbackIssuer,
) -> tuple[str, InlineKeyboardMarkup]:
    payload = json.dumps({"query": query, "interval": interval}, ensure_ascii=False)
    confirm_callback = issuer.payload("sub_create", payload)
    text = (
        "Главная → Подписки → Новая подписка\n\n"
        f"Запрос: `{md_code(query)}`\n"
        f"Интервал: `{interval}` мин.\n"
        "Фильтры: общие настройки\n"
        "Доставка: сразу после появления нового поста\n\n"
        "Создать подписку?"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Создать", callback_data=confirm_callback)],
        [
            InlineKeyboardButton("✏️ Изменить", callback_data="sub_add_new"),
            InlineKeyboardButton("❌ Отмена", callback_data="subscriptions"),
        ],
    ])
    return text, keyboard


async def get_user_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    settings = normalize_feature_settings(await get_user_settings(user_id))
    return get_settings_keyboard(settings)


async def get_user_main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    settings = normalize_feature_settings(await get_user_settings(user_id))
    return get_main_keyboard(settings.get("interface_mode", "simple"))


async def get_user_persistent_keyboard(user_id: int):
    settings = normalize_feature_settings(await get_user_settings(user_id))
    return get_persistent_keyboard(settings.get("interface_mode", "simple"))


async def get_user_subscriptions_keyboard(user_id: int) -> InlineKeyboardMarkup:
    pause_until = await get_subscription_pause_until(user_id)
    digest_count = await count_subscription_digest(user_id)
    return get_subscriptions_keyboard(
        subscriptions_paused=bool(pause_until),
        has_digest_posts=digest_count > 0,
        side_effect_callback=subscription_callback_issuer_for(user_id),
    )


async def build_subscriptions_menu_text(user_id: int) -> str:
    remaining = format_remaining_pause(await get_subscription_pause_until(user_id))
    total_count, active_count = await get_subscription_usage(user_id)
    status = (
        f"⏸ Сейчас приостановлены, осталось: {remaining}."
        if remaining
        else "✅ Сейчас работают по расписанию."
    )
    return (
        "🔔 *Подписки*\n\n"
        "Бот автоматически пришлёт новые посты по сохранённым запросам.\n\n"
        f"Подписки: {total_count} из {SUBSCRIPTION_MAX_TOTAL}.\n"
        f"Активные: {active_count} из {SUBSCRIPTION_MAX_ACTIVE}.\n\n"
        f"{status}"
    )


def should_spoiler(settings: dict | None, post: dict) -> bool:
    mode = (settings or {}).get("spoiler_mode", "off")
    return mode == "all" or (mode == "explicit" and post.get("rating") == "e")


def media_from_post(post: dict, caption: str = "", has_spoiler: bool = False):
    candidates = get_media_url_candidates(post)
    file_url = candidates[0][1] if candidates else ""
    media_caption = caption if caption else None
    url_path = media_url_path_lower(file_url)
    if url_path.endswith((".mp4", ".webm")):
        return InputMediaVideo(
            file_url, caption=media_caption, parse_mode="Markdown", has_spoiler=has_spoiler
        )
    if url_path.endswith(".gif"):
        return InputMediaAnimation(
            file_url, caption=media_caption, parse_mode="Markdown", has_spoiler=has_spoiler
        )
    return InputMediaPhoto(
        file_url, caption=media_caption, parse_mode="Markdown", has_spoiler=has_spoiler
    )


def gallery_failed_item_index(error: Exception, item_count: int) -> int | None:
    """Return the zero-based media index reported by Telegram, if available."""
    match = re.search(r"(?:message|item)\s*#(\d+)", str(error), re.IGNORECASE)
    if not match:
        return None
    index = int(match.group(1)) - 1
    return index if 0 <= index < item_count else None


def gallery_fallback_post(post: dict) -> dict | None:
    """Switch a failed album item to another Telegram-compatible static URL."""
    current_url = post.get("file_url") or ""
    fallback_url = next(
        (
            post.get(key) or ""
            for key in ("sample_url", "preview_url")
            if post.get(key)
            and post.get(key) != current_url
            and media_group_compatible_url(post.get(key))
        ),
        "",
    )
    if not fallback_url:
        return None
    return dict(post, file_url=fallback_url)


async def send_resilient_media_group(
    message,
    posts: list[dict],
    settings: dict,
    caption: str,
    log_context: str = "Gallery",
) -> tuple[list[dict], list[dict]]:
    """Send an album, repairing or removing only the item rejected by Telegram.

    The first list contains posts delivered as one album. The second contains
    rejected posts that may still be attempted sequentially. When album delivery
    is impossible, the first list is empty and all remaining posts are returned
    in the second list.
    """
    album_posts = list(posts)
    rejected_posts = []
    fallback_post_ids = set()
    max_album_attempts = min(5, len(album_posts) + 1)
    for attempt in range(1, max_album_attempts + 1):
        media = [
            media_from_post(
                post,
                caption if index == 0 else "",
                should_spoiler(settings, post),
            )
            for index, post in enumerate(album_posts)
        ]
        try:
            await execute_telegram_request(
                lambda: message.reply_media_group(media=media),
                operation_name="reply_media_group",
                chat_id=int(getattr(getattr(message, "chat", None), "id", 0) or 0),
            )
            return album_posts, rejected_posts
        except (TimedOut, RetryAfter):
            # The album may already have been accepted; sequential retry could duplicate it.
            raise
        except Exception as exc:
            if isinstance(exc, NetworkError) and not isinstance(exc, BadRequest):
                # Transport failures are ambiguous: Telegram may have accepted
                # the whole album, so a fallback send could duplicate it.
                raise
            failed_index = gallery_failed_item_index(exc, len(album_posts))
            if failed_index is None:
                logger.warning(
                    "%s album failed without item index; using sequential "
                    "fallback: %s",
                    log_context,
                    exc,
                )
                break

            failed_post = album_posts[failed_index]
            failed_post_id = int(failed_post.get("id") or 0)
            replacement = (
                gallery_fallback_post(failed_post)
                if failed_post_id not in fallback_post_ids
                else None
            )
            if replacement is not None:
                fallback_post_ids.add(failed_post_id)
                album_posts[failed_index] = replacement
                logger.warning(
                    "%s album item failed; retrying with fallback URL "
                    "attempt=%s/%s item=%s post=%s error=%s",
                    log_context,
                    attempt,
                    max_album_attempts,
                    failed_index + 1,
                    failed_post_id,
                    exc,
                )
                continue

            removed = album_posts.pop(failed_index)
            rejected_posts.append(removed)
            logger.warning(
                "%s album item failed without usable fallback; removing "
                "item=%s post=%s remaining=%s error=%s",
                log_context,
                failed_index + 1,
                removed.get("id"),
                len(album_posts),
                exc,
            )
            if len(album_posts) < 2:
                break

    return [], album_posts + rejected_posts


def should_show_tags_button(settings: dict | None = None) -> bool:
    if settings is None:
        return True
    return bool(settings.get("show_tags_button", True))


CAPTION_SETTING_ELEMENTS = [
    ("show_search_query", "Запрос поиска"),
    ("show_subscription_label", "Метка подписки"),
    ("show_id", "ID поста"),
    ("show_score", "Очки (score)"),
    ("show_rating", "Рейтинг"),
    ("show_tags", "Теги"),
    ("show_tags_button", "Кнопка всех тегов"),
]

DEFAULT_CAPTION_SETTINGS = {
    "show_caption": True,
    "show_search_query": True,
    "show_subscription_label": True,
    "show_id": True,
    "show_score": True,
    "show_rating": True,
    "show_tags": True,
    "show_tags_button": True,
}


async def mutate_user_settings(user_id: int, mutator) -> dict:
    """Serialize a dependent settings mutation and persist only changed fields."""
    async with guarded_user_state(user_id):
        settings = normalize_feature_settings(await get_user_settings(user_id))
        patch = dict(mutator(dict(settings)) or {})
        if patch:
            await save_user_settings(user_id, patch)
            settings.update(patch)
        return normalize_feature_settings(settings)


def build_caption_settings_text(settings: dict) -> str:
    text = "📝 *Настройки описания картинок*\n\n"

    if not settings.get("show_caption", True):
        return (
            text
            + "❌ Описание *полностью отключено*\n\n"
            "Нажмите '✅ Показывать описание' чтобы включить"
        )

    text += "✅ Описание *включено*\n\n"
    enabled = []
    disabled = []
    for setting_key, element_name in CAPTION_SETTING_ELEMENTS:
        if settings.get(setting_key, True):
            enabled.append(f"✅ {element_name}")
        else:
            disabled.append(f"❌ {element_name}")

    if enabled:
        text += "*Включено:*\n" + "\n".join(enabled) + "\n\n"
    if disabled:
        text += "*Выключено:*\n" + "\n".join(disabled)
    return text


async def send_full_post_tags(
    message,
    post_id: int,
    user_id: int,
    issuer: OneShotCallbackIssuer,
    page: int = 0,
    edit: bool = False,
):
    post = await get_known_post(post_id) or minimal_post(post_id)
    all_tags = [tag for tag in str(post.get("tags") or "").split() if tag]
    total_pages = max(1, (len(all_tags) + POST_TAGS_PAGE_SIZE - 1) // POST_TAGS_PAGE_SIZE)
    page = max(0, min(int(page), total_pages - 1))
    start = page * POST_TAGS_PAGE_SIZE
    page_tags = all_tags[start:start + POST_TAGS_PAGE_SIZE]
    translations = await tag_translation_service.translate_tags(page_tags)
    page_post = dict(post, tags=" ".join(page_tags))
    text = build_full_tags_messages(page_post, translations)[0]
    if all_tags:
        text += f"\n\nСтраница {page + 1}/{total_pages} · тегов: {len(all_tags)}"

    rows = []
    for tag in page_tags:
        rows.append([
            InlineKeyboardButton(
                f"🔍 {tag[:28]}",
                callback_data=store_callback_payload("tag_search", tag),
            ),
            InlineKeyboardButton(
                "🚫 В чёрный список",
                callback_data=issuer.payload("tag_block", tag),
            ),
        ])
    if total_pages > 1:
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(
                "◀️", callback_data=f"post_tags_page_{post_id}_{page - 1}"
            ))
        navigation.append(InlineKeyboardButton(
            f"{page + 1}/{total_pages}", callback_data="post_tags_noop"
        ))
        if page + 1 < total_pages:
            navigation.append(InlineKeyboardButton(
                "▶️", callback_data=f"post_tags_page_{post_id}_{page + 1}"
            ))
        rows.append(navigation)
    keyboard = InlineKeyboardMarkup(rows) if rows else None

    send = message.edit_text if edit else message.reply_text
    await send(text, parse_mode="Markdown", reply_markup=keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user_id = update.effective_user.id
    await invalidate_user_flow(user_id)
    pause_until = await get_subscription_pause_until(user_id)
    remaining = format_remaining_pause(pause_until)
    pause_text = (
        f"\n\n⏸ Подписки приостановлены, осталось: {remaining}."
        if remaining
        else ""
    )
    await update.message.reply_text(
        "👋 *Добро пожаловать!*\n\n"
        "Я помогу найти изображения, собрать библиотеку и настроить автоматические подписки.\n\n"
        "⚠️ Только для пользователей 18+."
        f"{pause_text}",
        reply_markup=await get_user_persistent_keyboard(user_id),
        parse_mode="Markdown",
    )
    await update.message.reply_text(
        "С чего хотите начать?",
        reply_markup=get_onboarding_keyboard(),
    )


async def safe_query_answer(query, text: str | None = None):
    await execute_telegram_request(
        lambda: query.answer(text=text),
        operation_name="answer_callback_query",
        chat_id=int(getattr(getattr(query, "from_user", None), "id", 0) or 0),
        safe_to_retry_timeout=True,
        ambiguous_bad_request_policy="answer_callback_query",
    )


def is_recipient_allowed(
    user_id: int | None,
    chat_id: int | None = None,
    chat_type: str | None = "private",
) -> bool:
    """Apply the incoming access policy to a prospective delivery recipient."""
    if user_id in ADMIN_USER_IDS:
        return True
    if chat_id in ALLOWED_CHAT_IDS:
        return True
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return False
    if chat_type in {"group", "supergroup", "channel"}:
        return ALLOW_GROUP_CHATS
    return True


def is_access_allowed(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return is_recipient_allowed(
        user.id if user else None,
        chat.id if chat else None,
        getattr(chat, "type", None),
    )


async def send_access_denied(update: Update):
    text = "Доступ к боту ограничен."
    if update.callback_query:
        query = update.callback_query
        await execute_telegram_request(
            lambda: query.answer(text, show_alert=True),
            operation_name="answer_callback_query",
            chat_id=int(getattr(getattr(query, "from_user", None), "id", 0) or 0),
            safe_to_retry_timeout=True,
            ambiguous_bad_request_policy="answer_callback_query",
        )
        return
    if update.effective_message:
        await update.effective_message.reply_text(text)


def require_access(handler):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_access_allowed(update):
            user_id = update.effective_user.id if update.effective_user else None
            chat_id = update.effective_chat.id if update.effective_chat else None
            logger.warning("Access denied user=%s chat=%s", user_id, chat_id)
            await send_access_denied(update)
            return
        user = getattr(update, "effective_user", None)
        user_id = getattr(user, "id", None)
        if not isinstance(user_id, int):
            return await handler(update, context)
        with temporary_user_state.activity(user_id):
            return await handler(update, context)

    return wrapped


def schedule_background_task(context: ContextTypes.DEFAULT_TYPE, coroutine):
    application = getattr(context, "application", None)
    if application and hasattr(application, "create_task"):
        application.create_task(coroutine)
    else:
        asyncio.create_task(coroutine)


def finalize_one_shot_handler(handler):
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global stale_flow_results_discarded
        query = getattr(update, "callback_query", None)
        raw_data = getattr(query, "data", "")
        user_id = int(getattr(getattr(query, "from_user", None), "id", 0) or 0)
        try:
            return await handler(update, context)
        except StaleCallbackIssuer:
            stale_flow_results_discarded += 1
            return None
        finally:
            if user_id and raw_data:
                await finish_one_shot_processing(user_id, raw_data)

    return wrapped


@finalize_one_shot_handler
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий кнопок"""
    global stale_flow_results_discarded
    query = update.callback_query
    user_id = query.from_user.id
    raw_data = query.data
    data = resolved_callback_data(raw_data)
    deferred_answer = data.startswith((
        "later_add_",
        "tag_block_",
        "bl_quick_",
        "gallery_bulk_fav_",
    ))
    reservation = await reserve_one_shot_callback_result(user_id, raw_data)
    if reservation != "accepted":
        already_processed = reservation == "duplicate"
        if deferred_answer:
            await safe_query_answer(
                query,
                "Кнопка уже обработана" if already_processed else "Кнопка устарела",
            )
        else:
            await safe_query_answer(query)
            await query.message.reply_text(
                "Эта кнопка уже была обработана."
                if already_processed
                else "Эта кнопка устарела."
            )
        return
    if not await begin_one_shot_processing(user_id, raw_data):
        if deferred_answer:
            await safe_query_answer(query, "Кнопка устарела")
        else:
            await safe_query_answer(query)
            await query.message.reply_text("Эта кнопка устарела.")
        return
    reserved_entry = issued_one_shot_callbacks.get(raw_data)
    callback_flow_scoped = (
        reserved_entry.flow_scoped if reserved_entry is not None else True
    )
    async with guarded_user_state(user_id):
        callback_generation = temporary_user_state.generation(user_id)
        if callback_generation == 0:
            callback_generation = temporary_user_state.begin_flow(user_id)
    callback_issuer = side_effect_callback_for(user_id, callback_generation)

    async def begin_callback_flow(state: str, **related_state):
        return await begin_user_flow(
            user_id,
            state,
            expected_generation=callback_generation,
            **related_state,
        )
    if not deferred_answer:
        await safe_query_answer(query)

    async with guarded_user_state(user_id):
        callback_is_current = (
            temporary_user_state.generation(user_id) == callback_generation
        )
    if callback_flow_scoped and not callback_is_current:
        stale_flow_results_discarded += 1
        if deferred_answer:
            await safe_query_answer(query, "Кнопка устарела")
        else:
            await query.message.reply_text("Эта кнопка устарела.")
        return

    data = resolved_callback_data(raw_data)

    if data == "cancel_input":
        await invalidate_user_flow(user_id)
        await query.edit_message_text(
            "Действие отменено.\n\n" + await build_main_menu_text(user_id),
            reply_markup=await get_user_main_keyboard(user_id),
        )

    elif data.startswith("context_help_"):
        section = data.replace("context_help_", "", 1)
        help_texts = {
            "start": (
                "📖 *Как пользоваться*\n\n"
                "1. Откройте поиск и отправьте теги через пробел.\n"
                "2. Сохраняйте понравившиеся посты в библиотеку.\n"
                "3. Создайте подписку, чтобы получать новые посты автоматически."
            ),
            "search": (
                "Главная → Поиск → Помощь\n\n"
                "Обычный поиск находит один пост, подборка формирует альбом, "
                "а конструктор помогает собрать запрос с исключениями."
            ),
            "library": (
                "Главная → Библиотека → Помощь\n\n"
                "Избранное можно распределять по коллекциям, снабжать заметками "
                "и сохранять в список «На потом»."
            ),
            "subscriptions": (
                "Главная → Подписки → Помощь\n\n"
                "Подписка периодически проверяет сохранённый запрос. Её можно "
                "приостановить отдельно или временно остановить все подписки."
            ),
            "blacklist": (
                "Главная → Чёрный список → Помощь\n\n"
                "Добавленные теги исключаются из поиска, подборок и случайных постов. "
                "Временные теги удаляются автоматически после истечения срока."
            ),
            "settings": (
                "Главная → Настройки → Помощь\n\n"
                "Здесь настраиваются подписи, спойлеры, размер подборок, качество "
                "медиа и сложность интерфейса."
            ),
        }
        help_back_callbacks = {
            "search": "search_hub",
            "library": "library",
            "subscriptions": "subscriptions",
            "blacklist": "blacklist",
            "settings": "settings",
        }
        await query.edit_message_text(
            help_texts.get(section, "ℹ️ Справка для этого раздела недоступна."),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data=help_back_callbacks.get(section, "back"),
                )]
            ]),
            parse_mode="Markdown",
        )

    elif data == "my_data":
        await query.edit_message_text(
            "Главная → Мои данные\n\n"
            "Здесь находятся статистика, сведения о хранилище и экспорт данных.",
            reply_markup=get_data_keyboard(),
        )

    elif data == "search":
        if await begin_callback_flow("waiting_search") is None:
            return
        await query.edit_message_text(
            "🔍 Введите теги для поиска (через пробел):\n\n"
            "Примеры:\n"
            "• `anime girl`\n"
            "• `2girls blonde_hair`\n"
            "• `solo male`\n\n"
            "💡 Используй `_` для тегов из нескольких слов",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("search_hub"),
        )

    elif data == "search_hub":
        await query.edit_message_text(
            "Главная → Поиск\n\nВыберите способ поиска.",
            reply_markup=get_search_hub_keyboard(),
            parse_mode="Markdown",
        )

    elif data == "library":
        total = await count_favorites(user_id)
        later_count = (await get_user_storage_stats(user_id)).get("read_later", 0)
        await query.edit_message_text(
            f"Главная → Библиотека\n\nИзбранное: `{total}`\nНа потом: `{later_count}`",
            reply_markup=get_library_keyboard(),
            parse_mode="Markdown",
        )

    elif data == "random":
        schedule_background_task(
            context,
            send_random_image(
                query.message,
                user_id,
                expected_generation=callback_generation,
            ),
        )

    elif data == "more":
        saved = await get_user_query(user_id)
        if saved and saved[0]:
            schedule_background_task(
                context,
                send_image(
                    query.message,
                    user_id,
                    saved[0],
                    edit=False,
                    is_more=True,
                    expected_generation=callback_generation,
                ),
            )
        else:
            await query.message.reply_text(
                "❌ Сначала выполните поиск!", reply_markup=get_main_keyboard()
            )

    elif data.startswith("post_more_"):
        post_id_text = data.replace("post_more_", "", 1)
        if post_id_text.isdigit():
            settings = await get_user_settings(user_id)
            await query.edit_message_reply_markup(
                reply_markup=get_post_more_keyboard(
                    int(post_id_text), should_show_tags_button(settings)
                )
            )

    elif data.startswith("post_compact_"):
        post_id_text = data.replace("post_compact_", "", 1)
        if post_id_text.isdigit():
            settings = await get_user_settings(user_id)
            await query.edit_message_reply_markup(
                reply_markup=get_image_keyboard(
                    int(post_id_text),
                    show_tags_button=should_show_tags_button(settings),
                    side_effect_callback=callback_issuer,
                )
            )
    elif data == "blacklist":
        await query.edit_message_text(
            "Главная → Чёрный список\n\n"
            "Теги из этого списка исключаются из результатов поиска.",
            reply_markup=get_blacklist_keyboard(),
            parse_mode="Markdown",
        )

    elif data == "subscriptions":
        await query.edit_message_text(
            await build_subscriptions_menu_text(user_id),
            reply_markup=await get_user_subscriptions_keyboard(user_id),
            parse_mode="Markdown",
        )

    elif data == "history":
        await show_history(query.message, user_id, edit=True)

    elif data == "favorites":
        await show_favorites(query.message, user_id, edit=True)

    elif data == "gallery":
        if await begin_callback_flow("waiting_gallery") is None:
            return
        await query.edit_message_text(
            "🖼 Введите теги для галереи. Для случайной подборки отправьте `random`.",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("search_hub"),
        )

    elif data.startswith("gallery_next_"):
        payload = get_callback_payload("gallery_next", data)
        try:
            params = json.loads(payload or "{}")
            page = int(params.get("page", 0))
            tags = str(params.get("tags", ""))
        except (ValueError, TypeError, json.JSONDecodeError):
            await query.message.reply_text("❌ Подборка устарела. Запустите галерею заново.")
            return
        schedule_background_task(
            context,
            send_search_gallery(
                query.message,
                user_id,
                tags,
                page,
                expected_generation=callback_generation,
            ),
        )

    elif data == "search_builder":
        if await begin_callback_flow(
            "waiting_builder_include",
            builder=(search_builders, {}),
        ) is None:
            return
        await query.message.reply_text(
            "🧩 *Конструктор поиска*\n\nВведите обязательные теги через пробел.",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("search_hub"),
        )

    elif data == "presets":
        await show_search_presets(query.message, user_id, issuer=callback_issuer)

    elif data == "preset_save_current":
        saved = await get_user_query(user_id)
        if not saved or not saved[0]:
            await query.message.reply_text("Сначала выполните поиск.")
        else:
            if await begin_callback_flow(
                "waiting_preset_name",
                preset=(pending_preset_queries, saved[0]),
            ) is None:
                return
            await query.message.reply_text(
                "Введите название сохранённого запроса (до 40 символов):",
                reply_markup=get_cancel_keyboard("search_hub"),
            )

    elif data.startswith("preset_run_"):
        value = data.replace("preset_run_", "", 1)
        preset = await get_search_preset(user_id, int(value)) if value.isdigit() else None
        if not preset:
            await query.message.reply_text("Сохранённый запрос не найден.")
        else:
            await save_user_settings(user_id, preset["settings"])
            schedule_background_task(
                context,
                send_search_gallery(
                    query.message,
                    user_id,
                    preset["query"],
                    expected_generation=callback_generation,
                ),
            )

    elif data.startswith("preset_del_"):
        value = data.replace("preset_del_", "", 1)
        if value.isdigit():
            await delete_search_preset(user_id, int(value))
        await show_search_presets(query.message, user_id, issuer=callback_issuer)

    elif data.startswith("preset_from_"):
        preset_query = get_callback_payload("preset_from", data)
        if preset_query:
            if await begin_callback_flow(
                "waiting_preset_name",
                preset=(pending_preset_queries, preset_query),
            ) is None:
                return
            await query.message.reply_text(
                "Введите название сохранённого запроса:",
                reply_markup=get_cancel_keyboard("search_hub"),
            )

    elif data.startswith("builder_run_"):
        built_query = get_callback_payload("builder_run", data)
        if built_query:
            schedule_background_task(
                context,
                send_search_gallery(
                    query.message,
                    user_id,
                    built_query,
                    expected_generation=callback_generation,
                ),
            )

    elif data == "recommendations":
        schedule_background_task(
            context,
            send_recommendations(
                query.message,
                user_id,
                expected_generation=callback_generation,
            ),
        )

    elif data.startswith("rec_hide_"):
        tag = get_callback_payload("rec_hide", data)
        if tag:
            def exclude_recommendation_tag(settings):
                excluded = set(
                    str(settings.get("recommendation_excluded_tags", "")).split()
                )
                excluded.add(tag)
                return {
                    "recommendation_excluded_tags": " ".join(
                        sorted(excluded)[:100]
                    )
                }

            await mutate_user_settings(user_id, exclude_recommendation_tag)
            await query.message.reply_text(f"🚫 `{md_code(tag)}` исключён из рекомендаций.", parse_mode="Markdown")

    elif data.startswith("similar_"):
        value = data.replace("similar_", "", 1)
        post = await get_known_post(int(value)) if value.isdigit() else None
        if not post:
            await query.message.reply_text("Не удалось получить теги поста.")
        else:
            similar_tags = similar_query_from_post(post)
            if similar_tags:
                schedule_background_task(
                    context,
                    send_search_gallery(
                        query.message,
                        user_id,
                        similar_tags,
                        expected_generation=callback_generation,
                    ),
                )
            else:
                await query.message.reply_text("Недостаточно характерных тегов для похожей подборки.")

    elif data.startswith("tag_search_"):
        tag = get_callback_payload("tag_search", data)
        if tag:
            schedule_background_task(
                context,
                send_search_gallery(
                    query.message,
                    user_id,
                    tag,
                    expected_generation=callback_generation,
                ),
            )

    elif data.startswith("tag_block_"):
        tag = get_callback_payload("tag_block", data)
        if tag:
            added = await add_to_blacklist(user_id, tag)
            await safe_query_answer(
                query,
                "Добавлено в чёрный список" if added else "Тег уже в чёрном списке",
            )
        else:
            await safe_query_answer(query, "Кнопка устарела")

    elif data.startswith("later_add_"):
        value = data.replace("later_add_", "", 1)
        post = await get_known_post(int(value)) if value.isdigit() else None
        settings = normalize_feature_settings(await get_user_settings(user_id))
        added = bool(post) and await add_read_later(
            user_id, post, settings.get("read_later_days", 30)
        )
        await safe_query_answer(
            query,
            "Добавлено в «На потом»" if added else "Уже сохранено или недоступно",
        )

    elif data == "later_list":
        await show_read_later(query.message, user_id, issuer=callback_issuer)

    elif data.startswith("later_open_"):
        value = data.replace("later_open_", "", 1)
        posts = await get_read_later(user_id, 100)
        post = next((item for item in posts if str(item.get("id")) == value), None)
        if post:
            settings = await get_user_settings(user_id)
            await send_post_media(
                query.message,
                post,
                keyboard=get_subscription_image_keyboard(
                    post.get("id", 0),
                    side_effect_callback=callback_issuer,
                ),
                settings=settings,
            )
        else:
            await query.message.reply_text("Пост больше не находится в списке.")

    elif data.startswith("later_del_"):
        value = data.replace("later_del_", "", 1)
        if value.isdigit():
            await remove_read_later(user_id, int(value))
        await show_read_later(query.message, user_id, issuer=callback_issuer)

    elif data == "storage":
        await show_storage(query.message, user_id)

    elif data == "storage_cleanup_90":
        await query.message.reply_text(
            "Удалить историю и служебные записи старше 90 дней?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🧹 Удалить",
                    callback_data=callback_issuer.side_effect(
                        "storage_cleanup_90_do"
                    ),
                ),
                InlineKeyboardButton("❌ Отмена", callback_data="storage"),
            ]]),
        )

    elif data == "storage_cleanup_90_do":
        removed = await cleanup_user_storage(user_id, 90)
        await query.message.reply_text(
            "🧹 Удалено старых записей: " + str(sum(removed.values()))
        )
        await show_storage(query.message, user_id)

    elif data == "storage_empty_collections":
        await query.message.reply_text(
            "Удалить все пустые коллекции?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🗑 Удалить",
                    callback_data=callback_issuer.side_effect(
                        "storage_empty_collections_do"
                    ),
                ),
                InlineKeyboardButton("❌ Отмена", callback_data="storage"),
            ]]),
        )

    elif data == "storage_empty_collections_do":
        removed = await cleanup_empty_collections(user_id)
        await query.message.reply_text(f"🗑 Удалено пустых коллекций: {removed}.")
        await show_storage(query.message, user_id)

    elif data.startswith("gallery_bulk_fav_"):
        raw_ids = get_callback_payload("gallery_bulk_fav", data) or ""
        added = 0
        for value in raw_ids.split(",")[:10]:
            post = await get_known_post(int(value)) if value.isdigit() else None
            if post and await add_favorite(user_id, post):
                added += 1
        await safe_query_answer(query, f"Добавлено в избранное: {added}")

    elif data.startswith("gallery_collection_"):
        raw_ids = get_callback_payload("gallery_collection", data) or ""
        bulk_post_ids = tuple(
            int(value) for value in raw_ids.split(",") if value.isdigit()
        )[:10]
        canonical_ids = ",".join(str(post_id) for post_id in bulk_post_ids)
        collections = await get_favorite_collections(user_id)
        if not await commit_flow_if_current(
            user_id, callback_generation, lambda: None
        ):
            return
        rows = [[InlineKeyboardButton(
            f"🗂 {item['name'][:28]}",
            callback_data=callback_issuer.side_effect(
                f"gallery_col_add:{item['id']}:{canonical_ids}"
            ),
        )] for item in collections]
        rows.append([InlineKeyboardButton(
            "➕ Новая коллекция",
            callback_data=callback_issuer.side_effect(
                f"gallery_col_new:{canonical_ids}"
            ),
        )])
        await query.message.reply_text(
            "Выберите коллекцию для всей подборки:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    elif data.startswith("gallery_col_add:"):
        _action, value, raw_ids = data.split(":", 2)
        collection_id = int(value) if value.isdigit() else 0
        added = 0
        bulk_post_ids = tuple(
            int(post_id) for post_id in raw_ids.split(",") if post_id.isdigit()
        )[:10]
        for post_id in bulk_post_ids:
            post = await get_known_post(post_id)
            if post:
                await add_favorite(user_id, post)
                if await add_favorite_to_collection(user_id, collection_id, post_id):
                    added += 1
        await query.message.reply_text(f"🗂 Добавлено в коллекцию: {added}.")

    elif data.startswith("gallery_col_new:"):
        raw_ids = data.split(":", 1)[1]
        bulk_post_ids = tuple(
            int(post_id) for post_id in raw_ids.split(",") if post_id.isdigit()
        )[:10]
        if await begin_callback_flow(
            "waiting_bulk_collection_name",
            bulk=(pending_bulk_posts, bulk_post_ids),
        ) is None:
            return
        await query.message.reply_text(
            "Введите название новой коллекции для этой подборки:",
            reply_markup=get_cancel_keyboard("library"),
        )

    elif data == "settings_spoiler":
        values = ["off", "explicit", "all"]
        settings = await mutate_user_settings(
            user_id,
            lambda current: {
                "spoiler_mode": values[
                    (values.index(current["spoiler_mode"]) + 1) % len(values)
                ]
            },
        )
        labels = {"off": "выключены", "explicit": "только explicit", "all": "для всех медиа"}
        await query.message.reply_text(
            f"🙈 Спойлеры: {labels[settings['spoiler_mode']]}",
            reply_markup=await get_user_settings_keyboard(user_id),
        )

    elif data == "sub_digest_send":
        claim_token, posts = await claim_subscription_digest(user_id, 10)
        if not claim_token:
            await query.message.reply_text("📨 Дайджест пока пуст.")
        else:
            claim_open = True
            locks: list[DigestSubscriptionLockHandle] = []
            lease: DigestClaimLease | None = None
            try:
                locks = await acquire_digest_subscription_locks(user_id, posts)
                active_keys = await get_subscription_digest_claim_keys(
                    user_id, claim_token
                )
                posts = [post for post in posts if digest_item_key(post) in active_keys]
                lease = DigestClaimLease(user_id, claim_token)
                if not posts or not await lease.start():
                    await cancellation_safe_digest_finish(user_id, claim_token, [])
                    claim_open = False
                    await query.message.reply_text("📨 Дайджест пока пуст.")
                    return
                delivery = await send_digest_posts(
                    query.message, user_id, posts, lease=lease
                )
                if delivery.ambiguous_ids:
                    await cancellation_safe_digest_finish(
                        user_id,
                        claim_token,
                        delivery.delivered_ids,
                        delivery.ambiguous_ids,
                    )
                else:
                    await cancellation_safe_digest_finish(
                        user_id, claim_token, delivery.delivered_ids
                    )
                claim_open = False
                total = len(posts)
                delivered_count = len(delivery.delivered_ids)
                logger.info(
                    "Manual digest result user=%s delivered=%s failed=%s ambiguous=%s",
                    user_id,
                    delivered_count,
                    len(delivery.failed_ids),
                    len(delivery.ambiguous_ids),
                )
                if delivered_count < total:
                    await query.message.reply_text(
                        f"⚠️ Доставлено {delivered_count} из {total} постов. "
                        "Остальные будут повторены позже."
                    )
            except DigestDeliveryCancelled as exc:
                await cancellation_safe_digest_finish(
                    user_id,
                    claim_token,
                    exc.result.delivered_ids,
                    exc.result.ambiguous_ids,
                )
                claim_open = False
                raise
            finally:
                if lease is not None:
                    await lease.stop()
                if claim_open:
                    await cancellation_safe_digest_release(user_id, claim_token)
                release_digest_subscription_locks(locks)

    elif data.startswith("sub_options_"):
        sub_query = get_callback_payload("sub_options", data)
        if sub_query:
            await show_subscription_options(query.message, user_id, sub_query)

    elif data.startswith(SUBSCRIPTION_OPTION_CALLBACK_PREFIXES):
        parsed_option = parse_subscription_option_callback(data)
        if parsed_option is None:
            await query.message.reply_text("Настройки подписки устарели.")
            return
        action, token = parsed_option
        sub_query = get_callback_payload_by_token("sub_options", token)
        if not sub_query:
            await query.message.reply_text("Настройки подписки устарели.")
            return
        options = await get_subscription_options(user_id, sub_query)
        if action == "rating":
            values = ["all", "s", "q", "e"]
            current = options.get("rating_filter", "all")
            options["rating_filter"] = values[(values.index(current) + 1) % len(values)]
        elif action == "type":
            values = ["all", "images", "animations", "videos"]
            current = options.get("media_type", "all")
            options["media_type"] = values[(values.index(current) + 1) % len(values)]
        elif action == "orientation":
            values = ["any", "portrait", "landscape", "square"]
            current = options.get("orientation", "any")
            options["orientation"] = values[(values.index(current) + 1) % len(values)]
        elif action == "resolution":
            values = [(0, 0), (1280, 720), (1920, 1080), (2560, 1440)]
            current = (int(options.get("min_width", 0)), int(options.get("min_height", 0)))
            choice = values[(values.index(current) + 1) % len(values)] if current in values else values[0]
            options["min_width"], options["min_height"] = choice
        elif action == "quality":
            values = ["auto", "preview", "sample", "original"]
            current = options.get("quality_mode", "auto")
            options["quality_mode"] = values[(values.index(current) + 1) % len(values)]
        elif action == "blacklist":
            if await begin_callback_flow(
                "waiting_subscription_blacklist",
                subscription=(pending_subscription_options, sub_query),
            ) is None:
                return
            await query.message.reply_text(
                "Введите дополнительные теги чёрного списка через пробел или `-` для сброса.",
                reply_markup=get_cancel_keyboard("subscriptions"),
            )
            return
        else:
            options["digest_mode"] = "instant" if options.get("digest_mode") == "digest" else "digest"
        await update_subscription_options(user_id, sub_query, options)
        await show_subscription_options(query.message, user_id, sub_query)

    elif data == "stats":
        await show_user_stats(query.message, user_id)

    elif data == "stats_clear_confirm":
        await query.message.reply_text(
            "Очистить историю поиска и отметки просмотренных постов? Избранное и настройки сохранятся.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ Очистить",
                    callback_data=callback_issuer.side_effect("stats_clear_do"),
                ),
                InlineKeyboardButton("Отмена", callback_data="stats"),
            ]]),
        )

    elif data == "stats_clear_do":
        await clear_user_activity_stats(user_id)
        await query.message.reply_text("✅ Персональная статистика очищена.")

    elif data == "fav_collections":
        await show_collections(query.message, user_id)

    elif data == "col_create":
        if await begin_callback_flow("waiting_collection_create") is None:
            return
        await query.message.reply_text(
            "Введите название коллекции (до 40 символов):",
            reply_markup=get_cancel_keyboard("fav_collections"),
        )

    elif data.startswith("col_open_"):
        value = data.replace("col_open_", "", 1)
        if value.isdigit():
            await show_collection(
                query.message, user_id, int(value), issuer=callback_issuer
            )

    elif data.startswith("col_page_"):
        parts = data.split("_")
        if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
            await show_collection(
                query.message,
                user_id,
                int(parts[2]),
                int(parts[3]),
                issuer=callback_issuer,
            )

    elif data.startswith("col_delete_") and not data.startswith("col_delete_do_"):
        value = data.replace("col_delete_", "", 1)
        if value.isdigit():
            await query.message.reply_text(
                "Удалить коллекцию? Посты останутся в общем избранном.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🗑 Удалить",
                        callback_data=callback_issuer.side_effect(
                            f"col_delete_do_{value}"
                        ),
                    ),
                    InlineKeyboardButton("❌ Отмена", callback_data="fav_collections"),
                ]]),
            )

    elif data.startswith("col_delete_do_"):
        value = data.replace("col_delete_do_", "", 1)
        if value.isdigit():
            await delete_favorite_collection(user_id, int(value))
            await show_collections(query.message, user_id)

    elif data.startswith("col_rename_"):
        value = data.replace("col_rename_", "", 1)
        if value.isdigit():
            if await begin_callback_flow(
                f"waiting_collection_rename_{value}"
            ) is None:
                return
            await query.message.reply_text(
                "Введите новое название коллекции:",
                reply_markup=get_cancel_keyboard("fav_collections"),
            )

    elif data.startswith("fav_col_pick_"):
        value = data.replace("fav_col_pick_", "", 1)
        if value.isdigit():
            await show_collection_picker(
                query.message, user_id, int(value), issuer=callback_issuer
            )

    elif data.startswith("col_add_"):
        parts = data.split("_")
        if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
            added = await add_favorite_to_collection(user_id, int(parts[2]), int(parts[3]))
            await query.message.reply_text(
                "✅ Добавлено в коллекцию." if added else "ℹ️ Пост уже в коллекции или не найден."
            )

    elif data.startswith("col_remove_"):
        parts = data.split("_")
        if len(parts) == 5 and all(part.isdigit() for part in parts[2:]):
            collection_id, post_id, index = map(int, parts[2:])
            await remove_favorite_from_collection(user_id, collection_id, post_id)
            await show_collection(
                query.message,
                user_id,
                collection_id,
                index,
                issuer=callback_issuer,
            )

    elif data.startswith("col_export_"):
        value = data.replace("col_export_", "", 1)
        if value.isdigit():
            await enqueue_collection_zip_export(query.message, user_id, int(value))

    elif data.startswith("zip_cancel_"):
        job_id = data.replace("zip_cancel_", "", 1)
        cancelled = bool(zip_export_manager) and await zip_export_manager.cancel_for_user(
            user_id, job_id
        )
        if not cancelled:
            await query.message.reply_text("ℹ️ ZIP-экспорт уже завершён или не найден.")

    elif data.startswith("fav_note_"):
        value = data.replace("fav_note_", "", 1)
        if value.isdigit():
            note_generation = await begin_callback_flow(
                f"waiting_favorite_note_{value}"
            )
            if note_generation is None:
                return
            current = await get_favorite_note(user_id, int(value))
            if not await commit_flow_if_current(
                user_id, note_generation, lambda: None
            ):
                return
            await query.message.reply_text(
                "Введите заметку до 1000 символов. Отправьте `-`, чтобы удалить."
                + (f"\n\nСейчас: {current}" if current else ""),
                reply_markup=get_cancel_keyboard("library"),
            )

    elif data == "fav_gallery":
        await send_favorites_gallery(query.message, user_id, issuer=callback_issuer)

    elif data == "fav_list":
        await show_favorites_list(query.message, user_id, edit=False, page=0)

    elif data == "fav_find":
        if await begin_callback_flow("waiting_fav_tag") is None:
            return
        await query.edit_message_text(
            "🔎 Введите теги или слова из заметки для поиска в избранном:\n\n"
            "Пример: `blonde_hair wallpaper`",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("library"),
        )

    elif data == "fav_export":
        await enqueue_favorites_zip_export(query.message, user_id)

    elif data.startswith("fav_list_page_"):
        page_text = data.replace("fav_list_page_", "", 1)
        if not page_text.isdigit():
            await query.message.reply_text("Не удалось открыть страницу избранного.")
            return
        await show_favorites_list(query.message, user_id, edit=True, page=int(page_text))

    elif data.startswith("fav_tag_page_"):
        payload = get_callback_payload("fav_tag_page", data)
        if not payload or "\n" not in payload:
            await query.message.reply_text("Не удалось открыть страницу избранного.")
            return
        tag_filter, page_text = payload.split("\n", 1)
        if not page_text.isdigit():
            await query.message.reply_text("Не удалось открыть страницу избранного.")
            return
        await show_favorites_list(
            query.message,
            user_id,
            edit=True,
            page=int(page_text),
            tag_filter=tag_filter,
        )

    elif data == "noop":
        return

    elif data.startswith("post_original_"):
        post_id_text = data.replace("post_original_", "", 1)
        if not post_id_text.isdigit():
            await query.message.reply_text("❌ Не удалось определить пост.")
            return
        post = await get_known_post(int(post_id_text))
        if not post or not post.get("file_url"):
            post = await api.get_post_by_id(int(post_id_text))
            if post:
                await remember_and_cache_post(post)
        if not post:
            await query.message.reply_text("❌ Оригинал недоступен.")
            return
        settings = await get_user_settings(user_id)
        settings["quality_mode"] = "original"
        await send_post_media(
            query.message,
            post,
            keyboard=get_image_keyboard(
                int(post_id_text),
                show_tags_button=should_show_tags_button(settings),
                side_effect_callback=callback_issuer,
            ),
            settings=settings,
        )

    elif data == "post_tags_noop":
        return

    elif data.startswith("post_tags_page_"):
        payload = data.replace("post_tags_page_", "", 1)
        try:
            post_id_text, page_text = payload.rsplit("_", 1)
            post_id, page = int(post_id_text), int(page_text)
        except (TypeError, ValueError):
            await query.message.reply_text("❌ Не удалось открыть страницу тегов.")
            return
        await send_full_post_tags(
            query.message,
            post_id,
            user_id,
            callback_issuer,
            page=page,
            edit=True,
        )

    elif data.startswith("post_tags_"):
        post_id_text = data.replace("post_tags_", "", 1)
        if not post_id_text.isdigit():
            await query.message.reply_text("❌ Не удалось определить пост.")
            return
        await send_full_post_tags(
            query.message, int(post_id_text), user_id, callback_issuer
        )

    elif data == "settings":
        settings = await get_user_settings(user_id)
        caption_enabled = (
            "✅ Включено" if settings.get(
                "show_caption", True) else "❌ Выключено"
        )

        await query.edit_message_text(
            "Главная → Настройки\n\n"
            f"Подписи к постам: {caption_enabled}\n\n"
            "Здесь можно настроить внешний вид постов, подборки и качество медиа.",
            reply_markup=get_settings_keyboard(normalize_feature_settings(settings)),
            parse_mode="Markdown",
        )

    elif data == "settings_caption":
        settings = await get_user_settings(user_id)
        text = build_caption_settings_text(settings)
        keyboard = await get_caption_settings_keyboard(user_id)

        try:
            await query.edit_message_text(
                text=text, reply_markup=keyboard, parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Error in settings_caption: {e}")
            await query.message.reply_text(
                text=text, reply_markup=keyboard, parse_mode="Markdown"
            )

    elif data == "settings_gallery":
        settings = normalize_feature_settings(await get_user_settings(user_id))
        await query.edit_message_text(
            gallery_settings_text(settings),
            reply_markup=get_gallery_settings_keyboard(settings),
            parse_mode="Markdown",
        )

    elif data == "settings_quality":
        settings = normalize_feature_settings(await get_user_settings(user_id))
        await query.edit_message_text(
            quality_settings_text(settings),
            reply_markup=get_quality_settings_keyboard(settings),
            parse_mode="Markdown",
        )

    elif data.startswith("gallery_cycle_") or data.startswith("gallery_size_"):
        def mutate_gallery(current):
            if data == "gallery_cycle_sort":
                values = ["random", "new", "popular"]
                return {"gallery_sort": values[(values.index(current["gallery_sort"]) + 1) % len(values)]}
            if data == "gallery_cycle_rating":
                values = ["all", "s", "q", "e"]
                return {"rating_filter": values[(values.index(current["rating_filter"]) + 1) % len(values)]}
            if data == "gallery_cycle_type":
                values = ["all", "images", "animations", "videos"]
                return {"media_type": values[(values.index(current["media_type"]) + 1) % len(values)]}
            if data == "gallery_cycle_orientation":
                values = ["any", "portrait", "landscape", "square"]
                return {"orientation": values[(values.index(current["orientation"]) + 1) % len(values)]}
            if data == "gallery_size_down":
                return {"gallery_size": max(2, current["gallery_size"] - 1)}
            return {"gallery_size": min(10, current["gallery_size"] + 1)}

        settings = await mutate_user_settings(user_id, mutate_gallery)
        await query.edit_message_text(
            gallery_settings_text(settings),
            reply_markup=get_gallery_settings_keyboard(settings),
            parse_mode="Markdown",
        )

    elif data == "gallery_resolution":
        if await begin_callback_flow("waiting_gallery_resolution") is None:
            return
        await query.edit_message_text(
            "Введите минимальное разрешение как `ширинаxвысота`, например `1920x1080`. Для сброса: `0x0`.",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("settings_gallery"),
        )

    elif data == "quality_cycle_mode" or data.startswith("quality_max_"):
        def mutate_quality(current):
            if data == "quality_cycle_mode":
                values = ["auto", "preview", "sample", "original"]
                return {"quality_mode": values[(values.index(current["quality_mode"]) + 1) % len(values)]}
            if data == "quality_max_down":
                return {"max_file_mb": max(1, current["max_file_mb"] - 1)}
            return {"max_file_mb": min(50, current["max_file_mb"] + 1)}

        settings = await mutate_user_settings(user_id, mutate_quality)
        await query.edit_message_text(
            quality_settings_text(settings),
            reply_markup=get_quality_settings_keyboard(settings),
            parse_mode="Markdown",
        )

    elif data == "settings_reset":
        await query.edit_message_text(
            "Сбросить все настройки к значениям по умолчанию?\n\n"
            "Библиотека, подписки и чёрный список не будут удалены.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ Сбросить",
                    callback_data=callback_issuer.side_effect("settings_reset_do"),
                ),
                InlineKeyboardButton("❌ Отмена", callback_data="settings"),
            ]]),
        )

    elif data == "settings_reset_do":
        await save_user_settings(user_id, DEFAULT_USER_SETTINGS)
        await query.edit_message_text(
            "✅ Настройки сброшены к значениям по умолчанию!",
            reply_markup=await get_user_settings_keyboard(user_id),
            parse_mode="Markdown",
        )

    elif data == "settings_interface_mode":
        settings = await mutate_user_settings(
            user_id,
            lambda current: {
                "interface_mode": (
                    "advanced"
                    if current["interface_mode"] == "simple"
                    else "simple"
                )
            },
        )
        label = "расширенный" if settings["interface_mode"] == "advanced" else "простой"
        await query.edit_message_text(
            f"🧭 Режим интерфейса: {label}.\n\n"
            "Нижняя клавиатура обновлена. Расширенный режим показывает быстрый "
            "доступ к подборкам, подпискам и разделу данных.",
            reply_markup=get_settings_keyboard(settings),
        )
        await query.message.reply_text(
            "Основные кнопки обновлены.",
            reply_markup=get_persistent_keyboard(settings["interface_mode"]),
        )

    elif data == "settings_pause_subscriptions":
        if await begin_callback_flow("waiting_pause_subscriptions") is None:
            return
        await query.edit_message_text(
            "⏸ На сколько остановить все активные подписки?\n\n"
            "Можно написать в минутах или коротко: `30`, `2ч`, `1д`.\n"
            "Максимум: 7 дней.",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("subscriptions"),
        )

    elif data == "settings_resume_subscriptions":
        resumed_count = await resume_all_active_subscriptions(user_id)
        await query.edit_message_text(
            "▶️ Подписки возобновлены.\n\n"
            f"Активных подписок: {resumed_count}.",
            reply_markup=await get_user_subscriptions_keyboard(user_id),
        )

    elif data.startswith("toggle_"):
        setting_name = data.replace("toggle_", "")
        if setting_name not in DEFAULT_CAPTION_SETTINGS:
            await query.message.reply_text("Настройка устарела.")
            return

        def mutate_caption(current):
            current_value = bool(current.get(setting_name, True))
            patch = {setting_name: not current_value}
            if setting_name == "show_caption" and current_value:
                patch.update({
                    "show_search_query": False,
                    "show_subscription_label": False,
                    "show_id": False,
                    "show_score": False,
                    "show_rating": False,
                    "show_tags": False,
                    "show_tags_button": False,
                })
            elif setting_name == "show_caption" and not current_value:
                patch.update({
                    "show_id": True,
                    "show_tags": True,
                    "show_tags_button": True,
                })
            return patch

        settings = await mutate_user_settings(user_id, mutate_caption)

        text = build_caption_settings_text(settings)
        keyboard = await get_caption_settings_keyboard(user_id)

        try:
            await query.edit_message_text(
                text=text, reply_markup=keyboard, parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Error updating toggle: {e}")

    elif data == "sub_add_current":
        saved = await get_user_query(user_id)
        if saved and saved[0]:
            if await begin_callback_flow(
                f"waiting_sub_interval_{saved[0]}"
            ) is None:
                return
            await query.edit_message_text(
                f"🔔 Подписка на: `{md_code(saved[0])}`\n\n"
                "Введите интервал в минутах от 1 до 120 (по умолчанию 10):",
                parse_mode="Markdown",
                reply_markup=get_cancel_keyboard("subscriptions"),
            )
        else:
            await query.message.reply_text(
                "❌ Сначала выполните поиск!",
                reply_markup=await get_user_subscriptions_keyboard(user_id),
            )

    elif data == "sub_add_new":
        if await begin_callback_flow("waiting_sub_new") is None:
            return
        await query.edit_message_text(
            "🔔 Введите теги для подписки (через пробел):\n\n" "Пример: `anime girl`",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("subscriptions"),
        )

    elif data == "sub_list":
        subscriptions = await get_all_user_subscriptions(user_id)
        if subscriptions:
            subs_list = []
            for sub_query, interval, is_active, empty_count, next_check_at in subscriptions:
                if not is_active:
                    status = "⏸ остановлена"
                elif empty_count:
                    status = f"🕒 ожидает новые посты, пустых проверок: {empty_count}"
                else:
                    status = "✅ активна"
                subs_list.append(
                    f"• `{md_code(sub_query)}` - каждые {interval} мин., {status}"
                )

            text = "📋 *Ваши подписки:*\n\n" + "\n".join(subs_list)
        else:
            text = "📋 У вас пока нет подписок."

        await query.edit_message_text(
            text,
            reply_markup=await get_user_subscriptions_keyboard(user_id),
            parse_mode="Markdown",
        )

    elif data == "sub_manage":
        subscriptions = await get_all_user_subscriptions(user_id)
        if not subscriptions:
            await query.edit_message_text(
                "❌ У вас нет подписок.",
                reply_markup=await get_user_subscriptions_keyboard(user_id),
            )
            return

        keyboard = []
        for sub_query, interval, is_active, empty_count, next_check_at in subscriptions:
            wait_marker = " 🕒" if is_active and empty_count else ""
            status_icon = "✅" if is_active else "⏸"
            toggle_action = "Приостановить" if is_active else "Возобновить"
            keyboard.extend(
                [
                    [
                        InlineKeyboardButton(
                            f"{status_icon}{wait_marker} {sub_query[:32]}",
                            callback_data="noop",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            f"{toggle_action}",
                            callback_data=callback_issuer.payload(
                                "sub_toggle", sub_query
                            ),
                        ),
                        InlineKeyboardButton(
                            f"⏱ {interval} мин.",
                            callback_data=store_callback_payload("sub_interval", sub_query),
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "🖼 Посты",
                            callback_data=store_callback_payload("sub_posts", sub_query),
                        ),
                        InlineKeyboardButton(
                            "🎛 Фильтры",
                            callback_data=store_callback_payload("sub_options", sub_query),
                        ),
                        InlineKeyboardButton(
                            "🗑 Удалить",
                            callback_data=store_callback_payload("sub_remove", sub_query),
                        ),
                    ],
                ]
            )

        keyboard.append(
            [InlineKeyboardButton("⬅️ К подпискам", callback_data="subscriptions")]
        )

        await query.edit_message_text(
            "⚙️ *Управление подписками*\n\n🕒 значит, что тег временно исчерпан: бот проверяет его реже и вернется к обычному интервалу, когда появится новый пост.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    elif data.startswith("sub_interval_"):
        sub_query = get_callback_payload("sub_interval", data)
        if not sub_query:
            await query.edit_message_text(
                "❌ Не удалось найти подписку. Откройте список подписок заново.",
                parse_mode="Markdown",
            )
            return

        if await begin_callback_flow(
            f"waiting_sub_interval_update_{sub_query}"
        ) is None:
            return
        await query.edit_message_text(
            f"⏱ Новый интервал для `{md_code(sub_query)}`\n\n"
            "Введите число минут от 1 до 120:",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("subscriptions"),
        )

    elif data.startswith("sub_posts_"):
        token = data.replace("sub_posts_", "", 1)
        sub_query = get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.edit_message_text(
                "❌ Не удалось найти подписку. Откройте список подписок заново.",
                parse_mode="Markdown",
            )
            return

        await show_subscription_posts_menu(query.message, user_id, sub_query, token)

    elif data.startswith("sub_list_posts_"):
        token = data.replace("sub_list_posts_", "", 1)
        sub_query = get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.message.reply_text(
                "❌ Не удалось найти подписку. Откройте список заново."
            )
            return

        await show_subscription_posts_menu(
            query.message, user_id, sub_query, token, edit=False
        )

    elif data.startswith("sub_one_"):
        parts = data.split("_")
        if len(parts) < 4 or not parts[-1].isdigit():
            await query.message.reply_text("❌ Не удалось открыть пост.")
            return

        token = parts[2]
        index = int(parts[3])
        sub_query = get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.message.reply_text(
                "❌ Не удалось найти подписку. Откройте список заново."
            )
            return

        await send_subscription_post_by_index(
            query.message, user_id, sub_query, index, issuer=callback_issuer
        )

    elif data.startswith("sub_all_"):
        token = data.replace("sub_all_", "", 1)
        sub_query = get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.message.reply_text(
                "❌ Не удалось найти подписку. Откройте список заново."
            )
            return

        await send_subscription_gallery(
            query.message, user_id, sub_query, token, issuer=callback_issuer
        )

    elif data.startswith("sub_page_"):
        parts = data.split("_")
        if len(parts) < 4 or not parts[-1].isdigit():
            await query.message.reply_text("❌ Не удалось открыть пост.")
            return

        token = parts[2]
        index = int(parts[3])
        sub_query = get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.message.reply_text(
                "❌ Не удалось найти подписку. Откройте список заново."
            )
            return

        await edit_subscription_gallery(
            query, user_id, sub_query, token, index, issuer=callback_issuer
        )

    elif data.startswith("sub_post_del_"):
        parts = data.split("_")
        if len(parts) < 5 or not parts[-1].isdigit() or not parts[-2].isdigit():
            await query.message.reply_text("❌ Не удалось удалить пост.")
            return

        token = parts[3]
        post_id = int(parts[4])
        index = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
        sub_query = get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.message.reply_text(
                "❌ Не удалось найти подписку. Откройте список заново."
            )
            return

        await remove_subscription_post(user_id, sub_query, post_id)
        await remove_favorite(user_id, post_id)
        await edit_subscription_gallery(
            query, user_id, sub_query, token, index, issuer=callback_issuer
        )

    elif data.startswith("sub_toggle_"):
        sub_query = get_callback_payload("sub_toggle", data)
        if not sub_query:
            await query.edit_message_text(
                "❌ Не удалось найти подписку. Откройте список подписок заново.",
                parse_mode="Markdown",
            )
            return

        toggle_result = await toggle_subscription(user_id, sub_query)
        if toggle_result.status == "not_found":
            await query.edit_message_text(
                "❌ Подписка не найдена.", parse_mode="Markdown"
            )
        elif toggle_result.status in {"active_limit_reached", "total_limit_reached"}:
            await query.edit_message_text(
                (
                    "❌ Сначала удалите лишние подписки до общего лимита."
                    if toggle_result.status == "total_limit_reached"
                    else "❌ Сначала приостановите или удалите одну из активных подписок."
                ),
                reply_markup=await get_user_subscriptions_keyboard(user_id),
                parse_mode="Markdown",
            )
        else:
            state_text = "запущена" if toggle_result.is_active else "остановлена"
            await query.edit_message_text(
                f"✅ Подписка `{md_code(sub_query)}` {state_text}.",
                reply_markup=await get_user_subscriptions_keyboard(user_id),
                parse_mode="Markdown",
            )

    elif data.startswith("sub_create_"):
        payload = get_callback_payload("sub_create", data)
        try:
            preview = json.loads(payload or "{}")
            sub_query = str(preview["query"]).strip()
            interval = parse_subscription_interval(str(preview["interval"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await query.edit_message_text("❌ Предпросмотр устарел. Создайте подписку заново.")
            return
        result = await add_subscription(user_id, sub_query, interval)
        await query.edit_message_text(
            await build_subscription_added_text(sub_query, interval, user_id)
            if result
            else build_subscription_create_error(result),
            reply_markup=await get_user_subscriptions_keyboard(user_id),
            parse_mode="Markdown",
        )

    elif data.startswith("subscribe_"):
        # Подписка из клавиатуры под изображением
        sub_query = get_callback_payload("subscribe", data)
        if not sub_query:
            await query.message.reply_text(
                "❌ Не удалось найти запрос для подписки. Попробуйте выполнить поиск заново.",
                parse_mode="Markdown",
            )
            return

        preview_text, preview_keyboard = get_subscription_preview(
            sub_query, 10, user_id, issuer=callback_issuer
        )
        await query.message.reply_text(
            preview_text,
            reply_markup=preview_keyboard,
            parse_mode="Markdown",
        )

    elif data.startswith("sub_remove_") and not data.startswith("sub_remove_do_"):
        # Удаление подписки
        sub_query = get_callback_payload("sub_remove", data)
        if not sub_query:
            await query.edit_message_text(
                "❌ Не удалось найти подписку для удаления. Откройте список подписок заново.",
                parse_mode="Markdown",
            )
            return

        confirm_callback = callback_issuer.payload("sub_remove_do", sub_query)
        await query.edit_message_text(
            f"Удалить подписку `{md_code(sub_query)}`?\n\n"
            "Сохранённые посты этой подписки также будут удалены.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Удалить", callback_data=confirm_callback),
                InlineKeyboardButton("❌ Отмена", callback_data="subscriptions"),
            ]]),
            parse_mode="Markdown",
        )

    elif data.startswith("sub_remove_do_"):
        sub_query = get_callback_payload("sub_remove_do", data)
        if not sub_query:
            await query.edit_message_text("❌ Подтверждение устарело.")
            return
        async with digest_subscription_lock(user_id, sub_query):
            success = await remove_subscription(user_id, sub_query)

        if success:
            await query.edit_message_text(
                f"✅ Подписка на `{md_code(sub_query)}` удалена.",
                reply_markup=await get_user_subscriptions_keyboard(user_id),
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                "Подписка не найдена.", parse_mode="Markdown"
            )
    elif data.startswith("fav_remove_") and not data.startswith("fav_remove_do_"):
        payload_parts = data.replace("fav_remove_", "", 1).split("_")
        post_id_text = payload_parts[0]
        page = int(payload_parts[1]) if len(payload_parts) > 1 and payload_parts[1].isdigit() else 0
        if not post_id_text.isdigit():
            await query.message.reply_text("Не удалось определить пост.")
            return

        await query.message.reply_text(
            f"Удалить пост `{md_code(post_id_text)}` из избранного?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🗑 Удалить",
                    callback_data=callback_issuer.side_effect(
                        f"fav_remove_do_{post_id_text}_{page}"
                    ),
                ),
                InlineKeyboardButton("❌ Отмена", callback_data="fav_list"),
            ]]),
            parse_mode="Markdown",
        )

    elif data.startswith("fav_remove_do_"):
        payload_parts = data.replace("fav_remove_do_", "", 1).split("_")
        post_id_text = payload_parts[0]
        page = int(payload_parts[1]) if len(payload_parts) > 1 and payload_parts[1].isdigit() else 0
        if not post_id_text.isdigit():
            await query.message.reply_text("Не удалось определить пост.")
            return

        removed = await remove_favorite(user_id, int(post_id_text))
        if removed:
            await show_favorites_list(query.message, user_id, edit=True, page=page)
        else:
            await query.message.reply_text("Пост не найден в избранном.")

    elif data.startswith("fav_open_"):
        post_id_text = data.replace("fav_open_", "", 1)
        if not post_id_text.isdigit():
            await query.message.reply_text("❌ Не удалось определить пост.")
            return

        post = await get_favorite(user_id, int(post_id_text))
        if not post:
            await query.message.reply_text("❌ Пост не найден в избранном.")
            return

        settings = await get_user_settings(user_id)
        caption = build_favorites_gallery_caption(post, 0, 1)
        await send_post_media(
            query.message,
            post,
            caption,
            get_image_keyboard(
                post["id"],
                show_tags_button=should_show_tags_button(settings),
                side_effect_callback=callback_issuer,
            ),
            settings=settings,
        )

    elif data == "fav_all":
        await send_favorites_gallery(query.message, user_id, issuer=callback_issuer)

    elif data.startswith("fav_page_"):
        page_text = data.replace("fav_page_", "", 1)
        if not page_text.isdigit():
            await query.message.reply_text("❌ Не удалось открыть страницу.")
            return

        await send_favorites_gallery(
            query.message, user_id, int(page_text), issuer=callback_issuer
        )

    elif data.startswith("fav_del_") and not data.startswith("fav_del_do_"):
        parts = data.split("_")
        if len(parts) < 4 or not parts[2].isdigit() or not parts[3].isdigit():
            await query.message.reply_text("❌ Не удалось удалить пост.")
            return

        post_id = int(parts[2])
        index = int(parts[3])
        await query.message.reply_text(
            f"Удалить пост `{post_id}` из избранного?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🗑 Удалить",
                    callback_data=callback_issuer.side_effect(
                        f"fav_del_do_{post_id}_{index}"
                    ),
                ),
                InlineKeyboardButton("❌ Отмена", callback_data="fav_gallery"),
            ]]),
            parse_mode="Markdown",
        )

    elif data.startswith("fav_del_do_"):
        parts = data.split("_")
        if len(parts) < 5 or not parts[3].isdigit() or not parts[4].isdigit():
            await query.message.reply_text("❌ Не удалось удалить пост.")
            return
        post_id = int(parts[3])
        index = int(parts[4])
        await remove_favorite(user_id, post_id)
        await edit_favorites_gallery(query, user_id, index, issuer=callback_issuer)

    elif data.startswith("sub_fav_"):
        payload = data.replace("sub_fav_", "", 1)
        sub_query = ""
        if payload.isdigit():
            post_id_text = payload
        else:
            legacy_payload = get_callback_payload("sub_fav", data)
            if not legacy_payload or "\n" not in legacy_payload:
                await query.message.reply_text("❌ Не удалось определить пост подписки.")
                return
            post_id_text, sub_query = legacy_payload.split("\n", 1)

        if not post_id_text.isdigit():
            await query.message.reply_text("❌ Не удалось определить пост подписки.")
            return

        post_id = int(post_id_text)
        post = await get_known_post(post_id) or minimal_post(post_id)
        if not post.get("file_url"):
            logger.warning(
                "Saving subscription favorite without cached media user=%s post=%s",
                user_id,
                post_id,
            )

        await add_favorite(user_id, post)
        sub_queries = [sub_query] if sub_query else await get_subscription_queries_for_post(user_id, post_id)
        for known_sub_query in sub_queries:
            await add_subscription_post(user_id, known_sub_query, post)

        if sub_queries:
            await query.message.reply_text(
                f"⭐ Пост `{md_code(post_id)}` добавлен в избранное подписки.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🗂 В коллекцию",
                        callback_data=callback_issuer.side_effect(
                            f"fav_col_pick_{post_id}"
                        ),
                    )
                ]]),
                parse_mode="Markdown",
            )
        else:
            await query.message.reply_text(
                f"⭐ Пост `{md_code(post_id)}` добавлен в избранное.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🗂 В коллекцию",
                        callback_data=callback_issuer.side_effect(
                            f"fav_col_pick_{post_id}"
                        ),
                    )
                ]]),
                parse_mode="Markdown",
            )
            logger.warning(
                "Subscription favorite saved without matching subscription user=%s post=%s",
                user_id,
                post_id,
            )

    elif data.startswith("fav_"):
        post_id_text = data.replace("fav_", "", 1)
        if not post_id_text.isdigit():
            await query.message.reply_text("❌ Не удалось определить пост.")
            return

        post_id = int(post_id_text)
        post = await get_known_post(post_id) or minimal_post(post_id)
        if not post.get("file_url"):
            logger.warning(
                "Saving favorite without cached media user=%s post=%s",
                user_id,
                post_id,
            )

        added = await add_favorite(user_id, post)
        if added:
            await query.message.reply_text(
                f"⭐ Пост `{md_code(post_id)}` добавлен в избранное.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🗂 В коллекцию",
                        callback_data=callback_issuer.side_effect(
                            f"fav_col_pick_{post_id}"
                        ),
                    )
                ]]),
                parse_mode="Markdown",
            )
        else:
            await query.message.reply_text(
                f"⭐ Пост `{md_code(post_id)}` уже есть в избранном.",
                parse_mode="Markdown",
            )

    elif data.startswith("hist_"):
        history_query = get_callback_payload("hist", data)
        if not history_query:
            await query.message.reply_text(
                "❌ Не удалось найти запрос. Откройте историю заново."
            )
            return
        schedule_background_task(
            context,
            send_image(
                query.message,
                user_id,
                history_query,
                expected_generation=callback_generation,
            ),
        )

    elif data == "bl_add":
        if await begin_callback_flow("waiting_bl_add") is None:
            return
        await query.edit_message_text(
            "➕ Введите тег для добавления в чёрный список:\n\n"
            "💡 Можно ввести несколько тегов через пробел",
            reply_markup=get_cancel_keyboard("blacklist"),
        )

    elif data == "bl_remove":
        remove_generation = await begin_callback_flow("waiting_bl_remove")
        if remove_generation is None:
            return
        blacklist = await get_user_blacklist(user_id)
        if not await commit_flow_if_current(
            user_id, remove_generation, lambda: None
        ):
            return
        if blacklist:
            ordered_tags = sorted(blacklist)
            visible_tags = ordered_tags[:60]
            translations = await tag_translation_service.translate_tags(visible_tags)
            tags_list = ", ".join(
                f"`{md_code(tag)}`"
                + (f" — {md_text(translations[tag])}" if translations.get(tag) else "")
                for tag in visible_tags
            )
            if len(ordered_tags) > len(visible_tags):
                tags_list += f"\n\n…и ещё {len(ordered_tags) - len(visible_tags)}. Полный список доступен через «Показать»."
            text = f"➖ Введите тег для удаления:\n\nВаши теги: {tags_list}"
        else:
            text = "➖ Ваш чёрный список пуст"
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("blacklist"),
        )

    elif data == "bl_show":
        entries = await get_blacklist_entries(user_id)
        if entries:
            translations = await tag_translation_service.translate_tags(
                [item["tag"] for item in entries], immediate_limit=50
            )
            pages = []
            current = "📋 *Ваш чёрный список:*\n\n"
            for item in entries:
                translation = translations.get(item["tag"], "")
                line = f"• `{md_code(item['tag'])}`"
                if translation:
                    line += f" — {md_text(translation)}"
                if item["expires_at"]:
                    line += f" — до {md_text(item['expires_at'])}"
                line += "\n"
                if len(current) + len(line) > 3900:
                    pages.append(current.rstrip())
                    current = line
                else:
                    current += line
            if current.strip():
                pages.append(current.rstrip())
        else:
            pages = ["📋 Ваш чёрный список пуст"]

        await query.edit_message_text(
            pages[0],
            reply_markup=get_blacklist_keyboard() if len(pages) == 1 else None,
            parse_mode="Markdown",
        )
        for index, page in enumerate(pages[1:], start=1):
            await query.message.reply_text(
                page,
                reply_markup=get_blacklist_keyboard() if index == len(pages) - 1 else None,
                parse_mode="Markdown",
            )

    elif data == "bl_temp":
        if await begin_callback_flow("waiting_bl_temp") is None:
            return
        await query.edit_message_text(
            "Введите тег и срок: `tag 2ч`, `tag 1д` или `tag 30` (минуты).",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("blacklist"),
        )

    elif data == "bl_import":
        if await begin_callback_flow("waiting_bl_import") is None:
            return
        await query.edit_message_text(
            "Отправьте список тегов через пробел, запятую или с новой строки. "
            "Импорт заменит текущий чёрный список.",
            reply_markup=get_cancel_keyboard("blacklist"),
        )

    elif data == "bl_export":
        entries = await get_blacklist_entries(user_id)
        content = "\n".join(item["tag"] for item in entries).encode("utf-8")
        document = io.BytesIO(content)
        document.name = f"blacklist_{user_id}.txt"
        await query.message.reply_document(
            document=document,
            filename=document.name,
            caption=f"Чёрный список: {len(entries)} тегов",
        )

    elif data == "bl_suggest":
        if await begin_callback_flow("waiting_bl_suggest") is None:
            return
        await query.edit_message_text(
            "Введите тег, для которого найти похожие варианты:",
            reply_markup=get_cancel_keyboard("blacklist"),
        )

    elif data == "bl_presets":
        rows = []
        for preset, tags in BLACKLIST_PRESETS.items():
            rows.append([
                InlineKeyboardButton(
                    f"➕ {preset} ({len(tags)})",
                    callback_data=callback_issuer.side_effect(
                        f"bl_preset_add_{preset}"
                    ),
                ),
                InlineKeyboardButton(
                    "➖",
                    callback_data=callback_issuer.side_effect(
                        f"bl_preset_del_{preset}"
                    ),
                ),
            ])
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data="blacklist")])
        await query.edit_message_text(
            "🧰 *Готовые наборы чёрного списка*\n\n"
            "Добавление набора не удаляет ваши собственные теги.",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )

    elif data.startswith("bl_preset_add_"):
        preset = data.replace("bl_preset_add_", "", 1)
        changed = await apply_blacklist_preset(user_id, preset)
        await query.message.reply_text(f"✅ Добавлено тегов: {changed}.")

    elif data.startswith("bl_preset_del_"):
        preset = data.replace("bl_preset_del_", "", 1)
        changed = await remove_blacklist_preset(user_id, preset)
        await query.message.reply_text(f"✅ Удалено тегов набора: {changed}.")

    elif data.startswith("bl_quick_"):
        tag = get_callback_payload("bl_quick", data)
        if tag:
            added = await add_to_blacklist(user_id, tag)
            await safe_query_answer(
                query,
                "Тег добавлен" if added else "Тег уже в чёрном списке",
            )
        else:
            await safe_query_answer(query, "Кнопка устарела")

    elif data == "back":
        await invalidate_user_flow(user_id)
        await query.edit_message_text(
            await build_main_menu_text(user_id),
            reply_markup=await get_user_main_keyboard(user_id),
        )

    elif data == "help":
        settings = normalize_feature_settings(await get_user_settings(user_id))
        await query.edit_message_text(
            "Главная → Помощь\n\n"
            "*Быстрый старт*\n"
            "1. Откройте «🔎 Поиск».\n"
            "2. Отправьте теги через пробел.\n"
            "3. Сохраняйте понравившиеся посты в библиотеку или создавайте подписки.\n\n"
            "*Основные разделы*\n"
            "• *Поиск* — один пост, случайный результат, подборка, конструктор запроса, "
            "история и сохранённые запросы.\n"
            "• *Библиотека* — избранное, коллекции, заметки, рекомендации и список «На потом».\n"
            "• *Подписки* — автоматическая проверка запросов по расписанию. Перед созданием "
            "бот показывает запрос и интервал для подтверждения.\n"
            "• *Чёрный список* — исключает нежелательные теги из поиска, подборок и случайных постов.\n"
            "• *Настройки* — подписи, спойлеры, размер подборок, качество медиа и режим интерфейса.\n"
            "• *Мои данные* — статистика, хранилище и экспорт; доступен в расширенном режиме.\n\n"
            "*Как вводить теги*\n"
            "Разделяйте теги пробелами, а слова внутри одного тега соединяйте `_`. "
            "Чтобы исключить тег, поставьте перед ним `-`.\n"
            "Пример: `blue_hair 1girl -comic`\n\n"
            "*Управление интерфейсом*\n"
            "В простом режиме показаны только основные кнопки. Расширенный режим включает "
            "быстрый доступ к подборкам, подпискам и данным. Переключение находится в настройках.\n"
            "Во время любого ввода используйте кнопку «❌ Отмена» или команду `/cancel`.\n\n"
            "*Команды*\n"
            "`/start` — обновить меню и открыть быстрый старт\n"
            "`/search <теги>` — найти пост\n"
            "`/random` — случайный пост\n"
            "`/gallery <теги>` — создать подборку\n"
            "`/favorites`, `/collections`, `/later` — разделы библиотеки\n"
            "`/subscriptions` — подписки\n"
            "`/presets` — сохранённые запросы\n"
            "`/blacklist` — чёрный список\n"
            "`/settings` — настройки\n"
            "`/stats`, `/storage` — данные пользователя\n"
            "`/tags <запрос>` — подобрать теги\n"
            "`/id <номер>` — открыть пост по ID\n"
            "`/cancel` — отменить текущее действие\n\n"
            "⚠️ Бот предназначен только для пользователей 18+.",
            reply_markup=get_help_keyboard(settings.get("interface_mode", "simple")),
            parse_mode="Markdown",
        )


async def select_post_matching_preferences(
    result: dict | None,
    settings: dict,
    fetch_replacement,
    excluded_post_ids: set[int],
    *,
    max_replacements: int = 4,
) -> dict | None:
    """Return only a matching post, including after the final retry."""
    for attempt in range(max_replacements + 1):
        if not result or post_matches_preferences(result, settings):
            return result
        try:
            excluded_post_ids.add(int(result.get("id")))
        except (TypeError, ValueError):
            pass
        if attempt == max_replacements:
            break
        result = await fetch_replacement()
    return None


async def send_random_image(
    message, user_id: int, *, expected_generation: int | None = None
):
    """Отправка случайного изображения без поисковых тегов."""
    try:
        callback_issuer = callback_issuer_for(user_id, expected_generation)
    except StaleCallbackIssuer:
        return False
    blacklist = await get_user_blacklist(user_id)
    settings = await get_user_settings(user_id)

    if await reserve_search_cooldown(user_id):
        await message.reply_text(
            f"⏳ Подождите {SEARCH_COOLDOWN_SECONDS} сек. перед следующим поиском.",
            reply_markup=get_main_keyboard(),
        )
        return False

    status_msg = await message.reply_text("🎲 Ищу случайную картинку...")
    excluded_post_ids = await get_sent_post_ids(user_id)

    started_at = time.monotonic()
    try:
        result = await api.get_global_random_image(blacklist, excluded_post_ids)
        filter_settings = normalize_feature_settings(settings)
        result = await select_post_matching_preferences(
            result,
            filter_settings,
            lambda: api.get_global_random_image(blacklist, excluded_post_ids),
            excluded_post_ids,
        )
        logger.info(
            "Random post source=api user=%s post=%s elapsed=%.3fs",
            user_id,
            result.get("id") if result else None,
            time.monotonic() - started_at,
        )
    except APITemporaryError:
        await status_msg.delete()
        logger.warning(
            "Temporary Rule34 API error during random search user=%s elapsed=%.3fs",
            user_id,
            time.monotonic() - started_at,
        )
        await message.reply_text(
            "⚠️ Rule34 сейчас отвечает слишком долго. Попробуйте ещё раз чуть позже.",
            reply_markup=get_main_keyboard(),
        )
        return False

    await status_msg.delete()

    if not result:
        logger.info(
            "Random post source=api_empty user=%s post=None elapsed=%.3fs",
            user_id,
            time.monotonic() - started_at,
        )
        await message.reply_text(
            "❌ Не удалось найти случайную картинку с учётом чёрного списка и фильтров.",
            reply_markup=get_main_keyboard(),
        )
        return False

    await remember_and_cache_post(result)
    post_id = result.get("id", 0)

    caption = ""
    if settings.get("show_caption", True):
        caption = await build_caption(settings, result)

    try:
        keyboard = get_random_image_keyboard(
            post_id,
            should_show_tags_button(settings),
            side_effect_callback=callback_issuer,
        )
    except StaleCallbackIssuer:
        keyboard = None
    delivered = await send_post_media(
        message,
        result,
        caption,
        keyboard,
        settings=settings,
    )
    if delivered and post_id:
        await mark_post_sent(user_id, int(post_id))

    return delivered


async def send_image(
    message,
    user_id: int,
    tags: str,
    edit: bool = False,
    is_more: bool = False,
    is_subscription: bool = False,
    expected_generation: int | None = None,
):
    """Отправка изображения"""
    try:
        callback_issuer = callback_issuer_for(user_id, expected_generation)
    except StaleCallbackIssuer:
        return False
    if not is_subscription and not str(tags).strip():
        await message.reply_text(
            "❌ Укажите хотя бы один тег для поиска.",
            reply_markup=get_main_keyboard(),
        )
        return False

    blacklist = await get_user_blacklist(user_id)
    settings = await get_user_settings(user_id)

    if not is_subscription and await reserve_search_cooldown(user_id):
        await message.reply_text(
            f"⏳ Подождите {SEARCH_COOLDOWN_SECONDS} сек. перед следующим поиском.",
            reply_markup=get_main_keyboard(),
        )
        return False

    status_msg = None
    if not is_subscription:  # Не показываем статус для подписок
        status_msg = await message.reply_text("🔍 Ищу...")

    excluded_post_ids = await get_sent_post_ids(user_id)

    started_at = time.monotonic()
    try:
        # Если это кнопка "ещё", используем улучшенную логику
        if is_more:
            result = await api.get_next_image(user_id, tags, blacklist, excluded_post_ids)
            filter_settings = normalize_feature_settings(settings)
            result = await select_post_matching_preferences(
                result,
                filter_settings,
                lambda: api.get_next_image(
                    user_id, tags, blacklist, excluded_post_ids
                ),
                excluded_post_ids,
            )
        else:
            result = await api.get_random_image(tags, blacklist, excluded_post_ids)
            filter_settings = normalize_feature_settings(settings)
            result = await select_post_matching_preferences(
                result,
                filter_settings,
                lambda: api.get_random_image(tags, blacklist, excluded_post_ids),
                excluded_post_ids,
            )
            # Сохраняем историю поиска для кнопки "ещё"
            if result:
                await api.save_search_state(user_id, tags, blacklist, result.get("id"))
        logger.info(
            "Search post source=api user=%s tags=%r post=%s elapsed=%.3fs more=%s subscription=%s",
            user_id,
            tags,
            result.get("id") if result else None,
            time.monotonic() - started_at,
            is_more,
            is_subscription,
        )
    except APITemporaryError:
        if status_msg:
            await status_msg.delete()
        logger.warning(
            "Temporary Rule34 API error during user search user=%s tags=%r elapsed=%.3fs",
            user_id,
            tags,
            time.monotonic() - started_at,
        )
        if not is_subscription:
            await message.reply_text(
                "⚠️ Rule34 сейчас отвечает слишком долго. Попробуйте ещё раз чуть позже.",
                reply_markup=get_main_keyboard(),
            )
        return False

    if status_msg:
        await status_msg.delete()

    if result:
        await remember_and_cache_post(result)
        await save_user_query(user_id, tags)

        post_id = result.get("id", 0)

        # Строим описание на основе настроек
        caption = ""
        if settings.get("show_caption", True):
            caption = await build_caption(settings, result, tags, is_subscription)

        # Для подписок не добавляем кнопку подписки (чтобы избежать рекурсии)
        show_tags_button = should_show_tags_button(settings)
        try:
            if is_subscription:
                keyboard = get_subscription_image_keyboard(
                    post_id,
                    tags,
                    show_tags_button,
                    side_effect_callback=callback_issuer,
                )
            else:
                keyboard = get_image_keyboard(
                    post_id,
                    tags,
                    show_tags_button,
                    side_effect_callback=callback_issuer,
                )
        except StaleCallbackIssuer:
            keyboard = None

        delivered = await send_post_media(
            message, result, caption, keyboard, settings=settings
        )
        if delivered and post_id:
            await mark_post_sent(user_id, int(post_id))

        return delivered
    else:
        if not is_subscription:
            if is_more:
                await message.reply_text(
                    "❌ Больше не найдено постов по этому запросу.\n\n"
                    "Попробуйте:\n"
                    "• Другие теги\n"
                    "• Новый поиск",
                    reply_markup=get_main_keyboard(),
                    parse_mode="Markdown",
                )
            else:
                await message.reply_text(
                    "❌ Ничего не найдено по запросу.\n\n"
                    "Попробуйте:\n"
                    "• Другие теги\n"
                    "• Проверить правильность написания\n"
                    "• Использовать `/tags` для поиска тегов",
                    reply_markup=get_main_keyboard(),
                    parse_mode="Markdown",
                )
        return False


async def send_post_media(
    message, post: dict, caption: str = "", keyboard=None, settings: dict | None = None,
    raise_on_timeout: bool = False,
):
    if settings:
        post = prepare_post_quality(post, normalize_feature_settings(settings))
    return await send_post_media_with_retries(
        message,
        post,
        caption,
        keyboard,
        retries=MEDIA_SEND_RETRIES,
        has_spoiler=should_spoiler(settings, post),
        raise_on_timeout=raise_on_timeout,
    )


async def send_post_media_to_chat(
    bot, chat_id: int, post: dict, caption: str = "", keyboard=None,
    settings: dict | None = None, raise_on_timeout: bool = False,
    before_send=None,
):
    if settings:
        post = prepare_post_quality(post, normalize_feature_settings(settings))
    return await send_post_media_to_chat_with_retries(
        bot,
        chat_id,
        post,
        caption,
        keyboard,
        retries=MEDIA_SEND_RETRIES,
        has_spoiler=should_spoiler(settings, post),
        raise_on_timeout=raise_on_timeout,
        before_send=before_send,
    )


async def ensure_favorite_original_url(post: dict) -> dict:
    if post.get("file_url"):
        return post

    try:
        post_id = int(post.get("id"))
    except (TypeError, ValueError):
        return post

    fresh_post = await api.get_post_by_id(post_id)
    if fresh_post:
        await remember_and_cache_post(fresh_post)
        return fresh_post
    return post


async def load_zip_export_source(
    user_id: int, source_kind: str, collection_id: int | None
) -> ZipExportSource:
    if source_kind == "favorites":
        candidates = await get_favorites(
            user_id, limit=ZIP_EXPORT_MAX_FILES + 1
        )
        return ZipExportSource(
            title="Избранное",
            posts=candidates[:ZIP_EXPORT_MAX_FILES],
            truncated=len(candidates) > ZIP_EXPORT_MAX_FILES,
        )

    if collection_id is None:
        return ZipExportSource(title="Коллекция", posts=[])
    collection = await get_favorite_collection(user_id, collection_id)
    if not collection:
        return ZipExportSource(title="Коллекция", posts=[])
    candidates = await get_collection_favorites(
        user_id, collection_id, limit=ZIP_EXPORT_MAX_FILES + 1
    )
    return ZipExportSource(
        title=collection["name"],
        posts=candidates[:ZIP_EXPORT_MAX_FILES],
        truncated=len(candidates) > ZIP_EXPORT_MAX_FILES,
    )


def get_zip_export_cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛑 Отменить экспорт", callback_data=f"zip_cancel_{job_id}")
    ]])


async def show_zip_enqueue_rejection(message, result) -> None:
    if result.status == "duplicate":
        position = result.position
        text = (
            "📦 ZIP-экспорт уже выполняется."
            if position == 0
            else f"📦 ZIP-экспорт уже ожидает в очереди. Позиция: {position}."
        )
    elif result.status == "queue_full":
        text = "⏳ Очередь ZIP-экспорта заполнена. Попробуйте позже."
    elif result.status == "cooldown":
        minutes = max(1, ((result.retry_after_seconds or 1) + 59) // 60)
        text = f"📦 Экспорт уже недавно запускался. Попробуйте через {minutes} мин."
    elif result.status == "cancelled":
        text = "🛑 ZIP-экспорт отменён."
    else:
        text = "🛑 ZIP-экспорт сейчас недоступен: бот завершает работу."
    await message.reply_text(text)


async def enqueue_favorites_zip_export(message, user_id: int) -> None:
    if zip_export_manager is None:
        await message.reply_text("❌ ZIP-экспорт временно недоступен.")
        return
    result = await zip_export_manager.enqueue_favorites(message, user_id)
    if result.status != "queued":
        await show_zip_enqueue_rejection(message, result)


async def enqueue_collection_zip_export(message, user_id: int, collection_id: int) -> None:
    if zip_export_manager is None:
        await message.reply_text("❌ ZIP-экспорт временно недоступен.")
        return
    result = await zip_export_manager.enqueue_collection(
        message, user_id, collection_id
    )
    if result.status != "queued":
        await show_zip_enqueue_rejection(message, result)


async def show_subscription_posts_menu(
    message, user_id: int, sub_query: str, token: str, edit: bool = True
):
    total = await count_subscription_posts(user_id, sub_query)
    posts = await get_subscription_posts(user_id, sub_query, limit=20)
    if total <= 0:
        text = (
            f"⭐ Для подписки `{md_code(sub_query)}` пока нет избранных постов.\n\n"
            "Нажмите `⭐ В избранное` под постом из этой подписки, и он появится здесь."
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("◀️ Назад", callback_data="sub_manage")]]
        )
    else:
        text = (
            f"⭐ *Избранное подписки* `{md_code(sub_query)}`: {total}\n\n"
            "Выберите номер или откройте просмотр всех."
        )
        rows = []
        for row_start in range(0, len(posts), 5):
            row = []
            for index in range(row_start, min(row_start + 5, len(posts))):
                row.append(
                    InlineKeyboardButton(
                        str(index + 1), callback_data=f"sub_one_{token}_{index}"
                    )
                )
            rows.append(row)

        rows.append(
            [InlineKeyboardButton(
                "🖼 Галерея", callback_data=f"sub_all_{token}")]
        )
        rows.append([InlineKeyboardButton(
            "◀️ Назад", callback_data="sub_manage")])
        keyboard = InlineKeyboardMarkup(rows)

    if edit:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def send_subscription_post_by_index(
    message,
    user_id: int,
    sub_query: str,
    index: int,
    *,
    issuer: OneShotCallbackIssuer,
):
    total = await count_subscription_posts(user_id, sub_query)
    if index < 0 or index >= total:
        await message.reply_text("❌ Пост не найден. Откройте список заново.")
        return

    post = await get_subscription_post_by_index(user_id, sub_query, index)
    if not post:
        await message.reply_text("❌ Пост не найден. Откройте список заново.")
        return

    settings = await get_user_settings(user_id)
    caption = build_subscription_gallery_caption(
        sub_query, post, index, total)
    await send_post_media(
        message, post, caption, get_subscription_image_keyboard(
            post.get("id", 0),
            show_tags_button=should_show_tags_button(settings),
            side_effect_callback=issuer,
        ), settings=settings
    )


async def send_subscription_gallery(
    message,
    user_id: int,
    sub_query: str,
    token: str,
    index: int = 0,
    *,
    issuer: OneShotCallbackIssuer,
):
    total = await count_subscription_posts(user_id, sub_query)
    if total <= 0:
        await message.reply_text("❌ Для этой подписки пока нет избранных постов.")
        return

    index = max(0, min(index, total - 1))
    post = await get_subscription_post_by_index(user_id, sub_query, index)
    if not post:
        await message.reply_text("❌ Для этой подписки пока нет избранных постов.")
        return

    settings = await get_user_settings(user_id)
    caption = build_subscription_gallery_caption(
        sub_query, post, index, total)
    await send_post_media(
        message,
        post,
        caption,
        get_subscription_gallery_keyboard(
            token,
            index,
            total,
            post.get("id", 0),
            should_show_tags_button(settings),
            side_effect_callback=issuer,
        ),
        settings=settings,
    )


async def edit_subscription_gallery(
    query,
    user_id: int,
    sub_query: str,
    token: str,
    index: int,
    *,
    issuer: OneShotCallbackIssuer,
):
    total = await count_subscription_posts(user_id, sub_query)
    if total <= 0:
        await query.message.reply_text(
            "❌ Для этой подписки больше нет избранных постов."
        )
        return

    index = max(0, min(index, total - 1))
    post = await get_subscription_post_by_index(user_id, sub_query, index)
    if not post:
        await query.message.reply_text(
            "❌ Для этой подписки больше нет избранных постов."
        )
        return

    settings = await get_user_settings(user_id)
    caption = build_subscription_gallery_caption(
        sub_query, post, index, total)
    keyboard = get_subscription_gallery_keyboard(
        token,
        index,
        total,
        post.get("id", 0),
        should_show_tags_button(settings),
        side_effect_callback=issuer,
    )
    try:
        await query.edit_message_media(
            media=media_from_post(post, caption, should_spoiler(settings, post)),
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"Ошибка обновления галереи подписки: {e}")
        await send_post_media(query.message, post, caption, keyboard, settings=settings)


async def send_favorites_gallery(
    message,
    user_id: int,
    page: int = 0,
    *,
    issuer: OneShotCallbackIssuer,
):
    total = await count_favorites(user_id)
    if total <= 0:
        await message.reply_text("❌ Избранное пока пустое.")
        return False

    total_pages = max(1, (total + FAVORITES_GALLERY_PAGE_SIZE - 1) // FAVORITES_GALLERY_PAGE_SIZE)
    page = max(0, min(int(page), total_pages - 1))
    favorites = await get_favorites(
        user_id,
        limit=FAVORITES_GALLERY_PAGE_SIZE,
        offset=page * FAVORITES_GALLERY_PAGE_SIZE,
    )
    if not favorites:
        await message.reply_text("❌ Избранное пока пустое.")
        return False

    settings = normalize_feature_settings(await get_user_settings(user_id))
    prepared = prepare_gallery_album_posts(
        favorites, settings, FAVORITES_GALLERY_PAGE_SIZE
    )
    prepared_ids = {int(post.get("id") or 0) for post in prepared}
    standalone_posts = [
        prepare_post_quality(post, settings)
        for post in favorites
        if int(post.get("id") or 0) not in prepared_ids
    ]
    if not prepared:
        prepared, standalone_posts = standalone_posts, []

    first_number = page * FAVORITES_GALLERY_PAGE_SIZE + 1
    last_number = min(first_number + len(favorites) - 1, total)
    caption = (
        f"⭐ Избранное · страница {page + 1}/{total_pages}\n"
        f"Посты {first_number}–{last_number} из {total}"
    )
    delivered_posts = []
    rejected_posts = []
    can_group = len(prepared) > 1 and all(
        media_group_compatible_url(post.get("file_url", "")) for post in prepared
    )
    if can_group:
        delivered_posts, rejected_posts = await send_resilient_media_group(
            message,
            prepared,
            settings,
            caption,
            log_context="Favorites gallery",
        )

    sequential_posts = standalone_posts + (
        rejected_posts if can_group else prepared
    )
    for index, post in enumerate(sequential_posts):
        delivered = await send_post_media(
            message,
            post,
            caption if not delivered_posts and index == 0 else "",
            get_image_keyboard(
                int(post.get("id") or 0),
                show_tags_button=should_show_tags_button(settings),
                side_effect_callback=issuer,
            ),
            settings=settings,
        )
        if delivered:
            delivered_posts.append(post)

    await message.reply_text(
        f"⭐ Показано: {len(delivered_posts)} · "
        f"страница {page + 1}/{total_pages}",
        reply_markup=get_favorites_album_keyboard(page, total_pages),
    )
    return bool(delivered_posts)


async def edit_favorites_gallery(
    query,
    user_id: int,
    index: int,
    *,
    issuer: OneShotCallbackIssuer,
):
    total = await count_favorites(user_id)
    if total <= 0:
        await query.message.reply_text("❌ В избранном больше нет постов.")
        return

    index = max(0, min(index, total - 1))
    post = await get_favorite_by_index(user_id, index)
    if not post:
        await query.message.reply_text("❌ В избранном больше нет постов.")
        return

    settings = await get_user_settings(user_id)
    caption = build_favorites_gallery_caption(post, index, total)
    keyboard = get_favorites_gallery_keyboard(
        index,
        total,
        post.get("id", 0),
        should_show_tags_button(settings),
        side_effect_callback=issuer,
    )
    try:
        await query.edit_message_media(
            media=media_from_post(post, caption, should_spoiler(settings, post)),
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"Ошибка обновления галереи избранного: {e}")
        await send_post_media(query.message, post, caption, keyboard, settings=settings)


async def show_history(message, user_id: int, edit: bool = False):
    history = await get_search_history(user_id)
    if not history:
        text = (
            "Главная → Поиск → История\n\n"
            "История пока пустая. Выполните первый поиск — запрос появится здесь."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 Начать поиск", callback_data="search")],
            [InlineKeyboardButton("⬅️ К поиску", callback_data="search_hub")],
        ])
    else:
        text = "🕘 *Последние запросы:*\n\n" + "\n".join(
            f"• `{md_code(item)}`" for item in history
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        item[:40], callback_data=store_callback_payload(
                            "hist", item)
                    )
                ]
                for item in history[:8]
            ]
            + [[InlineKeyboardButton("◀️ Назад", callback_data="back")]]
        )

    if edit:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def show_favorites(message, user_id: int, edit: bool = False):
    total = await count_favorites(user_id)
    if total == 0:
        text = (
            "Главная → Библиотека → Избранное\n\n"
            "Здесь пока ничего нет. Найдите пост и нажмите «⭐ В избранное»."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 Найти первый пост", callback_data="search")],
            [InlineKeyboardButton("🎲 Показать случайное", callback_data="random")],
            [InlineKeyboardButton("⬅️ В библиотеку", callback_data="library")],
        ])
    else:
        text = f"⭐ *Избранное:* {total}"
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🖼 Галерея", callback_data="fav_gallery")],
                [InlineKeyboardButton("📋 Список", callback_data="fav_list")],
                [InlineKeyboardButton("🔎 Найти по тегу", callback_data="fav_find")],
                [InlineKeyboardButton("🗂 Коллекции", callback_data="fav_collections")],
                [InlineKeyboardButton("📦 Скачать ZIP", callback_data="fav_export")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back")],
            ]
        )

    if edit:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def show_favorites_list(
    message,
    user_id: int,
    edit: bool = False,
    page: int = 0,
    tag_filter: str = "",
):
    total = await count_favorites(user_id, tag_filter=tag_filter)
    if total == 0:
        if tag_filter:
            text = f"🔎 В избранном нет постов с тегом `{md_code(tag_filter)}`."
        else:
            text = "⭐ Избранное пока пустое.\n\n" + await build_main_menu_text(user_id)
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("◀️ Назад", callback_data="favorites")]]
        )
    else:
        page = clamp_page(page, total)
        favorites = await get_favorites(
            user_id,
            limit=FAVORITES_PAGE_SIZE,
            offset=page * FAVORITES_PAGE_SIZE,
            tag_filter=tag_filter,
        )
        total_pages = (total - 1) // FAVORITES_PAGE_SIZE + 1
        lines = []
        keyboard_rows = []
        for favorite in favorites:
            post_id = favorite["id"]
            tags = favorite.get("tags", "")
            if len(tags) > 60:
                tags = tags[:60] + "..."
            lines.append(
                f"• `{md_code(post_id)}` rating: {md_text(favorite.get('rating', ''))} "
                f"score: {favorite.get('score', 0)}\n`{md_code(tags)}`"
            )
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        f"📤 {post_id}", callback_data=f"fav_open_{post_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ Удалить", callback_data=f"fav_remove_{post_id}_{page}"
                    ),
                ]
            )

        title = (
            f"🔎 *Избранное по тегу* `{md_code(tag_filter)}`"
            if tag_filter
            else "📋 *Список избранного*"
        )
        text = f"{title}: {total} (стр. {page + 1}/{total_pages})\n\n" + "\n".join(lines)
        if total_pages > 1:
            prev_page = clamp_page(page - 1, total)
            next_page = clamp_page(page + 1, total)
            if tag_filter:
                prev_callback = store_callback_payload(
                    "fav_tag_page", f"{tag_filter}\n{prev_page}"
                )
                next_callback = store_callback_payload(
                    "fav_tag_page", f"{tag_filter}\n{next_page}"
                )
            else:
                prev_callback = f"fav_list_page_{prev_page}"
                next_callback = f"fav_list_page_{next_page}"
            keyboard_rows.append(
                [
                    InlineKeyboardButton("◀️ Назад", callback_data=prev_callback),
                    InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"),
                    InlineKeyboardButton("Вперед ▶️", callback_data=next_callback),
                ]
            )
        keyboard_rows.append(
            [InlineKeyboardButton("🖼 Галерея", callback_data="fav_gallery")]
        )
        if not tag_filter:
            keyboard_rows.append(
                [InlineKeyboardButton("📦 Скачать ZIP", callback_data="fav_export")]
            )
        keyboard_rows.append(
            [InlineKeyboardButton("◀️ Назад", callback_data="favorites")])
        keyboard = InlineKeyboardMarkup(keyboard_rows)

    if edit:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def send_search_gallery(
    message,
    user_id: int,
    tags: str,
    page: int = 0,
    *,
    expected_generation: int | None = None,
):
    try:
        callback_issuer = callback_issuer_for(user_id, expected_generation)
    except StaleCallbackIssuer:
        return False
    settings = normalize_feature_settings(await get_user_settings(user_id))
    blacklist = await get_user_blacklist(user_id)
    excluded = await get_sent_post_ids(user_id)
    try:
        posts = await api.search(
            tags=tags,
            blacklist=blacklist,
            limit=100,
            pid=max(0, page),
            allow_blacklist_only=not tags.strip(),
        )
    except APITemporaryError:
        await message.reply_text("⚠️ Rule34 временно недоступен. Попробуйте позже.")
        return False

    candidates = filter_and_sort_posts(posts or [], settings, excluded)
    if not candidates:
        await message.reply_text(
            "❌ По текущим фильтрам ничего не найдено. Попробуйте ослабить фильтры.",
            reply_markup=await get_user_settings_keyboard(user_id),
        )
        return False

    if settings["media_type"] == "animations":
        prepared = [
            prepare_post_quality(post, settings)
            for post in candidates[: settings["gallery_size"]]
        ]
    else:
        prepared = prepare_gallery_album_posts(
            candidates, settings, settings["gallery_size"]
        )
        if not prepared:
            # A GIF-only result without static previews cannot be sent as an album.
            prepared = [
                prepare_post_quality(post, settings)
                for post in candidates[: settings["gallery_size"]]
            ]
    can_group = len(prepared) > 1 and all(
        media_group_compatible_url(post.get("file_url", ""))
        for post in prepared
    )
    source_posts = {
        str(post.get("id")): post
        for post in candidates
        if post.get("id") is not None
    }
    for post in prepared:
        # Delivery preparation can replace the original URL with a static GIF
        # preview or a lower-quality variant. Keep canonical API URLs in cache.
        await remember_and_cache_post(source_posts.get(str(post.get("id")), post))
    delivered_ids = []
    if can_group:
        delivered_posts, prepared = await send_resilient_media_group(
            message,
            prepared,
            settings,
            f"🖼 Галерея: `{md_code(tags or 'random')}`",
        )
        delivered_ids = [int(post["id"]) for post in delivered_posts]
        if delivered_ids:
            runtime_metrics.increment("gallery_albums")
            runtime_metrics.increment("gallery_items", len(delivered_ids))

    if not delivered_ids:
        for post in prepared:
            try:
                keyboard = get_image_keyboard(
                    int(post.get("id") or 0),
                    query=tags,
                    show_tags_button=should_show_tags_button(settings),
                    side_effect_callback=callback_issuer,
                )
            except StaleCallbackIssuer:
                keyboard = None
            delivered = await send_post_media(
                message,
                post,
                keyboard=keyboard,
                settings=settings,
            )
            if delivered:
                delivered_ids.append(int(post["id"]))

    for post_id in delivered_ids:
        await mark_post_sent(user_id, post_id)
    if tags:
        await save_user_query(user_id, tags)
    next_callback = store_callback_payload(
        "gallery_next", json.dumps({"tags": tags, "page": page + 1})
    )
    previous_callback = None
    if page > 0:
        previous_callback = store_callback_payload(
            "gallery_next", json.dumps({"tags": tags, "page": page - 1})
        )
    try:
        bulk_callback = (
            callback_issuer.payload(
                "gallery_bulk_fav",
                ",".join(str(post_id) for post_id in delivered_ids),
            )
            if delivered_ids
            else None
        )
        collection_callback = (
            callback_issuer.payload(
                "gallery_collection",
                ",".join(str(post_id) for post_id in delivered_ids),
            )
            if delivered_ids
            else None
        )
    except StaleCallbackIssuer:
        bulk_callback = None
        collection_callback = None
    await message.reply_text(
        f"Показано: {len(delivered_ids)}. Страница источника: {page + 1}.",
        reply_markup=get_gallery_result_keyboard(
            next_callback,
            previous_callback,
            bulk_callback,
            store_callback_payload("preset_from", tags) if tags else None,
            store_callback_payload("subscribe", tags) if tags else None,
            collection_callback,
        ),
    )
    return bool(delivered_ids)


def gallery_settings_text(settings: dict) -> str:
    settings = normalize_feature_settings(settings)
    return (
        "🖼 *Галерея и фильтры*\n\n"
        f"Сортировка: `{md_code(settings['gallery_sort'])}`\n"
        f"Rating: `{md_code(settings['rating_filter'])}`\n"
        f"Тип: `{md_code(settings['media_type'])}`\n"
        f"Ориентация: `{md_code(settings['orientation'])}`\n"
        f"Минимум: `{settings['min_width']}×{settings['min_height']}`\n"
        f"Размер альбома: `{settings['gallery_size']}`"
    )


def quality_settings_text(settings: dict) -> str:
    settings = normalize_feature_settings(settings)
    return (
        "📦 *Качество медиа*\n\n"
        f"Режим: `{md_code(settings['quality_mode'])}`\n"
        f"Максимальный размер оригинала в auto: `{settings['max_file_mb']} MiB`\n\n"
        "Auto предпочитает оригинал, но выбирает sample для слишком больших файлов."
    )


async def show_collections(message, user_id: int, edit: bool = False):
    collections = await get_favorite_collections(user_id)
    rows = []
    lines = []
    for collection in collections:
        lines.append(f"• `{md_code(collection['name'])}` — {collection['count']}")
        rows.append([
            InlineKeyboardButton(
                f"🗂 {collection['name'][:24]} ({collection['count']})",
                callback_data=f"col_open_{collection['id']}",
            ),
            InlineKeyboardButton("✏️", callback_data=f"col_rename_{collection['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"col_delete_{collection['id']}"),
        ])
    rows.append([InlineKeyboardButton("➕ Создать", callback_data="col_create")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="favorites")])
    text = "Главная → Библиотека → Коллекции"
    if lines:
        text += "\n\n" + "\n".join(lines)
    else:
        text += (
            "\n\nКоллекций пока нет. Создайте первую, чтобы группировать "
            "избранное по темам."
        )
    kwargs = {"reply_markup": InlineKeyboardMarkup(rows), "parse_mode": "Markdown"}
    if edit:
        await message.edit_text(text, **kwargs)
    else:
        await message.reply_text(text, **kwargs)


async def show_collection(
    message,
    user_id: int,
    collection_id: int,
    index: int = 0,
    *,
    issuer: OneShotCallbackIssuer | None = None,
):
    issuer = issuer or callback_issuer_for(user_id)
    collection = await get_favorite_collection(user_id, collection_id)
    if not collection:
        await message.reply_text("❌ Коллекция не найдена.")
        return
    total = await count_collection_favorites(user_id, collection_id)
    if not total:
        await message.reply_text(
            f"🗂 Коллекция «{collection['name']}» пуста.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Коллекции", callback_data="fav_collections")]
            ]),
        )
        return
    index = max(0, min(index, total - 1))
    posts = await get_collection_favorites(user_id, collection_id, limit=1, offset=index)
    post = posts[0]
    note = await get_favorite_note(user_id, int(post["id"]))
    caption = f"🗂 *{md_text(collection['name'])}* — {index + 1}/{total}"
    if note:
        caption += f"\n📝 {md_text(note)}"
    prev_index = (index - 1) % total
    next_index = (index + 1) % total
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("◀️", callback_data=f"col_page_{collection_id}_{prev_index}"),
            InlineKeyboardButton(f"{index + 1}/{total}", callback_data="noop"),
            InlineKeyboardButton("▶️", callback_data=f"col_page_{collection_id}_{next_index}"),
        ],
        [
            InlineKeyboardButton(
                "📝 Заметка",
                callback_data=issuer.side_effect(f"fav_note_{post['id']}"),
            ),
            InlineKeyboardButton(
                "➖ Из коллекции",
                callback_data=issuer.side_effect(
                    f"col_remove_{collection_id}_{post['id']}_{index}"
                ),
            ),
        ],
        [InlineKeyboardButton("📦 ZIP коллекции", callback_data=f"col_export_{collection_id}")],
        [InlineKeyboardButton("◀️ Коллекции", callback_data="fav_collections")],
    ])
    settings = await get_user_settings(user_id)
    await send_post_media(message, post, caption, keyboard, settings=settings)


async def show_collection_picker(
    message,
    user_id: int,
    post_id: int,
    *,
    issuer: OneShotCallbackIssuer | None = None,
):
    issuer = issuer or callback_issuer_for(user_id)
    collections = await get_favorite_collections(user_id)
    if not collections:
        await message.reply_text(
            "Сначала создайте коллекцию.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Создать коллекцию", callback_data="col_create")]
            ]),
        )
        return
    rows = [[InlineKeyboardButton(
        f"🗂 {item['name'][:28]}",
        callback_data=issuer.side_effect(f"col_add_{item['id']}_{post_id}"),
    )] for item in collections]
    await message.reply_text("Выберите коллекцию:", reply_markup=InlineKeyboardMarkup(rows))


async def show_user_stats(message, user_id: int):
    stats = await get_user_activity_stats(user_id)
    top_queries = "\n".join(
        f"• `{md_code(query)}` — {count}" for query, count in stats["top_queries"]
    ) or "• пока нет"
    top_tags = ", ".join(
        f"`{md_code(tag)}` ({count})" for tag, count in stats["top_tags"]
    ) or "пока нет"
    text = (
        "📊 *Ваша статистика*\n\n"
        f"Просмотрено: {stats['viewed_total']} (7 дней: {stats['viewed_week']}, 30 дней: {stats['viewed_month']})\n"
        f"В избранном: {stats['favorites_total']} (7 дней: {stats['favorites_week']}, 30 дней: {stats['favorites_month']})\n"
        f"Поисков: {stats['searches_total']} (7 дней: {stats['searches_week']}, 30 дней: {stats['searches_month']})\n"
        f"Активных подписок: {stats['subscriptions_active']}\n\n"
        f"*Частые запросы:*\n{top_queries}\n\n"
        f"*Теги избранного:* {top_tags}"
    )
    await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🧹 Очистить статистику", callback_data="stats_clear_confirm")],
            [InlineKeyboardButton("⬅️ Мои данные", callback_data="my_data")],
        ]),
        parse_mode="Markdown",
    )


async def show_search_presets(
    message, user_id: int, *, issuer: OneShotCallbackIssuer | None = None
):
    issuer = issuer or callback_issuer_for(user_id)
    presets = await get_search_presets(user_id)
    rows = []
    lines = []
    for item in presets:
        lines.append(f"• *{md_text(item['name'])}*: `{md_code(item['query'])}`")
        rows.append([
            InlineKeyboardButton("▶️ " + item["name"][:24], callback_data=f"preset_run_{item['id']}"),
            InlineKeyboardButton(
                "🗑",
                callback_data=issuer.side_effect(f"preset_del_{item['id']}"),
            ),
        ])
    rows.extend([
        [InlineKeyboardButton("➕ Сохранить текущий поиск", callback_data="preset_save_current")],
        [InlineKeyboardButton("🧩 Конструктор", callback_data="search_builder")],
        [InlineKeyboardButton("◀️ Меню", callback_data="back")],
    ])
    text = "Главная → Поиск → Сохранённые запросы"
    text += "\n\n" + (
        "\n".join(lines)
        if lines
        else "Сохранённых запросов пока нет. Выполните поиск и сохраните его для быстрого запуска."
    )
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")


async def show_read_later(
    message,
    user_id: int,
    *,
    issuer: OneShotCallbackIssuer | None = None,
):
    callback_issuer = issuer or callback_issuer_for(user_id)
    posts = await get_read_later(user_id, limit=20)
    if not posts:
        await message.reply_text(
            "Главная → Библиотека → На потом\n\n"
            "Список пуст. Нажимайте «🕓 На потом» под постами, которые хотите посмотреть позже.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔎 Найти пост", callback_data="search")],
                [InlineKeyboardButton("⬅️ В библиотеку", callback_data="library")],
            ]),
        )
        return
    if temporary_user_state.generation(user_id) != callback_issuer.expected_generation:
        return
    rows = []
    lines = []
    for post in posts[:10]:
        post_id = int(post.get("id") or 0)
        tags = str(post.get("tags") or "")[:45]
        lines.append(f"• `{post_id}` {md_text(tags)}")
        rows.append([
            InlineKeyboardButton(f"📤 {post_id}", callback_data=f"later_open_{post_id}"),
            InlineKeyboardButton(
                "✅ Убрать",
                callback_data=callback_issuer.side_effect(
                    f"later_del_{post_id}"
                ),
            ),
        ])
    rows.append([InlineKeyboardButton("◀️ Меню", callback_data="back")])
    await message.reply_text(
        "⏳ *Посмотреть позже*\n\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown",
    )


async def show_storage(message, user_id: int):
    stats = await get_user_storage_stats(user_id)
    text = (
        "Главная → Мои данные → Хранилище\n\n"
        f"Избранное: {stats['favorites']}\n"
        f"Коллекции: {stats['collections']}\n"
        f"История запросов: {stats['history']}\n"
        f"Просмотренные: {stats['viewed']}\n"
        f"На потом: {stats['read_later']}\n"
        f"Сохранённые запросы: {stats['presets']}\n"
        f"В дайджесте: {stats['digest']}\n"
        f"Пустые коллекции: {stats['empty_collections']}\n"
        f"Избранное без URL: {stats['favorites_without_url']}"
    )
    await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🧹 Удалить историю старше 90 дней", callback_data="storage_cleanup_90")],
            [InlineKeyboardButton("🗑 Удалить пустые коллекции", callback_data="storage_empty_collections")],
            [InlineKeyboardButton("⬅️ Мои данные", callback_data="my_data")],
        ]),
        parse_mode="Markdown",
    )


async def send_recommendations(
    message, user_id: int, *, expected_generation: int | None = None
):
    try:
        callback_issuer = callback_issuer_for(user_id, expected_generation)
    except StaleCallbackIssuer:
        return False
    profile = await get_favorite_tag_profile(user_id, limit=5)
    settings = await get_user_settings(user_id)
    excluded = set(str(settings.get("recommendation_excluded_tags", "")).split())
    profile = [item for item in profile if item[0] not in excluded]
    if not profile:
        await message.reply_text(
            "Главная → Библиотека → Рекомендации\n\n"
            "Добавьте несколько постов в избранное — после этого бот сможет подобрать похожие.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔎 Найти посты", callback_data="search")],
                [InlineKeyboardButton("⬅️ В библиотеку", callback_data="library")],
            ]),
        )
        return False
    tags = " ".join(tag for tag, _count in profile[:3])
    try:
        recommendation_rows = [
            [InlineKeyboardButton(
                f"🚫 Не рекомендовать {tag[:24]}",
                callback_data=callback_issuer.payload("rec_hide", tag),
            )]
            for tag, _count in profile[:3]
        ]
    except StaleCallbackIssuer:
        return False
    await message.reply_text(
        "✨ Подборка по частым тегам избранного: "
        + ", ".join(f"`{md_code(tag)}`" for tag, _count in profile[:3]),
        reply_markup=InlineKeyboardMarkup(recommendation_rows),
        parse_mode="Markdown",
    )
    return await send_search_gallery(
        message,
        user_id,
        tags,
        expected_generation=callback_issuer.expected_generation,
    )


async def show_favorite_search_results(message, user_id: int, text: str):
    posts = await search_favorites(user_id, text, limit=20)
    if not posts:
        await message.reply_text("🔎 В избранном ничего не найдено.")
        return
    rows = []
    lines = []
    for post in posts:
        post_id = int(post["id"])
        lines.append(f"• `{post_id}` {md_text(str(post.get('tags') or '')[:55])}")
        rows.append([InlineKeyboardButton(f"📤 Открыть {post_id}", callback_data=f"fav_open_{post_id}")])
    rows.append([InlineKeyboardButton("◀️ Избранное", callback_data="favorites")])
    await message.reply_text(
        f"🔎 *Найдено в избранном: {len(posts)}*\n\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown",
    )


def similar_query_from_post(post: dict) -> str:
    ignored = {
        "solo", "1girl", "1boy", "highres", "absurdres", "explicit", "safe",
        "questionable", "looking_at_viewer", "simple_background",
    }
    tags = [
        tag for tag in str(post.get("tags") or "").split()
        if tag.lower() not in ignored and len(tag) > 2
    ]
    return " ".join(tags[:4])


SUBSCRIPTION_OPTION_ACTIONS = (
    "rating", "type", "orientation", "resolution", "quality", "blacklist", "digest",
)
SUBSCRIPTION_OPTION_CALLBACK_PREFIXES = tuple(
    f"subopt_{action}_" for action in SUBSCRIPTION_OPTION_ACTIONS
)


def subscription_option_callback(action: str, token: str) -> str:
    if action not in SUBSCRIPTION_OPTION_ACTIONS or not token:
        raise ValueError("Invalid subscription option callback")
    return f"subopt_{action}_{token}"


def parse_subscription_option_callback(data: str) -> tuple[str, str] | None:
    parts = data.split("_", 2)
    if len(parts) != 3 or parts[0] != "subopt":
        return None
    action, token = parts[1:]
    if action not in SUBSCRIPTION_OPTION_ACTIONS or not token:
        return None
    return action, token


async def show_subscription_options(message, user_id: int, sub_query: str):
    options = await get_subscription_options(user_id, sub_query)
    rating = options.get("rating_filter", "all")
    media_type = options.get("media_type", "all")
    orientation = options.get("orientation", "any")
    resolution = f"{options.get('min_width', 0)}×{options.get('min_height', 0)}"
    quality = options.get("quality_mode", "auto")
    extra_blacklist = str(options.get("extra_blacklist", ""))
    digest = options.get("digest_mode", "instant")
    stored_callback = store_callback_payload("sub_options", sub_query)
    callback_prefix = "sub_options_"
    if not stored_callback.startswith(callback_prefix):
        raise RuntimeError("Unexpected subscription callback payload format")
    token = stored_callback[len(callback_prefix):]
    await message.reply_text(
        f"🎛 *Фильтры подписки* `{md_code(sub_query)}`",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Rating: {rating}", callback_data=subscription_option_callback("rating", token))],
            [InlineKeyboardButton(f"Тип: {media_type}", callback_data=subscription_option_callback("type", token))],
            [InlineKeyboardButton(f"Ориентация: {orientation}", callback_data=subscription_option_callback("orientation", token))],
            [InlineKeyboardButton(f"Разрешение: {resolution}", callback_data=subscription_option_callback("resolution", token))],
            [InlineKeyboardButton(f"Качество: {quality}", callback_data=subscription_option_callback("quality", token))],
            [InlineKeyboardButton(
                f"Чёрный список: {extra_blacklist[:20] or 'общий'}",
                callback_data=subscription_option_callback("blacklist", token),
            )],
            [InlineKeyboardButton(
                "📨 Дайджест" if digest == "digest" else "⚡ Сразу",
                callback_data=subscription_option_callback("digest", token),
            )],
            [InlineKeyboardButton("◀️ Подписки", callback_data="sub_manage")],
        ]),
        parse_mode="Markdown",
    )


DigestItemKey = tuple[str, int]


@dataclass
class DigestDeliveryResult:
    delivered_ids: list[DigestItemKey] = field(default_factory=list)
    failed_ids: list[DigestItemKey] = field(default_factory=list)
    ambiguous_ids: list[DigestItemKey] = field(default_factory=list)

    def add(self, bucket: str, keys) -> None:
        target = getattr(self, bucket)
        for key in keys:
            if key not in target:
                target.append(key)


class DigestDeliveryCancelled(asyncio.CancelledError):
    def __init__(self, result: DigestDeliveryResult):
        super().__init__("Digest delivery cancelled")
        self.result = result


class DigestClaimLease:
    """Keeps a claimed batch alive and stops delivery if ownership is lost."""

    HEARTBEAT_SECONDS = 60

    def __init__(self, user_id: int, claim_token: str):
        self.user_id = user_id
        self.claim_token = claim_token
        self.lost = False
        self._task: asyncio.Task | None = None

    async def start(self) -> bool:
        if not await self.ensure_owned():
            return False
        self._task = asyncio.create_task(self._heartbeat())
        return True

    async def ensure_owned(self) -> bool:
        if self.lost:
            return False
        try:
            owned = await renew_subscription_digest_claim(
                self.user_id, self.claim_token
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Failed to renew digest claim user=%s", self.user_id
            )
            owned = False
        self.lost = not owned
        return owned

    async def _heartbeat(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.HEARTBEAT_SECONDS)
                if not await self.ensure_owned():
                    return
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None


def reserve_digest_subscription_lock(
    user_id: int, query: str
) -> DigestSubscriptionLockHandle:
    key = (user_id, query)
    entry = digest_subscription_locks.get(key)
    if entry is None:
        entry = DigestSubscriptionLockEntry()
        digest_subscription_locks[key] = entry
    entry.references += 1
    return key, entry


def unreserve_digest_subscription_lock(
    handle: DigestSubscriptionLockHandle,
) -> None:
    key, entry = handle
    entry.references -= 1
    if entry.references == 0 and digest_subscription_locks.get(key) is entry:
        del digest_subscription_locks[key]


@asynccontextmanager
async def digest_subscription_lock(user_id: int, query: str):
    handle = reserve_digest_subscription_lock(user_id, query)
    entry = handle[1]
    acquired = False
    try:
        await entry.lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            entry.lock.release()
        unreserve_digest_subscription_lock(handle)


async def acquire_digest_subscription_locks(
    user_id: int, posts: list[dict]
) -> list[DigestSubscriptionLockHandle]:
    handles = [
        reserve_digest_subscription_lock(user_id, query)
        for query in sorted({digest_item_key(post)[0] for post in posts})
    ]
    acquired: list[DigestSubscriptionLockHandle] = []
    try:
        for handle in handles:
            await handle[1].lock.acquire()
            acquired.append(handle)
        return handles
    except BaseException:
        for handle in reversed(acquired):
            handle[1].lock.release()
        for handle in handles:
            unreserve_digest_subscription_lock(handle)
        raise


def release_digest_subscription_locks(
    handles: list[DigestSubscriptionLockHandle],
) -> None:
    for handle in reversed(handles):
        handle[1].lock.release()
    for handle in handles:
        unreserve_digest_subscription_lock(handle)


def digest_item_key(post: dict) -> DigestItemKey:
    stored = post.get("digest_item_key")
    if isinstance(stored, (tuple, list)) and len(stored) == 2:
        return str(stored[0]), int(stored[1])
    return str(post.get("subscription_query", "digest")), int(post.get("id") or 0)


def partition_digest_posts(posts: list[dict], settings: dict) -> tuple[list[dict], list[dict]]:
    album_posts = prepare_gallery_album_posts(posts, settings, 10)
    album_keys = {digest_item_key(post) for post in album_posts}
    standalone_posts = [post for post in posts[:10] if digest_item_key(post) not in album_keys]
    return album_posts, standalone_posts


async def send_digest_posts(
    message, user_id: int, posts: list[dict], lease: DigestClaimLease | None = None
) -> DigestDeliveryResult:
    result = DigestDeliveryResult()
    if not posts:
        await message.reply_text("📨 Дайджест пока пуст.")
        return result
    if not is_recipient_allowed(user_id, user_id, "private"):
        result.add("failed_ids", [digest_item_key(post) for post in posts[:10]])
        return result
    settings = normalize_feature_settings(await get_user_settings(user_id))
    album_posts, standalone_posts = partition_digest_posts(posts, settings)
    sequential_posts = list(standalone_posts)
    in_flight: list[DigestItemKey] = []
    if len(album_posts) > 1:
        media = []
        for index, post in enumerate(album_posts):
            caption = "📨 Дайджест подписок" if index == 0 else ""
            media.append(media_from_post(post, caption, should_spoiler(settings, post)))
        album_item_keys = [digest_item_key(post) for post in album_posts]
        if lease is not None and not await lease.ensure_owned():
            result.add("failed_ids", album_item_keys)
            result.add("failed_ids", [digest_item_key(post) for post in standalone_posts])
            return result
        in_flight = album_item_keys
        request_started = False
        try:
            async def send_album():
                nonlocal request_started
                if not is_recipient_allowed(user_id, user_id, "private"):
                    return False
                if lease is not None and not await lease.ensure_owned():
                    return False
                request_started = True
                return await message.reply_media_group(media=media)

            sent = await execute_telegram_request(
                send_album,
                operation_name="digest_reply_media_group",
                chat_id=user_id,
            )
            if sent is False:
                result.add("failed_ids", in_flight)
                result.add("failed_ids", [digest_item_key(post) for post in standalone_posts])
                return result
            result.add("delivered_ids", in_flight)
            in_flight = []
        except TimedOut:
            result.add("ambiguous_ids", in_flight)
            in_flight = []
        except RetryAfter:
            result.add("failed_ids", in_flight)
            result.add("failed_ids", [digest_item_key(post) for post in standalone_posts])
            return result
        except asyncio.CancelledError as exc:
            result.add("ambiguous_ids" if request_started else "failed_ids", in_flight)
            raise DigestDeliveryCancelled(result) from exc
        except Exception as exc:
            if isinstance(exc, NetworkError) and not isinstance(exc, BadRequest):
                result.add("ambiguous_ids", in_flight)
                in_flight = []
                sequential_posts = list(standalone_posts)
            else:
                logger.warning("Digest album failed, using sequential delivery: %s", exc)
                sequential_posts = album_posts + standalone_posts
                in_flight = []
    elif album_posts:
        sequential_posts = album_posts + standalone_posts

    try:
        for index, post in enumerate(sequential_posts):
            key = digest_item_key(post)
            if not is_recipient_allowed(user_id, user_id, "private"):
                result.add("failed_ids", [
                    digest_item_key(item) for item in sequential_posts[index:]
                ])
                return result
            if lease is not None and not await lease.ensure_owned():
                result.add("failed_ids", [
                    digest_item_key(item) for item in sequential_posts[index:]
                ])
                return result
            in_flight = [key]
            keyboard = None
            try:
                keyboard = get_subscription_image_keyboard(
                    post.get("id", 0),
                    side_effect_callback=subscription_callback_issuer_for(user_id),
                )
                delivered = await send_post_media(
                    message,
                    post,
                    caption="📨 Дайджест подписок" if index == 0 else "",
                    keyboard=keyboard,
                    settings=settings,
                    raise_on_timeout=True,
                )
            except TimedOut:
                result.add("ambiguous_ids", [key])
            except NetworkError as exc:
                if isinstance(exc, BadRequest):
                    result.add("failed_ids", [
                        digest_item_key(item) for item in sequential_posts[index:]
                    ])
                    if keyboard is not None:
                        revoke_unsent_keyboard_callbacks(keyboard)
                    logger.warning(
                        "Digest item rejected user=%s key=%s: %s",
                        user_id, key, exc,
                    )
                    return result
                result.add("ambiguous_ids", [key])
            except Exception as exc:
                result.add("failed_ids", [
                    digest_item_key(item) for item in sequential_posts[index:]
                ])
                if keyboard is not None:
                    revoke_unsent_keyboard_callbacks(keyboard)
                logger.warning(
                    "Digest item delivery failed user=%s key=%s type=%s: %s",
                    user_id, key, type(exc).__name__, exc,
                )
                return result
            else:
                result.add("delivered_ids" if delivered else "failed_ids", [key])
                if not delivered:
                    revoke_unsent_keyboard_callbacks(keyboard)
            in_flight = []
    except asyncio.CancelledError as exc:
        result.add("ambiguous_ids", in_flight)
        raise DigestDeliveryCancelled(result) from exc
    return result


async def send_digest_to_chat(
    bot, user_id: int, posts: list[dict], lease: DigestClaimLease | None = None
) -> DigestDeliveryResult:
    result = DigestDeliveryResult()
    if not posts:
        return result
    if not is_recipient_allowed(user_id, user_id, "private"):
        result.add("failed_ids", [digest_item_key(post) for post in posts[:10]])
        return result
    settings = normalize_feature_settings(await get_user_settings(user_id))
    album_posts, standalone_posts = partition_digest_posts(posts, settings)
    sequential_posts = list(standalone_posts)
    in_flight: list[DigestItemKey] = []
    if len(album_posts) > 1:
        media = [
            media_from_post(
                post,
                "📨 Дайджест подписок" if index == 0 else "",
                should_spoiler(settings, post),
            )
            for index, post in enumerate(album_posts)
        ]
        album_item_keys = [digest_item_key(post) for post in album_posts]
        request_started = False
        try:
            if lease is not None and not await lease.ensure_owned():
                result.add("failed_ids", album_item_keys)
                result.add("failed_ids", [digest_item_key(post) for post in standalone_posts])
                return result
            in_flight = album_item_keys
            async def send_album():
                nonlocal request_started
                if not is_recipient_allowed(user_id, user_id, "private"):
                    return False
                if lease is not None and not await lease.ensure_owned():
                    return False
                request_started = True
                return await bot.send_media_group(chat_id=user_id, media=media)

            sent = await execute_telegram_request(
                send_album,
                operation_name="digest_send_media_group",
                chat_id=user_id,
            )
            if sent is False:
                result.add("failed_ids", in_flight)
                result.add("failed_ids", [digest_item_key(post) for post in standalone_posts])
                return result
            result.add("delivered_ids", in_flight)
            in_flight = []
        except TimedOut:
            result.add("ambiguous_ids", in_flight)
            in_flight = []
        except RetryAfter:
            result.add("failed_ids", in_flight)
            result.add("failed_ids", [digest_item_key(post) for post in standalone_posts])
            return result
        except asyncio.CancelledError as exc:
            if in_flight and request_started:
                result.add("ambiguous_ids", in_flight)
            else:
                result.add("failed_ids", album_item_keys)
            raise DigestDeliveryCancelled(result) from exc
        except Exception as exc:
            if isinstance(exc, NetworkError) and not isinstance(exc, BadRequest):
                result.add("ambiguous_ids", in_flight)
                in_flight = []
                sequential_posts = list(standalone_posts)
            else:
                logger.warning("Scheduled digest album failed, using sequential delivery: %s", exc)
                sequential_posts = album_posts + standalone_posts
                in_flight = []
    elif album_posts:
        sequential_posts = album_posts + standalone_posts

    try:
        for index, post in enumerate(sequential_posts):
            key = digest_item_key(post)
            if not is_recipient_allowed(user_id, user_id, "private"):
                result.add("failed_ids", [
                    digest_item_key(item) for item in sequential_posts[index:]
                ])
                return result
            if lease is not None and not await lease.ensure_owned():
                result.add("failed_ids", [
                    digest_item_key(item) for item in sequential_posts[index:]
                ])
                return result
            in_flight = [key]
            keyboard = None
            try:
                async def digest_delivery_allowed():
                    if not is_recipient_allowed(user_id, user_id, "private"):
                        return False
                    return lease is None or await lease.ensure_owned()

                keyboard = get_subscription_image_keyboard(
                    post.get("id", 0),
                    side_effect_callback=subscription_callback_issuer_for(user_id),
                )
                delivered = await send_post_media_to_chat(
                    bot,
                    user_id,
                    post,
                    caption="📨 Дайджест подписок" if index == 0 else "",
                    keyboard=keyboard,
                    settings=settings,
                    raise_on_timeout=True,
                    before_send=digest_delivery_allowed,
                )
            except TimedOut:
                result.add("ambiguous_ids", [key])
            except NetworkError as exc:
                if isinstance(exc, BadRequest):
                    result.add("failed_ids", [
                        digest_item_key(item) for item in sequential_posts[index:]
                    ])
                    if keyboard is not None:
                        revoke_unsent_keyboard_callbacks(keyboard)
                    logger.warning(
                        "Scheduled digest item rejected user=%s key=%s: %s",
                        user_id, key, exc,
                    )
                    return result
                result.add("ambiguous_ids", [key])
            except Exception as exc:
                result.add("failed_ids", [
                    digest_item_key(item) for item in sequential_posts[index:]
                ])
                if keyboard is not None:
                    revoke_unsent_keyboard_callbacks(keyboard)
                logger.warning(
                    "Scheduled digest item delivery failed user=%s key=%s type=%s: %s",
                    user_id, key, type(exc).__name__, exc,
                )
                return result
            else:
                result.add("delivered_ids" if delivered else "failed_ids", [key])
                if not delivered:
                    revoke_unsent_keyboard_callbacks(keyboard)
            in_flight = []
    except asyncio.CancelledError as exc:
        result.add("ambiguous_ids", in_flight)
        raise DigestDeliveryCancelled(result) from exc
    return result


async def _cancellation_safe_db_call(coroutine):
    cleanup_task = asyncio.create_task(coroutine)
    try:
        return await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        await cleanup_task
        raise


async def cancellation_safe_digest_finish(
    user_id: int, claim_token: str, delivered_ids, ambiguous_ids=()
):
    if not ambiguous_ids:
        return await _cancellation_safe_db_call(
            finish_subscription_digest_claim(user_id, claim_token, delivered_ids)
        )
    return await _cancellation_safe_db_call(
        finish_subscription_digest_claim(
            user_id,
            claim_token,
            delivered_ids,
            ambiguous_keys=ambiguous_ids,
        )
    )


async def cancellation_safe_digest_release(user_id: int, claim_token: str):
    return await _cancellation_safe_db_call(
        release_subscription_digest_claim(user_id, claim_token)
    )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    state = None
    flow_generation = temporary_user_state.generation(user_id)
    flow_snapshot = {}
    persistent_actions = {
        PERSISTENT_SEARCH,
        LEGACY_PERSISTENT_SEARCH,
        PERSISTENT_GALLERY,
        PERSISTENT_RANDOM,
        LEGACY_PERSISTENT_RANDOM,
        PERSISTENT_FAVORITES,
        LEGACY_PERSISTENT_FAVORITES,
        PERSISTENT_SUBSCRIPTIONS,
        PERSISTENT_MENU,
        LEGACY_PERSISTENT_MENU,
    }
    if text.lower() in {"отмена", "❌ отмена", "cancel", "/cancel"}:
        await invalidate_user_flow(user_id)
        await update.message.reply_text(
            "Действие отменено.\n\n" + await build_main_menu_text(user_id),
            reply_markup=await get_user_main_keyboard(user_id),
        )
        return

    if text in persistent_actions:
        flow_generation = await invalidate_user_flow(user_id)
    else:
        state, flow_generation, flow_snapshot = await claim_user_message_state(user_id)

    if text in {PERSISTENT_SEARCH, LEGACY_PERSISTENT_SEARCH}:
        await update.message.reply_text(
            "Главная → Поиск\n\nВыберите способ поиска.",
            reply_markup=get_search_hub_keyboard(),
            parse_mode="Markdown",
        )

    elif text == PERSISTENT_GALLERY:
        await begin_user_flow(user_id, "waiting_gallery")
        await update.message.reply_text(
            "🖼 Введите теги для подборки или `random` для случайных изображений.",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("search_hub"),
        )

    elif text in {PERSISTENT_RANDOM, LEGACY_PERSISTENT_RANDOM}:
        schedule_background_task(
            context,
            send_random_image(
                update.message,
                user_id,
                expected_generation=flow_generation,
            ),
        )

    elif text in {PERSISTENT_FAVORITES, LEGACY_PERSISTENT_FAVORITES}:
        total = await count_favorites(user_id)
        later_count = (await get_user_storage_stats(user_id)).get("read_later", 0)
        await update.message.reply_text(
            f"Главная → Библиотека\n\nИзбранное: `{total}`\nНа потом: `{later_count}`",
            reply_markup=get_library_keyboard(),
            parse_mode="Markdown",
        )

    elif text == PERSISTENT_SUBSCRIPTIONS:
        await update.message.reply_text(
            await build_subscriptions_menu_text(user_id),
            reply_markup=await get_user_subscriptions_keyboard(user_id),
            parse_mode="Markdown",
        )

    elif text in {PERSISTENT_MENU, LEGACY_PERSISTENT_MENU}:
        await update.message.reply_text(
            await build_main_menu_text(user_id),
            reply_markup=await get_user_main_keyboard(user_id),
            parse_mode="Markdown",
        )

    elif text.lower() in RESTART_TEXT_COMMANDS:
        await request_restart(update, context)

    elif state == "waiting_search":
        schedule_background_task(
            context,
            send_image(
                update.message,
                user_id,
                text,
                expected_generation=flow_generation,
            ),
        )

    elif state == "waiting_gallery":
        tags = "" if text.lower() in {"random", "рандом", "случайно"} else text
        schedule_background_task(
            context,
            send_search_gallery(
                update.message,
                user_id,
                tags,
                expected_generation=flow_generation,
            ),
        )

    elif state == "waiting_builder_include":
        include_builder = {"include": " ".join(text.split()[:12])}

        def commit_builder_include():
            search_builders[user_id] = include_builder
            user_states[user_id] = "waiting_builder_exclude"

        if not await commit_flow_if_current(
            user_id, flow_generation, commit_builder_include
        ):
            return
        await update.message.reply_text(
            "Введите исключаемые теги без минуса или отправьте `-`, если исключений нет.",
            reply_markup=get_cancel_keyboard("search_hub"),
        )

    elif state == "waiting_builder_exclude":
        builder = flow_snapshot.get("builder", {})
        include = builder.get("include", "")
        excluded = [] if text == "-" else text.split()[:12]
        built_query = " ".join([include] + [f"-{tag.lstrip('-')}" for tag in excluded]).strip()
        if not await commit_flow_if_current(
            user_id,
            flow_generation,
            lambda: pending_preset_queries.__setitem__(user_id, built_query),
        ):
            return
        await update.message.reply_text(
            f"🧩 Готовый запрос: `{md_code(built_query)}`",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "▶️ Запустить",
                    callback_data=store_callback_payload("builder_run", built_query),
                ),
                InlineKeyboardButton(
                    "💾 Сохранить",
                    callback_data=store_callback_payload("preset_from", built_query),
                ),
            ]]),
            parse_mode="Markdown",
        )

    elif state == "waiting_preset_name":
        preset_query = flow_snapshot.get("preset_query", "")
        settings = normalize_feature_settings(await get_user_settings(user_id))
        preset_id = await create_search_preset(user_id, text, preset_query, settings)
        await update.message.reply_text(
            "✅ Запрос сохранён." if preset_id else "❌ Пустое имя или такой запрос уже существует."
        )
        await show_search_presets(update.message, user_id)

    elif state == "waiting_bulk_collection_name":
        post_ids = flow_snapshot.get("bulk_posts", ())
        collection_id = await create_favorite_collection(user_id, text)
        added = 0
        if collection_id:
            for post_id in post_ids:
                post = await get_known_post(post_id)
                if post:
                    await add_favorite(user_id, post)
                    if await add_favorite_to_collection(user_id, collection_id, post_id):
                        added += 1
        await update.message.reply_text(
            f"🗂 Коллекция создана, добавлено: {added}." if collection_id
            else "❌ Не удалось создать коллекцию: имя уже занято или пустое."
        )

    elif state == "waiting_subscription_blacklist":
        sub_query = flow_snapshot.get("subscription_query", "")
        options = await get_subscription_options(user_id, sub_query)
        options["extra_blacklist"] = "" if text == "-" else " ".join(text.lower().split()[:30])
        saved = await update_subscription_options(user_id, sub_query, options)
        await update.message.reply_text(
            "✅ Фильтр подписки обновлён." if saved else "❌ Подписка не найдена."
        )

    elif state == "waiting_collection_create":
        collection_id = await create_favorite_collection(user_id, text)
        if collection_id:
            await update.message.reply_text(f"✅ Коллекция «{text[:40]}» создана.")
        else:
            await update.message.reply_text("❌ Пустое имя или коллекция уже существует.")
        await show_collections(update.message, user_id)

    elif state and state.startswith("waiting_collection_rename_"):
        collection_id = state.replace("waiting_collection_rename_", "", 1)
        renamed = collection_id.isdigit() and await rename_favorite_collection(
            user_id, int(collection_id), text
        )
        await update.message.reply_text(
            "✅ Коллекция переименована." if renamed else "❌ Имя занято или коллекция не найдена."
        )
        await show_collections(update.message, user_id)

    elif state and state.startswith("waiting_favorite_note_"):
        post_id_text = state.replace("waiting_favorite_note_", "", 1)
        note = "" if text == "-" else text
        saved = post_id_text.isdigit() and await set_favorite_note(
            user_id, int(post_id_text), note
        )
        await update.message.reply_text(
            "✅ Заметка сохранена." if saved and note else
            "✅ Заметка удалена." if saved else "❌ Пост не найден в избранном."
        )

    elif state == "waiting_gallery_resolution":
        normalized = text.lower().replace("×", "x").replace(" ", "")
        parts = normalized.split("x", 1)
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            await update.message.reply_text("❌ Формат: `1920x1080`.", parse_mode="Markdown")
        else:
            settings = await mutate_user_settings(
                user_id,
                lambda _current: {
                    "min_width": min(int(parts[0]), 10000),
                    "min_height": min(int(parts[1]), 10000),
                },
            )
            await update.message.reply_text(
                gallery_settings_text(settings),
                reply_markup=get_gallery_settings_keyboard(normalize_feature_settings(settings)),
                parse_mode="Markdown",
            )

    elif state == "waiting_bl_temp":
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text("❌ Укажите тег и срок, например `animated 2ч`.", parse_mode="Markdown")
        else:
            minutes = parse_pause_minutes(parts[-1])
            tag = "_".join(parts[:-1]).lower()
            changed = await add_temporary_blacklist_tag(user_id, tag, minutes)
            await update.message.reply_text(
                (
                    f"✅ `{md_code(tag)}` скрыт на {format_pause_duration(minutes)}."
                    if changed else
                    f"ℹ️ `{md_code(tag)}` уже находится в постоянном чёрном списке."
                ),
                reply_markup=get_blacklist_keyboard(),
                parse_mode="Markdown",
            )

    elif state == "waiting_bl_import":
        tags = {
            tag.strip().lower()
            for tag in text.replace(",", " ").replace(";", " ").split()
            if tag.strip()
        }
        count = await replace_user_blacklist(user_id, tags)
        await update.message.reply_text(
            f"✅ Импортировано тегов: {count}.", reply_markup=get_blacklist_keyboard()
        )

    elif state == "waiting_bl_suggest":
        suggestions = await api.autocomplete(text)
        if suggestions:
            rows = [[InlineKeyboardButton(
                f"➕ {tag[:40]}", callback_data=store_callback_payload("bl_quick", tag)
            )] for tag in suggestions[:10]]
            await update.message.reply_text(
                "💡 Похожие теги:", reply_markup=InlineKeyboardMarkup(rows)
            )
        else:
            await update.message.reply_text("Похожие теги не найдены.")

    elif state == "waiting_pause_subscriptions":
        pause_minutes = parse_pause_minutes(text)
        paused_count = await pause_all_active_subscriptions(user_id, pause_minutes)
        await update.message.reply_text(
            "⏸ Подписки остановлены на "
            f"{format_pause_duration(pause_minutes)}.\n\n"
            f"Затронуто активных подписок: {paused_count}.\n"
            "Новые подписки во время паузы тоже начнут работать только после неё.",
            reply_markup=await get_user_subscriptions_keyboard(user_id),
        )

    elif state == "waiting_fav_tag":
        await show_favorite_search_results(update.message, user_id, text)

    elif state == "waiting_sub_new":
        if not await commit_flow_if_current(
            user_id,
            flow_generation,
            lambda: user_states.__setitem__(user_id, f"waiting_sub_interval_{text}"),
        ):
            return
        await update.message.reply_text(
            f"🔔 Подписка на: `{md_code(text)}`\n\n"
            "Введите интервал в минутах от 1 до 120 (по умолчанию 10):",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("subscriptions"),
        )

    elif state and state.startswith("waiting_sub_interval_update_"):
        sub_query = state.replace("waiting_sub_interval_update_", "", 1)
        interval = parse_subscription_interval(text)

        success = await update_subscription_interval(user_id, sub_query, interval)
        if success:
            await update.message.reply_text(
                f"✅ Интервал подписки `{md_code(sub_query)}` изменён на {interval} мин.",
                reply_markup=await get_user_subscriptions_keyboard(user_id),
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "❌ Подписка не найдена.",
                reply_markup=await get_user_subscriptions_keyboard(user_id),
            )

    elif state and state.startswith("waiting_sub_interval_"):
        query = state.replace("waiting_sub_interval_", "", 1)

        interval = parse_subscription_interval(text)
        preview_text, preview_keyboard = get_subscription_preview(
            query,
            interval,
            user_id,
            issuer=callback_issuer_for(user_id, flow_generation),
        )
        await update.message.reply_text(
            preview_text,
            reply_markup=preview_keyboard,
            parse_mode="Markdown",
        )

    elif state == "waiting_bl_add":
        tags = text.lower().split()
        added = []
        already = []

        for tag in tags:
            success = await add_to_blacklist(user_id, tag)
            if success:
                added.append(tag)
            else:
                already.append(tag)

        msg_parts = []
        if added:
            msg_parts.append(
                f"✅ Добавлены: {', '.join(f'`{md_code(t)}`' for t in added)}")
        if already:
            msg_parts.append(
                f"⚠️ Уже были: {', '.join(f'`{md_code(t)}`' for t in already)}")

        await update.message.reply_text(
            "\n".join(msg_parts) or "Ничего не добавлено",
            reply_markup=get_blacklist_keyboard(),
            parse_mode="Markdown",
        )

    elif state == "waiting_bl_remove":
        tags = text.lower().split()
        removed = []
        not_found = []

        for tag in tags:
            success = await remove_from_blacklist(user_id, tag)
            if success:
                removed.append(tag)
            else:
                not_found.append(tag)

        msg_parts = []
        if removed:
            msg_parts.append(
                f"✅ Удалены: {', '.join(f'`{md_code(t)}`' for t in removed)}")
        if not_found:
            msg_parts.append(
                f"⚠️ Не найдены: {', '.join(f'`{md_code(t)}`' for t in not_found)}")

        await update.message.reply_text(
            "\n".join(msg_parts) or "Ничего не удалено",
            reply_markup=get_blacklist_keyboard(),
            parse_mode="Markdown",
        )

    else:
        # По умолчанию - поиск
        schedule_background_task(
            context,
            send_image(
                update.message,
                user_id,
                text,
                expected_generation=flow_generation,
            ),
        )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /search"""
    if context.args:
        tags = " ".join(context.args)
        schedule_background_task(
            context,
            send_image(update.message, update.effective_user.id, tags),
        )
    else:
        await update.message.reply_text(
            "Использование: `/search <теги>`\n" "Пример: `/search anime girl`",
            parse_mode="Markdown",
        )


async def random_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /random"""
    schedule_background_task(
        context,
        send_random_image(update.message, update.effective_user.id),
    )


async def subscriptions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /subscriptions"""
    user_id = update.effective_user.id
    await update.message.reply_text(
        await build_subscriptions_menu_text(user_id),
        reply_markup=await get_user_subscriptions_keyboard(user_id),
        parse_mode="Markdown",
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the current multi-step input flow."""
    user_id = update.effective_user.id
    await invalidate_user_flow(user_id)
    export_cancelled = bool(zip_export_manager) and await zip_export_manager.cancel_for_user(user_id)
    await update.message.reply_text(
        ("Действие и ZIP-экспорт отменены.\n\n" if export_cancelled else "Действие отменено.\n\n")
        + await build_main_menu_text(user_id),
        reply_markup=await get_user_main_keyboard(user_id),
    )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /history"""
    await show_history(update.message, update.effective_user.id)


async def favorites_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /favorites"""
    await show_favorites(update.message, update.effective_user.id)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /settings"""
    user_id = update.effective_user.id
    settings = await get_user_settings(user_id)
    caption_enabled = (
        "✅ Включено" if settings.get("show_caption", True) else "❌ Выключено"
    )

    await update.message.reply_text(
        "⚙️ *Настройки*\n\n"
        f"Подписи к постам: {caption_enabled}\n\n"
        "Здесь можно настроить внешний вид постов, подборки и качество медиа.",
        reply_markup=await get_user_settings_keyboard(user_id),
        parse_mode="Markdown",
    )


async def gallery_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tags = " ".join(context.args).strip()
    if not tags:
        user_states[update.effective_user.id] = "waiting_gallery"
        await update.message.reply_text(
            "🖼 Введите теги для галереи или `random` для случайной подборки.",
            parse_mode="Markdown",
        )
        return
    if tags.lower() in {"random", "рандом"}:
        tags = ""
    schedule_background_task(
        context, send_search_gallery(update.message, update.effective_user.id, tags)
    )


async def collections_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_collections(update.message, update.effective_user.id)


async def presets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_search_presets(update.message, update.effective_user.id)


async def recommendations_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    schedule_background_task(
        context, send_recommendations(update.message, update.effective_user.id)
    )


async def later_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_read_later(update.message, update.effective_user.id)


async def storage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_storage(update.message, update.effective_user.id)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_user_stats(update.message, update.effective_user.id)


async def whyblocked_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Использование: `/whyblocked <ID поста или теги>`", parse_mode="Markdown"
        )
        return
    blacklist = await get_user_blacklist(update.effective_user.id)
    if len(context.args) == 1 and context.args[0].isdigit():
        post_id = int(context.args[0])
        post = await get_known_post(post_id) or await api.get_post_by_id(post_id)
        supplied = set((post or {}).get("tags", "").lower().split())
    else:
        supplied = {tag.lower().lstrip("-") for tag in context.args}
    matched = sorted(blacklist & supplied)
    if matched:
        await update.message.reply_text(
            "🚫 Совпали теги чёрного списка: "
            + ", ".join(f"`{md_code(tag)}`" for tag in matched),
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("✅ Среди переданных тегов совпадений с чёрным списком нет.")


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("❌ Недостаточно прав.")
        return
    db_stats = await get_admin_database_stats()
    disk = shutil.disk_usage(os.getcwd())
    api_started = time.monotonic()
    try:
        await api.search("1girl", set(), limit=1, timeout=5)
        api_status = f"ok ({time.monotonic() - api_started:.2f}s)"
    except Exception as exc:
        api_status = f"error: {type(exc).__name__}"
    sub_status = "running" if subscription_task and not subscription_task.done() else "stopped"
    heartbeat_status = "running" if heartbeat_task and not heartbeat_task.done() else "stopped"
    if not TAG_TRANSLATION_ENABLED:
        translation_status = "disabled"
    else:
        translation_status = (
            "running"
            if tag_translation_task and not tag_translation_task.done()
            else "stopped"
        )
    await update.message.reply_text(
        "🩺 *Health*\n\n"
        f"DB quick check: `{md_code(db_stats['quick_check'])}`\n"
        f"Rule34 API: `{api_status}`\n"
        f"Subscription worker: `{sub_status}`\n"
        f"Heartbeat: `{heartbeat_status}`\n"
        f"Tag translation worker: `{translation_status}`\n"
        f"DB size: `{os.path.getsize(DB_PATH) // (1024 * 1024)} MiB`\n"
        f"Disk free: `{disk.free // (1024 * 1024)} MiB`",
        parse_mode="Markdown",
    )


async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("❌ Недостаточно прав.")
        return
    db_stats = await get_admin_database_stats()
    metrics = runtime_metrics.snapshot()
    counters = "\n".join(
        f"• `{md_code(name)}`: {value}"
        for name, value in sorted(metrics["counters"].items())
    ) or "• событий пока нет"
    counts = "\n".join(
        f"• `{table}`: {count}" for table, count in db_stats["counts"].items()
    )
    await update.message.reply_text(
        f"📈 *Статистика бота*\n\nUptime: {metrics['uptime_seconds']} сек.\n\n"
        f"*Runtime:*\n{counters}\n\n*Database:*\n{counts}",
        parse_mode="Markdown",
    )


async def retry_failed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("❌ Недостаточно прав.")
        return
    claim_token, failures = await claim_delivery_failures(limit=20)
    delivered = 0
    try:
        for failure in failures:
            recipient_id = failure["user_id"]
            if not is_recipient_allowed(recipient_id, recipient_id, "private"):
                logger.info(
                    "Skipping failed delivery after access revocation user=%s post=%s",
                    recipient_id,
                    failure["post_id"],
                )
                continue

            async def retry_precondition(failure=failure):
                recipient_id = failure["user_id"]
                if not is_recipient_allowed(recipient_id, recipient_id, "private"):
                    return False
                return await renew_delivery_failure_claim_for_post(
                    recipient_id,
                    failure["post_id"],
                    claim_token,
                )

            keyboard = get_subscription_image_keyboard(
                failure["post"].get("id", 0),
                side_effect_callback=subscription_callback_issuer_for(
                    recipient_id
                ),
            )
            ok = await send_post_media_to_chat(
                context.bot,
                recipient_id,
                failure["post"],
                failure["caption"],
                keyboard=keyboard,
                before_send=retry_precondition,
            )
            if ok and claim_token:
                acknowledged = await delete_delivery_failure_for_post(
                    recipient_id, failure["post_id"], claim_token
                )
                delivered += int(acknowledged)
            elif not ok:
                revoke_unsent_keyboard_callbacks(keyboard)
    finally:
        if claim_token:
            await release_delivery_failure_claim(claim_token)
    await update.message.reply_text(
        f"♻️ Повторено: {len(failures)}, доставлено: {delivered}, осталось: {len(failures) - delivered}."
    )


async def request_restart(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    response_text: str | None = "♻️ Перезапускаюсь...",
):
    global restart_requested

    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("❌ Недостаточно прав.")
        logger.warning("Unauthorized restart attempt user=%s", user_id)
        return

    restart_requested = True
    if response_text:
        await update.message.reply_text(response_text)
    logger.warning("Restart requested by admin user=%s", user_id)
    context.application.stop_running()


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only command to restart the bot via the launcher."""
    await request_restart(update, context)


def is_private_admin_command(update: Update) -> bool:
    return bool(
        update.effective_user
        and update.effective_user.id in ADMIN_USER_IDS
        and update.effective_chat
        and update.effective_chat.type == "private"
    )


async def _reject_non_private_admin(update: Update) -> bool:
    if is_private_admin_command(update):
        return False
    await update.message.reply_text("❌ Команда доступна только администратору в личном чате.")
    return True


async def version_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_non_private_admin(update):
        return
    try:
        info = await get_version_info()
    except UpdateCommandError as exc:
        logger.warning(
            "Version command failed admin=%s stage=%s rc=%s timeout=%s",
            update.effective_user.id,
            exc.stage,
            exc.returncode,
            exc.timed_out,
        )
        await update.message.reply_text("❌ Не удалось определить версию проекта.")
        return
    await update.message.reply_text(
        "🔎 Версия проекта\n"
        f"Commit: {info.commit[:12]}\n"
        f"Branch: {info.branch[:128]}\n"
        f"Дата: {info.commit_date[:64]}\n"
        f"Локальные изменения: {'есть' if info.dirty else 'нет'}"
    )


async def update_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_non_private_admin(update):
        return
    admin_id = update.effective_user.id
    logger.info("Update check started admin=%s", admin_id)
    try:
        result = await check_for_updates()
    except (UpdateCommandError, ValueError) as exc:
        stage = exc.stage if isinstance(exc, UpdateCommandError) else "configuration"
        returncode = exc.returncode if isinstance(exc, UpdateCommandError) else None
        logger.warning(
            "Update check failed admin=%s stage=%s rc=%s",
            admin_id,
            stage,
            returncode,
        )
        await update.message.reply_text("❌ Не удалось проверить обновления.")
        return
    if result.current_commit == result.remote_commit:
        text = "✅ Уже установлена последняя версия."
    else:
        text = (
            f"⬆️ Доступно коммитов: {result.commits_behind}\n"
            f"Текущий: {result.current_commit[:12]}\n"
            f"Удалённый: {result.remote_commit[:12]}"
        )
    await update.message.reply_text(text)
    logger.info("Update check completed admin=%s behind=%s", admin_id, result.commits_behind)


def _update_error_text(result) -> str:
    if result.timed_out:
        text = f"❌ Обновление остановлено: превышен timeout на этапе {result.stage}."
    else:
        labels = {
            "work_tree": "проверки репозитория",
            "status": "проверки рабочей копии",
            "fetch": "получения данных из GitHub",
            "local_commit": "определения текущей версии",
            "remote_commit": "определения удалённой версии",
            "backup": "резервного копирования базы данных",
            "pull": "fast-forward обновления",
            "new_commit": "проверки новой версии",
            "dependency_diff": "проверки зависимостей",
            "dependencies": "установки зависимостей",
            "compile": "компиляции новой версии",
            "imports": "проверки импортов новой версии",
        }
        text = f"❌ Обновление остановлено на этапе {labels.get(result.stage, 'проверки')}."
    if result.old_commit and result.new_commit:
        text += (
            f"\nБыло: {result.old_commit[:12]}"
            f"\nСтало: {result.new_commit[:12]}"
        )
    return text


def build_subscription_create_error(result) -> str:
    if result.status == "total_limit_reached":
        return f"❌ Достигнут лимит подписок: {result.total_count} из {result.total_limit}."
    if result.status == "active_limit_reached":
        return "❌ Сначала приостановите или удалите одну из активных подписок."
    if result.status == "query_too_long":
        return "❌ Запрос слишком длинный. Сократите его и попробуйте снова."
    if result.status == "too_many_tags":
        return "❌ В запросе слишком много тегов. Удалите лишние и попробуйте снова."
    if result.status == "cooldown":
        return f"⏳ Новую подписку можно создать через {max(1, result.retry_after_seconds)} сек."
    if result.status == "invalid_query":
        return "❌ Некорректный запрос подписки. Проверьте теги и попробуйте снова."
    if result.status == "ambiguous_query":
        return (
            "❌ Найдено несколько старых подписок с таким запросом. "
            "Измените нужную подписку из списка или удалите дубликаты."
        )
    return "❌ Не удалось создать подписку. Попробуйте позже."


async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_non_private_admin(update):
        return
    if update_operation_lock.locked():
        await update.message.reply_text("⏳ Обновление уже выполняется.")
        return

    admin_id = update.effective_user.id
    await update_operation_lock.acquire()
    started = time.monotonic()
    logger.warning("Project update started admin=%s", admin_id)
    try:
        result = await perform_update()
        if result.status == "dirty":
            logger.warning(
                "Project update blocked by dirty tree admin=%s files=%s",
                admin_id,
                len(result.changed_files),
            )
            paths = "\n".join(f"• {path[:200]}" for path in result.changed_files[:20])
            suffix = "\n…" if len(result.changed_files) > 20 else ""
            await update.message.reply_text(
                "❌ Обновление отменено: рабочая копия содержит изменения.\n"
                f"{paths}{suffix}"
            )
            return
        if result.status == "current":
            logger.info("Project update already current admin=%s", admin_id)
            await update.message.reply_text("✅ Уже установлена последняя версия.")
            return
        if result.status != "updated":
            logger.warning(
                "Project update result admin=%s stage=%s rc=%s timeout=%s",
                admin_id,
                result.stage,
                result.returncode,
                result.timed_out,
            )
            await update.message.reply_text(_update_error_text(result))
            return

        write_update_marker(admin_id, result.new_commit)
        await update.message.reply_text(
            "✅ Обновление установлено.\n"
            f"Было: {result.old_commit[:12]}\n"
            f"Стало: {result.new_commit[:12]}\n"
            "Перезапускаюсь…"
        )
        await request_restart(update, context, response_text=None)
    except asyncio.CancelledError:
        logger.warning("Project update cancelled admin=%s", admin_id)
        raise
    except Exception:
        logger.exception("Project update handler failed admin=%s", admin_id)
        await update.message.reply_text("❌ Не удалось завершить обновление.")
    finally:
        update_operation_lock.release()
        logger.warning(
            "Project update finished admin=%s elapsed=%.2fs",
            admin_id,
            time.monotonic() - started,
        )


async def tags_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /tags - поиск/автодополнение тегов"""
    if context.args:
        query = " ".join(context.args)
        suggestions = await api.autocomplete(query)

        if suggestions:
            tags_list = "\n".join(f"• `{md_code(tag)}`" for tag in suggestions)
            await update.message.reply_text(
                f"🔖 *Найденные теги для* `{md_code(query)}`:\n\n{tags_list}",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                f"❌ Теги по запросу `{md_code(query)}` не найдены", parse_mode="Markdown"
            )
    else:
        await update.message.reply_text(
            "Использование: `/tags <запрос>`\n"
            "Пример: `/tags blon` → покажет теги начинающиеся на 'blon'",
            parse_mode="Markdown",
        )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /id - получить пост по ID"""
    if context.args and context.args[0].isdigit():
        post_id = int(context.args[0])

        status_msg = await update.message.reply_text("🔍 Ищу...")

        result = await get_known_post(post_id)
        if not result or not get_media_url_candidates(result):
            result = await api.get_post_by_id(post_id)
            if result:
                await remember_and_cache_post(result)

        await status_msg.delete()

        if result:
            user_id = update.effective_user.id
            settings = await get_user_settings(user_id)


            # Строим описание на основе настроек
            caption = ""
            if settings.get("show_caption", True):
                caption = await build_caption(settings, result, f"id:{post_id}")

            keyboard = get_image_keyboard(
                post_id,
                show_tags_button=should_show_tags_button(settings),
                side_effect_callback=side_effect_callback_for(user_id),
            )

            await send_post_media(
                update.message, result, caption, keyboard, settings=settings
            )
        else:
            await update.message.reply_text(
                f"❌ Пост с ID `{md_code(post_id)}` не найден",
                parse_mode="Markdown",
            )
    else:
        await update.message.reply_text(
            "Использование: `/id <номер>`\n" "Пример: `/id 1234567`",
            parse_mode="Markdown",
        )


async def blacklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /blacklist"""
    if context.args:
        action = context.args[0].lower()
        tags = [tag.lower() for tag in context.args[1:] if tag.strip()]
        if action not in {"add", "remove"} or not tags:
            await update.message.reply_text(
                "Использование:\n"
                "`/blacklist add <тег>`\n"
                "`/blacklist remove <тег>`",
                parse_mode="Markdown",
            )
            return

        changed = []
        unchanged = []
        operation = add_to_blacklist if action == "add" else remove_from_blacklist
        for tag in tags:
            if await operation(update.effective_user.id, tag):
                changed.append(tag)
            else:
                unchanged.append(tag)

        lines = []
        if changed:
            label = "Добавлены" if action == "add" else "Удалены"
            lines.append(
                f"✅ {label}: {', '.join(f'`{md_code(tag)}`' for tag in changed)}"
            )
        if unchanged:
            label = "Уже были" if action == "add" else "Не найдены"
            lines.append(
                f"⚠️ {label}: "
                f"{', '.join(f'`{md_code(tag)}`' for tag in unchanged)}"
            )

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=get_blacklist_keyboard(),
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        "🚫 *Чёрный список*",
        reply_markup=get_blacklist_keyboard(),
        parse_mode="Markdown",
    )


async def process_one_subscription(app, subscription):
    """Process one due subscription after atomically claiming it."""
    user_id, query, interval, empty_count = subscription
    processing_token = await claim_due_subscription(user_id, query)
    if not processing_token:
        return False

    if not is_recipient_allowed(user_id, user_id, "private"):
        logger.info(
            "Skipping subscription after access revocation user=%s query=%r",
            user_id,
            query,
        )
        await release_subscription_claim(user_id, query, processing_token)
        return False

    result = None
    caption = ""
    claim_completed = False
    delivery_started = False
    try:
        logger.info("Отправляем подписку пользователю %s: %s", user_id, query)

        blacklist = await get_user_blacklist(user_id)
        settings = await get_user_settings(user_id)
        subscription_options = await get_subscription_options(user_id, query)
        blacklist |= set(str(subscription_options.get("extra_blacklist", "")).split())
        settings.update({
            key: value for key, value in subscription_options.items()
            if key in {"rating_filter", "media_type", "orientation", "min_width", "min_height", "quality_mode"}
        })
        settings = normalize_feature_settings(settings)
        excluded_post_ids = await get_sent_post_ids(user_id)
        result = await get_subscription_cached_image(
            user_id, query, blacklist, excluded_post_ids, settings
        )
        reset_upstream_failure_streak()

        if result:
            await remember_and_cache_post(result)
            post_id = result.get("id", 0)
            if subscription_options.get("digest_mode") == "digest":
                queued = await enqueue_subscription_digest(user_id, query, result)
                updated = await update_subscription_time(user_id, query, processing_token)
                claim_completed = bool(updated)
                if updated and post_id:
                    await mark_post_sent(user_id, int(post_id))
                runtime_metrics.increment("subscription_digest_queued", int(queued))
                return bool(updated)
            if not await is_subscription_claim_active(
                user_id, query, processing_token
            ):
                logger.info(
                    "Subscription changed before delivery user=%s query=%r",
                    user_id,
                    query,
                )
                return False
            keyboard = get_subscription_image_keyboard(
                post_id,
                query,
                should_show_tags_button(settings),
                side_effect_callback=subscription_callback_issuer_for(user_id),
            )

            caption = ""
            if settings.get("show_caption", True):
                caption = await build_caption(settings, result, query, True)

            delivery_started = True

            async def subscription_delivery_allowed():
                return is_recipient_allowed(user_id, user_id, "private")

            delivered = await send_post_media_to_chat(
                app.bot,
                user_id,
                result,
                caption,
                keyboard,
                settings=settings,
                before_send=subscription_delivery_allowed,
            )
            if delivered:
                runtime_metrics.increment("subscription_delivered")
                updated = await update_subscription_time(user_id, query, processing_token)
                claim_completed = bool(updated)
                if updated and post_id:
                    await mark_post_sent(user_id, int(post_id))
                    await clear_delivery_failure_for_post(user_id, int(post_id))
                elif not updated:
                    logger.warning(
                        "Subscription claim expired before schedule update for user=%s query=%r",
                        user_id,
                        query,
                    )
            else:
                runtime_metrics.increment("subscription_failed")
                revoke_unsent_keyboard_callbacks(keyboard)
                await save_delivery_failure(user_id, result, caption)
            return bool(delivered)

        empty_count, backoff_minutes, should_notify = await mark_subscription_empty(
            user_id, query, processing_token
        )
        claim_completed = backoff_minutes > 0
        logger.info(
            "No new post for subscription user=%s query=%r; empty_count=%s backoff=%s",
            user_id,
            query,
            empty_count,
            backoff_minutes,
        )
        if should_notify:
            async def subscription_notice_allowed():
                return is_recipient_allowed(user_id, user_id, "private")

            await send_text_to_chat(
                app.bot,
                user_id,
                before_send=subscription_notice_allowed,
                text=(
                    f"🕒 По подписке `{md_code(query)}` пока нет новых постов.\n\n"
                    f"Я продолжу проверять ее реже: следующая проверка примерно через {backoff_minutes} мин. "
                    "Когда появится новый пост, подписка вернется к обычному интервалу."
                ),
                parse_mode="Markdown",
                reply_markup=await get_user_subscriptions_keyboard(user_id),
            )
        return False

    except asyncio.CancelledError:
        if delivery_started:
            claim_completed = await _cancellation_safe_db_call(
                defer_subscription_after_transient_failure(
                    user_id, query, processing_token, backoff_seconds=1800
                )
            )
        raise
    except TimedOut:
        claim_completed = await defer_subscription_after_transient_failure(
            user_id, query, processing_token, backoff_seconds=1800
        )
        logger.warning(
            "Ambiguous subscription timeout deferred user=%s query=%r",
            user_id,
            query,
        )
        return False
    except NetworkError as exc:
        if isinstance(exc, BadRequest):
            if result:
                await save_delivery_failure(
                    user_id, result, caption, error=f"BadRequest: {exc}"
                )
            claim_completed = await defer_subscription_after_transient_failure(
                user_id, query, processing_token
            )
            return False
        claim_completed = await defer_subscription_after_transient_failure(
            user_id, query, processing_token, backoff_seconds=1800
        )
        logger.warning(
            "Ambiguous subscription network error deferred user=%s query=%r: %s",
            user_id,
            query,
            exc,
        )
        return False
    except APITemporaryError as e:
        await note_upstream_failure(app, str(e))
        logger.warning(
            "Temporary Rule34 API error for subscription user=%s query=%r: %s",
            user_id,
            query,
            e,
        )
        claim_completed = await defer_subscription_after_transient_failure(
            user_id, query, processing_token
        )
        return False
    except Exception as exc:
        if result:
            await save_delivery_failure(
                user_id, result, caption, error=f"{type(exc).__name__}: {exc}"
            )
        logger.exception("Subscription processing error for user %s", user_id)
        claim_completed = await defer_subscription_after_transient_failure(
            user_id, query, processing_token
        )
        return False
    finally:
        if not claim_completed:
            await _cancellation_safe_db_call(
                release_subscription_claim(user_id, query, processing_token)
            )


async def get_subscription_cached_image(
    user_id: int,
    query: str,
    blacklist: set,
    excluded_post_ids: set,
    settings: dict | None = None,
):
    blocked_tags = {
        str(tag).strip().lower().lstrip("-")
        for tag in blacklist
        if str(tag).strip().lstrip("-")
    }

    def is_available(post: dict) -> bool:
        post_tags = {
            tag.lower() for tag in str(post.get("tags") or "").split() if tag
        }
        return (
            bool(post.get("file_url"))
            and post.get("id") not in excluded_post_ids
            and not post_tags.intersection(blocked_tags)
            and (settings is None or post_matches_preferences(post, settings))
        )

    cached_posts, _ = await get_subscription_cache(user_id, query)
    available_posts = [post for post in cached_posts if is_available(post)]
    should_refresh = (
        await is_subscription_cache_stale(user_id, query)
        or len(available_posts) < SUBSCRIPTION_CACHE_MIN_AVAILABLE
    )

    if should_refresh:
        try:
            fresh_posts = await api.search_subscription_cache(
                query,
                blacklist,
                pid=0,
            )
        except APITemporaryError:
            if available_posts:
                logger.warning(
                    "Using stale subscription cache after API error user=%s query=%r available=%s",
                    user_id,
                    query,
                    len(available_posts),
                )
                return random.choice(available_posts)
            raise

        if fresh_posts:
            cache_stats = await replace_subscription_cache(user_id, query, fresh_posts)
            cached_posts, _ = await get_subscription_cache(user_id, query)
            available_posts = [post for post in cached_posts if is_available(post)]
            logger.info(
                "Refreshed subscription cache user=%s query=%r api=%s new=%s total=%s available=%s",
                user_id,
                query,
                cache_stats["api"],
                cache_stats["new"],
                cache_stats["total"],
                len(available_posts),
            )

    if not available_posts:
        return None
    return random.choice(available_posts)


async def process_subscriptions(app):
    """Фоновая задача для обработки подписок"""
    logger.info("Запущена фоновая задача для подписок")
    semaphore = asyncio.Semaphore(SUBSCRIPTION_CONCURRENCY)

    async def guarded_user(subscriptions):
        async with semaphore:
            sent_this_pass = 0
            for subscription in subscriptions[:SUBSCRIPTION_MAX_POSTS_PER_USER_PASS]:
                sent_this_pass += bool(await process_one_subscription(app, subscription))
            logger.info(
                "Subscription pass user=%s sent=%s due=%s",
                subscriptions[0][0],
                sent_this_pass,
                len(subscriptions),
            )

    while True:
        try:
            await release_stale_subscription_claims()
            due_subs = await get_due_subscriptions()
            subscriptions_by_user = {}
            for subscription in due_subs:
                subscriptions_by_user.setdefault(subscription[0], []).append(subscription)
            await asyncio.gather(*(
                guarded_user(subscriptions)
                for subscriptions in subscriptions_by_user.values()
            ))
            for digest_user_id in await get_due_digest_users():
                if not is_recipient_allowed(
                    digest_user_id, digest_user_id, "private"
                ):
                    logger.info(
                        "Skipping digest after access revocation user=%s",
                        digest_user_id,
                    )
                    continue
                claim_token, digest_posts = await claim_subscription_digest(
                    digest_user_id, 10
                )
                if not claim_token:
                    continue
                claim_open = True
                locks: list[DigestSubscriptionLockHandle] = []
                lease: DigestClaimLease | None = None
                try:
                    locks = await acquire_digest_subscription_locks(
                        digest_user_id, digest_posts
                    )
                    active_keys = await get_subscription_digest_claim_keys(
                        digest_user_id, claim_token
                    )
                    digest_posts = [
                        post for post in digest_posts
                        if digest_item_key(post) in active_keys
                    ]
                    lease = DigestClaimLease(digest_user_id, claim_token)
                    if not digest_posts or not await lease.start():
                        await cancellation_safe_digest_finish(
                            digest_user_id, claim_token, []
                        )
                        claim_open = False
                        continue
                    delivery = await send_digest_to_chat(
                        app.bot, digest_user_id, digest_posts, lease=lease
                    )
                    if delivery.ambiguous_ids:
                        await cancellation_safe_digest_finish(
                            digest_user_id,
                            claim_token,
                            delivery.delivered_ids,
                            delivery.ambiguous_ids,
                        )
                    else:
                        await cancellation_safe_digest_finish(
                            digest_user_id, claim_token, delivery.delivered_ids
                        )
                    claim_open = False
                    logger.info(
                        "Scheduled digest result user=%s delivered=%s failed=%s ambiguous=%s",
                        digest_user_id,
                        len(delivery.delivered_ids),
                        len(delivery.failed_ids),
                        len(delivery.ambiguous_ids),
                    )
                except DigestDeliveryCancelled as exc:
                    await cancellation_safe_digest_finish(
                        digest_user_id,
                        claim_token,
                        exc.result.delivered_ids,
                        exc.result.ambiguous_ids,
                    )
                    claim_open = False
                    raise
                finally:
                    if lease is not None:
                        await lease.stop()
                    if claim_open:
                        await cancellation_safe_digest_release(
                            digest_user_id, claim_token
                        )
                    release_digest_subscription_locks(locks)
            logger.info(
                "Subscription pass complete users=%s due=%s",
                len(subscriptions_by_user),
                len(due_subs),
            )
            await asyncio.sleep(SUBSCRIPTION_CHECK_INTERVAL_SECONDS)

        except Exception:
            logger.exception("Ошибка в фоновой задаче подписок")
            await asyncio.sleep(SUBSCRIPTION_CHECK_INTERVAL_SECONDS)


async def maintenance_loop(
    *,
    cache_cleanup=None,
    sleep=None,
    clock=None,
    cache_interval_seconds: float | None = None,
    state_interval_seconds: float | None = None,
):
    global cache_cleanup_last_deleted, cache_cleanup_errors
    cleanup = cleanup_expired_caches if cache_cleanup is None else cache_cleanup
    sleep_call = asyncio.sleep if sleep is None else sleep
    monotonic = time.monotonic if clock is None else clock
    cache_interval = max(
        0.01,
        float(
            SUBSCRIPTION_CACHE_CLEANUP_INTERVAL_SECONDS
            if cache_interval_seconds is None else cache_interval_seconds
        ),
    )
    state_interval = max(
        0.01,
        float(
            USER_STATE_CLEANUP_INTERVAL_SECONDS
            if state_interval_seconds is None else state_interval_seconds
        ),
    )
    now = monotonic()
    next_cache_cleanup = now + cache_interval
    next_state_cleanup = now + state_interval

    while True:
        await sleep_call(max(0.0, min(next_cache_cleanup, next_state_cleanup) - monotonic()))
        now = monotonic()

        if now >= next_state_cleanup:
            try:
                temporary_user_state.cleanup_expired(
                    USER_STATE_TTL_MINUTES * 60,
                    now=now,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                cache_cleanup_errors += 1
                logger.exception("Process-local state cleanup failed")
            next_state_cleanup = now + state_interval

        if now >= next_cache_cleanup:
            try:
                result = await cleanup()
                cache_cleanup_last_deleted = (
                    result.subscription_cache_deleted + result.post_cache_deleted
                )
                logger.info(
                    "Cache cleanup deleted=%s subscription_remaining=%s post_remaining=%s elapsed_ms=%.1f",
                    cache_cleanup_last_deleted,
                    result.subscription_cache_remaining,
                    result.post_cache_remaining,
                    result.elapsed_ms,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                cache_cleanup_errors += 1
                logger.exception("SQLite cache cleanup failed")
            next_cache_cleanup = now + cache_interval


async def heartbeat_loop():
    started_at = time.monotonic()
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            export_stats = zip_export_manager.stats() if zip_export_manager else {
                "queued": 0, "active": 0, "tracked_users": 0,
            }
            telegram_metrics = telegram_rate_limiter.snapshot_metrics()
            logger.info(
                "Heartbeat alive uptime=%ss user_states=%s recent_posts=%s export_queued=%s export_active=%s cache_cleanup_last_deleted=%s cache_cleanup_errors=%s telegram_requests_total=%s telegram_rate_limit_waits=%s telegram_retry_after_count=%s telegram_retry_attempts=%s telegram_ambiguous_timeouts=%s telegram_request_failures=%s telegram_limiter_registry_size=%s user_gate_registry_size=%s user_gate_waiters=%s user_gate_contention_total=%s stale_flow_results_discarded=%s duplicate_callbacks_rejected=%s instance_lock_held=%s instance_lock_wait_ms=%s startup_orphans_deleted=%s",
                int(time.monotonic() - started_at),
                len(user_states),
                len(recent_posts),
                export_stats["queued"],
                export_stats["active"],
                cache_cleanup_last_deleted,
                cache_cleanup_errors,
                telegram_metrics["telegram_requests_total"],
                telegram_metrics["telegram_rate_limit_waits"],
                telegram_metrics["telegram_retry_after_count"],
                telegram_metrics["telegram_retry_attempts"],
                telegram_metrics["telegram_ambiguous_timeouts"],
                telegram_metrics["telegram_request_failures"],
                telegram_metrics["telegram_limiter_registry_size"],
                user_operation_gate.registry_size,
                user_operation_gate.waiter_count,
                user_operation_gate.metrics.contention_total,
                stale_flow_results_discarded,
                duplicate_callbacks_rejected,
                int(instance_lifecycle is not None and instance_lifecycle.lock.held),
                instance_lock_wait_ms,
                startup_orphans_deleted,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Heartbeat loop error")


async def post_init(application):
    global subscription_task, heartbeat_task, tag_translation_task, zip_export_manager, maintenance_task

    await user_operation_gate.start()
    bot = application.bot

    # 💣 СНАЧАЛА ЧИСТИМ ВСЁ
    await bot.delete_my_commands(scope=BotCommandScopeDefault())
    await bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
    await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())

    # 🔥 ПОТОМ СТАВИМ НОВЫЕ
    commands = [
        BotCommand("start", "Запуск бота"),
        BotCommand("search", "Поиск"),
        BotCommand("random", "Случайная картинка"),
        BotCommand("gallery", "Галерея по тегам"),
        BotCommand("blacklist", "Черный список"),
        BotCommand("subscriptions", "Подписки"),
        BotCommand("history", "История"),
        BotCommand("favorites", "Избранное"),
        BotCommand("collections", "Коллекции избранного"),
        BotCommand("presets", "Сохранённые запросы"),
        BotCommand("recommendations", "Рекомендации"),
        BotCommand("later", "Посмотреть позже"),
        BotCommand("storage", "Хранилище"),
        BotCommand("stats", "Личная статистика"),
        BotCommand("settings", "Настройки"),
        BotCommand("cancel", "Отменить текущее действие"),
        BotCommand("tags", "Поиск по тегу"),
        BotCommand("id", "Поиск по ID картинки"),
    ]

    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())

    """Инициализация после запуска"""
    await init_db()
    await notify_update_marker(bot)

    if zip_export_manager is None:
        zip_export_manager = ZipExportManager(
            application=application,
            source_loader=load_zip_export_source,
            post_resolver=ensure_favorite_original_url,
            cancel_markup_factory=get_zip_export_cancel_keyboard,
            favorites_cooldown_seconds=FAVORITES_EXPORT_COOLDOWN_SECONDS,
        )
    await zip_export_manager.start()

    # Запускаем фоновую задачу для подписок
    if subscription_task is None or subscription_task.done():
        subscription_task = asyncio.create_task(
            process_subscriptions(application))
        logger.info("Фоновая задача подписок запущена")

    if heartbeat_task is None or heartbeat_task.done():
        heartbeat_task = asyncio.create_task(heartbeat_loop())
        logger.info("Heartbeat task started")

    if maintenance_task is None or maintenance_task.done():
        maintenance_task = asyncio.create_task(maintenance_loop())
        logger.info("Cache and process-local state maintenance task started")

    if TAG_TRANSLATION_ENABLED and (
        tag_translation_task is None or tag_translation_task.done()
    ):
        tag_translation_task = asyncio.create_task(
            tag_translation_service.background_worker()
        )
        logger.info("Tag translation task started")


async def post_shutdown(application):
    """Очистка при завершении"""
    # Останавливаем фоновую задачу
    global subscription_task, heartbeat_task, tag_translation_task, zip_export_manager, maintenance_task
    await user_operation_gate.shutdown()
    if zip_export_manager is not None:
        await zip_export_manager.stop()
        zip_export_manager = None
    if subscription_task and not subscription_task.done():
        subscription_task.cancel()
        try:
            await subscription_task
        except asyncio.CancelledError:
            pass

    if heartbeat_task and not heartbeat_task.done():
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    if maintenance_task and not maintenance_task.done():
        maintenance_task.cancel()
        try:
            await maintenance_task
        except asyncio.CancelledError:
            pass
    maintenance_task = None
    temporary_user_state.clear_all()
    issued_one_shot_callbacks.clear()

    if tag_translation_task and not tag_translation_task.done():
        tag_translation_task.cancel()
        try:
            await tag_translation_task
        except asyncio.CancelledError:
            pass

    await tag_translation_service.close()
    await api.close()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    if isinstance(error, BadRequest) and (
        "Query is too old" in str(error) or "query id is invalid" in str(error)
    ):
        logger.warning("Ignoring expired callback query: %s", error)
        return
    logger.error(
        "Unhandled Telegram handler error",
        exc_info=(type(error), error, error.__traceback__),
    )


def build_and_run_application() -> None:
    """Build the Telegram application and run it until shutdown."""
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .rate_limiter(telegram_rate_limiter)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .concurrent_updates(8)
        .build()
    )

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", require_access(start)))
    application.add_handler(CommandHandler("search", require_access(search_command)))
    application.add_handler(CommandHandler("random", require_access(random_command)))
    application.add_handler(CommandHandler("cancel", require_access(cancel_command)))
    application.add_handler(CommandHandler("gallery", require_access(gallery_command)))
    application.add_handler(CommandHandler("blacklist", require_access(blacklist_command)))
    application.add_handler(CommandHandler(
        "subscriptions", require_access(subscriptions_command)))
    application.add_handler(CommandHandler("history", require_access(history_command)))
    application.add_handler(CommandHandler("favorites", require_access(favorites_command)))
    application.add_handler(CommandHandler("collections", require_access(collections_command)))
    application.add_handler(CommandHandler("presets", require_access(presets_command)))
    application.add_handler(CommandHandler("recommendations", require_access(recommendations_command)))
    application.add_handler(CommandHandler("later", require_access(later_command)))
    application.add_handler(CommandHandler("storage", require_access(storage_command)))
    application.add_handler(CommandHandler("stats", require_access(stats_command)))
    application.add_handler(CommandHandler("whyblocked", require_access(whyblocked_command)))
    application.add_handler(CommandHandler("settings", require_access(settings_command)))
    application.add_handler(CommandHandler("restart", require_access(restart_command)))
    application.add_handler(CommandHandler("update", require_access(update_command)))
    application.add_handler(CommandHandler("update_check", require_access(update_check_command)))
    application.add_handler(CommandHandler("version", require_access(version_command)))
    application.add_handler(CommandHandler("health", require_access(health_command)))
    application.add_handler(CommandHandler("adminstats", require_access(admin_stats_command)))
    application.add_handler(CommandHandler("retry_failed", require_access(retry_failed_command)))
    application.add_handler(CommandHandler("tags", require_access(tags_command)))
    application.add_handler(CommandHandler("id", require_access(id_command)))
    application.add_handler(CallbackQueryHandler(require_access(button_handler)))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, require_access(message_handler))
    )
    application.add_error_handler(error_handler)

    logger.info("Бот запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    if restart_requested:
        sys.exit(RESTART_EXIT_CODE)


def run_with_instance_lifecycle(
    lifecycle: BotInstanceLifecycle,
    runner=build_and_run_application,
) -> None:
    """Run the whole application lifecycle while exclusively owning the OS lock."""
    global instance_lifecycle, instance_lock_wait_ms, startup_orphans_deleted
    instance_lifecycle = lifecycle
    try:
        result = asyncio.run(lifecycle.start())
        instance_lock_wait_ms = result.wait_ms
        startup_orphans_deleted = result.cleanup.deleted
        runner()
    finally:
        lifecycle.close()
        if instance_lifecycle is lifecycle:
            instance_lifecycle = None


def main():
    """Запуск бота"""
    configure_logging()
    missing_config = validate_config()
    if missing_config:
        logger.error(
            "Не установлены обязательные переменные окружения: %s",
            ", ".join(missing_config),
        )
        return

    lifecycle = create_instance_lifecycle(
        database_path=DB_PATH,
        wait_seconds=INSTANCE_LOCK_WAIT_SECONDS,
        retry_interval_seconds=INSTANCE_LOCK_RETRY_INTERVAL_SECONDS,
    )
    try:
        run_with_instance_lifecycle(lifecycle)
    except InstanceLockBusy as exc:
        logger.error("Запуск отменён: %s", exc)
    except InstanceLockLifecycleError as exc:
        logger.error("Запуск отменён: %s", exc)


if __name__ == "__main__":
    main()
