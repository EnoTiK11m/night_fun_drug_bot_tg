import logging
import asyncio
import time
import random
import os
import sys
import tempfile
import zipfile
import json
import shutil
import io
import re
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse

import aiohttp
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
from telegram.error import BadRequest, RetryAfter
from config import (
    ALLOW_GROUP_CHATS,
    ALLOWED_CHAT_IDS,
    ALLOWED_USER_IDS,
    ADMIN_USER_IDS,
    API_KEY,
    API_USER_ID,
    BOT_TOKEN,
    DB_PATH,
    SEARCH_COOLDOWN_SECONDS,
    SUBSCRIPTION_CHECK_INTERVAL_SECONDS,
    SUBSCRIPTION_MAX_POSTS_PER_USER_PASS,
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
    _download_photo_file,
    get_media_url_candidates,
    media_url_path_lower,
    send_post_media as send_post_media_with_retries,
    send_post_media_to_chat as send_post_media_to_chat_with_retries,
    send_text_to_chat,
)
from bot_delivery import telegram_rate_limiter
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
    get_due_subscriptions,
    claim_due_subscription,
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
    get_delivery_failures,
    delete_delivery_failure,
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
    pop_subscription_digest,
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
user_states = {}
search_builders: dict[int, dict] = {}
pending_preset_queries: dict[int, str] = {}
pending_bulk_posts: dict[int, list[int]] = {}
pending_subscription_options: dict[int, str] = {}
# Глобальная задача для подписок
subscription_task = None
heartbeat_task = None
tag_translation_task = None
user_last_search_at = {}
MEDIA_SEND_RETRIES = 2
SUBSCRIPTION_CONCURRENCY = 5
HEARTBEAT_INTERVAL_SECONDS = 5 * 60
FAVORITES_EXPORT_ZIP_LIMIT_BYTES = 45 * 1024 * 1024
FAVORITES_EXPORT_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
FAVORITES_EXPORT_COOLDOWN_SECONDS = 5 * 60
POST_TAGS_PAGE_SIZE = 8
FAVORITES_GALLERY_PAGE_SIZE = 10
RESTART_EXIT_CODE = 42
restart_requested = False
RESTART_TEXT_COMMANDS = {"restart", "рестарт"}
favorites_export_users: set[int] = set()
favorites_export_last_finished_at: dict[int, float] = {}
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


def get_subscription_preview(query: str, interval: int) -> tuple[str, InlineKeyboardMarkup]:
    payload = json.dumps({"query": query, "interval": interval}, ensure_ascii=False)
    confirm_callback = store_callback_payload("sub_create", payload)
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
    )


async def build_subscriptions_menu_text(user_id: int) -> str:
    remaining = format_remaining_pause(await get_subscription_pause_until(user_id))
    status = (
        f"⏸ Сейчас приостановлены, осталось: {remaining}."
        if remaining
        else "✅ Сейчас работают по расписанию."
    )
    return (
        "🔔 *Подписки*\n\n"
        "Бот автоматически пришлёт новые посты по сохранённым запросам.\n\n"
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
            await message.reply_media_group(media=media)
            return album_posts, rejected_posts
        except Exception as exc:
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
    message, post_id: int, page: int = 0, edit: bool = False
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
                callback_data=store_callback_payload("tag_block", tag),
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
    user_states.pop(user_id, None)
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
    try:
        await query.answer(text=text)
    except BadRequest as exc:
        if "Query is too old" in str(exc) or "query id is invalid" in str(exc):
            logger.warning("Ignoring expired callback query answer: %s", exc)
            return
        raise


def is_access_allowed(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id if user else None
    chat_id = chat.id if chat else None
    chat_type = getattr(chat, "type", None)

    if user_id in ADMIN_USER_IDS:
        return True
    if chat_id in ALLOWED_CHAT_IDS:
        return True
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return False
    if chat_type in {"group", "supergroup", "channel"}:
        return ALLOW_GROUP_CHATS
    return True


async def send_access_denied(update: Update):
    text = "Доступ к боту ограничен."
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
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
        return await handler(update, context)

    return wrapped


def schedule_background_task(context: ContextTypes.DEFAULT_TYPE, coroutine):
    application = getattr(context, "application", None)
    if application and hasattr(application, "create_task"):
        application.create_task(coroutine)
    else:
        asyncio.create_task(coroutine)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий кнопок"""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    deferred_answer = data.startswith((
        "later_add_",
        "tag_block_",
        "bl_quick_",
        "gallery_bulk_fav_",
    ))
    if not deferred_answer:
        await safe_query_answer(query)

    if data == "cancel_input":
        user_states.pop(user_id, None)
        search_builders.pop(user_id, None)
        pending_preset_queries.pop(user_id, None)
        pending_bulk_posts.pop(user_id, None)
        pending_subscription_options.pop(user_id, None)
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
        user_states[user_id] = "waiting_search"
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
        schedule_background_task(context, send_random_image(query.message, user_id))

    elif data == "more":
        saved = await get_user_query(user_id)
        if saved and saved[0]:
            schedule_background_task(
                context,
                send_image(query.message, user_id, saved[0], edit=False, is_more=True),
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
        user_states[user_id] = "waiting_gallery"
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
            context, send_search_gallery(query.message, user_id, tags, page)
        )

    elif data == "search_builder":
        search_builders[user_id] = {}
        user_states[user_id] = "waiting_builder_include"
        await query.message.reply_text(
            "🧩 *Конструктор поиска*\n\nВведите обязательные теги через пробел.",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("search_hub"),
        )

    elif data == "presets":
        await show_search_presets(query.message, user_id)

    elif data == "preset_save_current":
        saved = await get_user_query(user_id)
        if not saved or not saved[0]:
            await query.message.reply_text("Сначала выполните поиск.")
        else:
            pending_preset_queries[user_id] = saved[0]
            user_states[user_id] = "waiting_preset_name"
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
            settings = await get_user_settings(user_id)
            settings.update(preset["settings"])
            await save_user_settings(user_id, settings)
            schedule_background_task(context, send_search_gallery(query.message, user_id, preset["query"]))

    elif data.startswith("preset_del_"):
        value = data.replace("preset_del_", "", 1)
        if value.isdigit():
            await delete_search_preset(user_id, int(value))
        await show_search_presets(query.message, user_id)

    elif data.startswith("preset_from_"):
        preset_query = get_callback_payload("preset_from", data)
        if preset_query:
            pending_preset_queries[user_id] = preset_query
            user_states[user_id] = "waiting_preset_name"
            await query.message.reply_text(
                "Введите название сохранённого запроса:",
                reply_markup=get_cancel_keyboard("search_hub"),
            )

    elif data.startswith("builder_run_"):
        built_query = get_callback_payload("builder_run", data)
        if built_query:
            schedule_background_task(context, send_search_gallery(query.message, user_id, built_query))

    elif data == "recommendations":
        schedule_background_task(context, send_recommendations(query.message, user_id))

    elif data.startswith("rec_hide_"):
        tag = get_callback_payload("rec_hide", data)
        if tag:
            settings = await get_user_settings(user_id)
            excluded = set(str(settings.get("recommendation_excluded_tags", "")).split())
            excluded.add(tag)
            settings["recommendation_excluded_tags"] = " ".join(sorted(excluded)[:100])
            await save_user_settings(user_id, settings)
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
                    context, send_search_gallery(query.message, user_id, similar_tags)
                )
            else:
                await query.message.reply_text("Недостаточно характерных тегов для похожей подборки.")

    elif data.startswith("tag_search_"):
        tag = get_callback_payload("tag_search", data)
        if tag:
            schedule_background_task(context, send_search_gallery(query.message, user_id, tag))

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
        await show_read_later(query.message, user_id)

    elif data.startswith("later_open_"):
        value = data.replace("later_open_", "", 1)
        posts = await get_read_later(user_id, 100)
        post = next((item for item in posts if str(item.get("id")) == value), None)
        if post:
            settings = await get_user_settings(user_id)
            await send_post_media(query.message, post, settings=settings)
        else:
            await query.message.reply_text("Пост больше не находится в списке.")

    elif data.startswith("later_del_"):
        value = data.replace("later_del_", "", 1)
        if value.isdigit():
            await remove_read_later(user_id, int(value))
        await show_read_later(query.message, user_id)

    elif data == "storage":
        await show_storage(query.message, user_id)

    elif data == "storage_cleanup_90":
        await query.message.reply_text(
            "Удалить историю и служебные записи старше 90 дней?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🧹 Удалить", callback_data="storage_cleanup_90_do"),
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
                InlineKeyboardButton("🗑 Удалить", callback_data="storage_empty_collections_do"),
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
        pending_bulk_posts[user_id] = [int(value) for value in raw_ids.split(",") if value.isdigit()][:10]
        collections = await get_favorite_collections(user_id)
        rows = [[InlineKeyboardButton(
            f"🗂 {item['name'][:28]}", callback_data=f"gallery_col_add_{item['id']}"
        )] for item in collections]
        rows.append([InlineKeyboardButton("➕ Новая коллекция", callback_data="gallery_col_new")])
        await query.message.reply_text(
            "Выберите коллекцию для всей подборки:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    elif data.startswith("gallery_col_add_"):
        value = data.replace("gallery_col_add_", "", 1)
        collection_id = int(value) if value.isdigit() else 0
        added = 0
        for post_id in pending_bulk_posts.pop(user_id, []):
            post = await get_known_post(post_id)
            if post:
                await add_favorite(user_id, post)
                if await add_favorite_to_collection(user_id, collection_id, post_id):
                    added += 1
        await query.message.reply_text(f"🗂 Добавлено в коллекцию: {added}.")

    elif data == "gallery_col_new":
        user_states[user_id] = "waiting_bulk_collection_name"
        await query.message.reply_text(
            "Введите название новой коллекции для этой подборки:",
            reply_markup=get_cancel_keyboard("library"),
        )

    elif data == "settings_spoiler":
        settings = normalize_feature_settings(await get_user_settings(user_id))
        values = ["off", "explicit", "all"]
        settings["spoiler_mode"] = values[(values.index(settings["spoiler_mode"]) + 1) % len(values)]
        await save_user_settings(user_id, settings)
        labels = {"off": "выключены", "explicit": "только explicit", "all": "для всех медиа"}
        await query.message.reply_text(
            f"🙈 Спойлеры: {labels[settings['spoiler_mode']]}",
            reply_markup=await get_user_settings_keyboard(user_id),
        )

    elif data == "sub_digest_send":
        posts = await pop_subscription_digest(user_id, 10)
        delivered = await send_digest_posts(query.message, user_id, posts)
        if posts and not delivered:
            for post in posts:
                await enqueue_subscription_digest(
                    user_id, post.get("subscription_query", "digest"), post
                )

    elif data.startswith("sub_options_"):
        sub_query = get_callback_payload("sub_options", data)
        if sub_query:
            await show_subscription_options(query.message, user_id, sub_query)

    elif data.startswith((
        "subopt_rating_", "subopt_type_", "subopt_orientation_", "subopt_resolution_",
        "subopt_quality_", "subopt_blacklist_", "subopt_digest_",
    )):
        action, token = data.split("_", 2)[1:]
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
            pending_subscription_options[user_id] = sub_query
            user_states[user_id] = "waiting_subscription_blacklist"
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
                InlineKeyboardButton("✅ Очистить", callback_data="stats_clear_do"),
                InlineKeyboardButton("Отмена", callback_data="stats"),
            ]]),
        )

    elif data == "stats_clear_do":
        await clear_user_activity_stats(user_id)
        await query.message.reply_text("✅ Персональная статистика очищена.")

    elif data == "fav_collections":
        await show_collections(query.message, user_id)

    elif data == "col_create":
        user_states[user_id] = "waiting_collection_create"
        await query.message.reply_text(
            "Введите название коллекции (до 40 символов):",
            reply_markup=get_cancel_keyboard("fav_collections"),
        )

    elif data.startswith("col_open_"):
        value = data.replace("col_open_", "", 1)
        if value.isdigit():
            await show_collection(query.message, user_id, int(value))

    elif data.startswith("col_page_"):
        parts = data.split("_")
        if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
            await show_collection(query.message, user_id, int(parts[2]), int(parts[3]))

    elif data.startswith("col_delete_") and not data.startswith("col_delete_do_"):
        value = data.replace("col_delete_", "", 1)
        if value.isdigit():
            await query.message.reply_text(
                "Удалить коллекцию? Посты останутся в общем избранном.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🗑 Удалить", callback_data=f"col_delete_do_{value}"),
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
            user_states[user_id] = f"waiting_collection_rename_{value}"
            await query.message.reply_text(
                "Введите новое название коллекции:",
                reply_markup=get_cancel_keyboard("fav_collections"),
            )

    elif data.startswith("fav_col_pick_"):
        value = data.replace("fav_col_pick_", "", 1)
        if value.isdigit():
            await show_collection_picker(query.message, user_id, int(value))

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
            await show_collection(query.message, user_id, collection_id, index)

    elif data.startswith("col_export_"):
        value = data.replace("col_export_", "", 1)
        if value.isdigit():
            schedule_background_task(
                context,
                start_collection_zip_export(query.message, user_id, int(value)),
            )

    elif data.startswith("fav_note_"):
        value = data.replace("fav_note_", "", 1)
        if value.isdigit():
            user_states[user_id] = f"waiting_favorite_note_{value}"
            current = await get_favorite_note(user_id, int(value))
            await query.message.reply_text(
                "Введите заметку до 1000 символов. Отправьте `-`, чтобы удалить."
                + (f"\n\nСейчас: {current}" if current else ""),
                reply_markup=get_cancel_keyboard("library"),
            )

    elif data == "fav_gallery":
        await send_favorites_gallery(query.message, user_id)

    elif data == "fav_list":
        await show_favorites_list(query.message, user_id, edit=False, page=0)

    elif data == "fav_find":
        user_states[user_id] = "waiting_fav_tag"
        await query.edit_message_text(
            "🔎 Введите теги или слова из заметки для поиска в избранном:\n\n"
            "Пример: `blonde_hair wallpaper`",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("library"),
        )

    elif data == "fav_export":
        schedule_background_task(context, start_favorites_zip_export(query.message, user_id))

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
                int(post_id_text), show_tags_button=should_show_tags_button(settings)
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
        await send_full_post_tags(query.message, post_id, page=page, edit=True)

    elif data.startswith("post_tags_"):
        post_id_text = data.replace("post_tags_", "", 1)
        if not post_id_text.isdigit():
            await query.message.reply_text("❌ Не удалось определить пост.")
            return
        await send_full_post_tags(query.message, int(post_id_text))

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
        settings = normalize_feature_settings(await get_user_settings(user_id))
        if data == "gallery_cycle_sort":
            values = ["random", "new", "popular"]
            settings["gallery_sort"] = values[(values.index(settings["gallery_sort"]) + 1) % len(values)]
        elif data == "gallery_cycle_rating":
            values = ["all", "s", "q", "e"]
            settings["rating_filter"] = values[(values.index(settings["rating_filter"]) + 1) % len(values)]
        elif data == "gallery_cycle_type":
            values = ["all", "images", "animations", "videos"]
            settings["media_type"] = values[(values.index(settings["media_type"]) + 1) % len(values)]
        elif data == "gallery_cycle_orientation":
            values = ["any", "portrait", "landscape", "square"]
            settings["orientation"] = values[(values.index(settings["orientation"]) + 1) % len(values)]
        elif data == "gallery_size_down":
            settings["gallery_size"] = max(2, settings["gallery_size"] - 1)
        elif data == "gallery_size_up":
            settings["gallery_size"] = min(10, settings["gallery_size"] + 1)
        await save_user_settings(user_id, settings)
        await query.edit_message_text(
            gallery_settings_text(settings),
            reply_markup=get_gallery_settings_keyboard(settings),
            parse_mode="Markdown",
        )

    elif data == "gallery_resolution":
        user_states[user_id] = "waiting_gallery_resolution"
        await query.edit_message_text(
            "Введите минимальное разрешение как `ширинаxвысота`, например `1920x1080`. Для сброса: `0x0`.",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("settings_gallery"),
        )

    elif data == "quality_cycle_mode" or data.startswith("quality_max_"):
        settings = normalize_feature_settings(await get_user_settings(user_id))
        if data == "quality_cycle_mode":
            values = ["auto", "preview", "sample", "original"]
            settings["quality_mode"] = values[(values.index(settings["quality_mode"]) + 1) % len(values)]
        elif data == "quality_max_down":
            settings["max_file_mb"] = max(1, settings["max_file_mb"] - 1)
        elif data == "quality_max_up":
            settings["max_file_mb"] = min(50, settings["max_file_mb"] + 1)
        await save_user_settings(user_id, settings)
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
                InlineKeyboardButton("✅ Сбросить", callback_data="settings_reset_do"),
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
        settings = normalize_feature_settings(await get_user_settings(user_id))
        settings["interface_mode"] = (
            "advanced" if settings["interface_mode"] == "simple" else "simple"
        )
        await save_user_settings(user_id, settings)
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
        user_states[user_id] = "waiting_pause_subscriptions"
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

        # Получаем текущие настройки
        settings = await get_user_settings(user_id)
        current_value = settings.get(setting_name, True)

        # Обновляем настройку
        settings[setting_name] = not current_value

        # Если отключаем описание полностью, выключаем все остальные настройки
        if setting_name == "show_caption" and not current_value:
            settings.update(
                {
                    "show_search_query": False,
                    "show_subscription_label": False,
                    "show_id": False,
                    "show_score": False,
                    "show_rating": False,
                    "show_tags": False,
                    "show_tags_button": False,
                }
            )
        # Если включаем описание, включаем основные настройки
        elif setting_name == "show_caption" and current_value:
            settings["show_id"] = True
            settings["show_tags"] = True
            settings["show_tags_button"] = True

        # Обновляем сообщение
        await save_user_settings(user_id, settings)

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
            user_states[user_id] = f"waiting_sub_interval_{saved[0]}"
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
        user_states[user_id] = "waiting_sub_new"
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
                            callback_data=store_callback_payload("sub_toggle", sub_query),
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

        user_states[user_id] = f"waiting_sub_interval_update_{sub_query}"
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

        await send_subscription_post_by_index(query.message, user_id, sub_query, index)

    elif data.startswith("sub_all_"):
        token = data.replace("sub_all_", "", 1)
        sub_query = get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.message.reply_text(
                "❌ Не удалось найти подписку. Откройте список заново."
            )
            return

        await send_subscription_gallery(query.message, user_id, sub_query, token)

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

        await edit_subscription_gallery(query, user_id, sub_query, token, index)

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
        await edit_subscription_gallery(query, user_id, sub_query, token, index)

    elif data.startswith("sub_toggle_"):
        sub_query = get_callback_payload("sub_toggle", data)
        if not sub_query:
            await query.edit_message_text(
                "❌ Не удалось найти подписку. Откройте список подписок заново.",
                parse_mode="Markdown",
            )
            return

        new_state = await toggle_subscription(user_id, sub_query)
        if new_state is None:
            await query.edit_message_text(
                "❌ Подписка не найдена.", parse_mode="Markdown"
            )
        else:
            state_text = "запущена" if new_state else "остановлена"
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
        success = await add_subscription(user_id, sub_query, interval)
        await query.edit_message_text(
            await build_subscription_added_text(sub_query, interval, user_id)
            if success
            else "❌ Не удалось создать подписку.",
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

        preview_text, preview_keyboard = get_subscription_preview(sub_query, 10)
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

        confirm_callback = store_callback_payload("sub_remove_do", sub_query)
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
                    callback_data=f"fav_remove_do_{post_id_text}_{page}",
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
            ),
            settings=settings,
        )

    elif data == "fav_all":
        await send_favorites_gallery(query.message, user_id)

    elif data.startswith("fav_page_"):
        page_text = data.replace("fav_page_", "", 1)
        if not page_text.isdigit():
            await query.message.reply_text("❌ Не удалось открыть страницу.")
            return

        await send_favorites_gallery(query.message, user_id, int(page_text))

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
                    "🗑 Удалить", callback_data=f"fav_del_do_{post_id}_{index}"
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
        await edit_favorites_gallery(query, user_id, index)

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
                    InlineKeyboardButton("🗂 В коллекцию", callback_data=f"fav_col_pick_{post_id}")
                ]]),
                parse_mode="Markdown",
            )
        else:
            await query.message.reply_text(
                f"⭐ Пост `{md_code(post_id)}` добавлен в избранное.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🗂 В коллекцию", callback_data=f"fav_col_pick_{post_id}")
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
                    InlineKeyboardButton("🗂 В коллекцию", callback_data=f"fav_col_pick_{post_id}")
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
            send_image(query.message, user_id, history_query),
        )

    elif data == "bl_add":
        user_states[user_id] = "waiting_bl_add"
        await query.edit_message_text(
            "➕ Введите тег для добавления в чёрный список:\n\n"
            "💡 Можно ввести несколько тегов через пробел",
            reply_markup=get_cancel_keyboard("blacklist"),
        )

    elif data == "bl_remove":
        user_states[user_id] = "waiting_bl_remove"
        blacklist = await get_user_blacklist(user_id)
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
        user_states[user_id] = "waiting_bl_temp"
        await query.edit_message_text(
            "Введите тег и срок: `tag 2ч`, `tag 1д` или `tag 30` (минуты).",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("blacklist"),
        )

    elif data == "bl_import":
        user_states[user_id] = "waiting_bl_import"
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
        user_states[user_id] = "waiting_bl_suggest"
        await query.edit_message_text(
            "Введите тег, для которого найти похожие варианты:",
            reply_markup=get_cancel_keyboard("blacklist"),
        )

    elif data == "bl_presets":
        rows = []
        for preset, tags in BLACKLIST_PRESETS.items():
            rows.append([
                InlineKeyboardButton(
                    f"➕ {preset} ({len(tags)})", callback_data=f"bl_preset_add_{preset}"
                ),
                InlineKeyboardButton("➖", callback_data=f"bl_preset_del_{preset}"),
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
        user_states.pop(user_id, None)
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


async def send_random_image(message, user_id: int):
    """Отправка случайного изображения без поисковых тегов."""
    blacklist = await get_user_blacklist(user_id)
    settings = await get_user_settings(user_id)

    if is_rate_limited(user_id):
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
        filter_attempts = 0
        while result and not post_matches_preferences(result, filter_settings) and filter_attempts < 4:
            try:
                excluded_post_ids.add(int(result.get("id")))
            except (TypeError, ValueError):
                pass
            result = await api.get_global_random_image(blacklist, excluded_post_ids)
            filter_attempts += 1
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
            "❌ Не удалось найти случайную картинку с учётом чёрного списка.",
            reply_markup=get_main_keyboard(),
        )
        return False

    await remember_and_cache_post(result)
    post_id = result.get("id", 0)

    caption = ""
    if settings.get("show_caption", True):
        caption = await build_caption(settings, result)

    delivered = await send_post_media(
        message,
        result,
        caption,
        get_random_image_keyboard(
            post_id,
            should_show_tags_button(settings),
        ),
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
):
    """Отправка изображения"""
    blacklist = await get_user_blacklist(user_id)
    settings = await get_user_settings(user_id)

    if not is_subscription and is_rate_limited(user_id):
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
            filter_attempts = 0
            while result and not post_matches_preferences(result, filter_settings) and filter_attempts < 4:
                result = await api.get_next_image(
                    user_id, tags, blacklist, excluded_post_ids
                )
                filter_attempts += 1
        else:
            result = await api.get_random_image(tags, blacklist, excluded_post_ids)
            filter_settings = normalize_feature_settings(settings)
            filter_attempts = 0
            while result and not post_matches_preferences(result, filter_settings) and filter_attempts < 4:
                try:
                    excluded_post_ids.add(int(result.get("id")))
                except (TypeError, ValueError):
                    pass
                result = await api.get_random_image(tags, blacklist, excluded_post_ids)
                filter_attempts += 1
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
        if is_subscription:
            keyboard = get_subscription_image_keyboard(
                post_id,
                tags,
                show_tags_button,
            )
        else:
            keyboard = get_image_keyboard(post_id, tags, show_tags_button)

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
    message, post: dict, caption: str = "", keyboard=None, settings: dict | None = None
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
    )


async def send_post_media_to_chat(
    bot, chat_id: int, post: dict, caption: str = "", keyboard=None,
    settings: dict | None = None,
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
    )


def image_extension_from_url(url: str) -> str:
    path = urlparse(url).path.lower()
    _, extension = os.path.splitext(path)
    if extension in FAVORITES_EXPORT_IMAGE_EXTENSIONS:
        return extension
    return ""


def is_exportable_image_url(url: str) -> bool:
    return bool(image_extension_from_url(url))


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


async def download_original_favorite_image(
    session: aiohttp.ClientSession,
    post: dict,
) -> tuple[str, bytes] | None:
    post = await ensure_favorite_original_url(post)
    url = post.get("file_url", "")
    extension = image_extension_from_url(url)
    if not extension:
        return None

    post_id = post.get("id", "unknown")
    filename = f"{post_id}{extension}"
    photo = await _download_photo_file(
        url, max_bytes=FAVORITES_EXPORT_ZIP_LIMIT_BYTES
    )
    try:
        return filename, photo.read()
    finally:
        photo.close()


async def send_favorites_zip_export(
    message, user_id: int, favorites: list[dict] | None = None, title: str = "Избранное"
):
    total = len(favorites) if favorites is not None else await count_favorites(user_id)
    if total <= 0:
        await message.reply_text("⭐ Избранное пока пустое.")
        return

    await message.reply_text(
        f"📦 Собираю ZIP «{title}»: {total} постов. Беру только оригинальные картинки."
    )

    if favorites is None:
        favorites = await get_favorites(user_id, limit=None)
    exported = 0
    skipped = 0
    part = 1

    with tempfile.TemporaryDirectory(prefix=f"favorites_{user_id}_") as tempdir:
        archive_path = os.path.join(tempdir, f"favorites_{part}.zip")
        archive = zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED)
        archive_size = 0
        archive_count = 0
        sent_parts = 0

        async with aiohttp.ClientSession() as session:
            for favorite in favorites:
                downloaded = None
                try:
                    downloaded = await download_original_favorite_image(session, favorite)
                except Exception as exc:
                    logger.warning(
                        "Favorite export failed post=%s: %s",
                        favorite.get("id"),
                        exc,
                    )

                if not downloaded:
                    skipped += 1
                    continue

                filename, data = downloaded
                if archive_count and archive_size + len(data) > FAVORITES_EXPORT_ZIP_LIMIT_BYTES:
                    archive.close()
                    with open(archive_path, "rb") as document:
                        await message.reply_document(
                            document=document,
                            filename=os.path.basename(archive_path),
                            caption=f"📦 {title}, часть {part}",
                        )
                    sent_parts += 1
                    part += 1
                    archive_path = os.path.join(tempdir, f"favorites_{part}.zip")
                    archive = zipfile.ZipFile(
                        archive_path,
                        "w",
                        compression=zipfile.ZIP_DEFLATED,
                    )
                    archive_size = 0
                    archive_count = 0

                archive.writestr(filename, data)
                archive_size += len(data)
                archive_count += 1
                exported += 1

        archive.close()
        if archive_count:
            with open(archive_path, "rb") as document:
                await message.reply_document(
                    document=document,
                    filename=os.path.basename(archive_path),
                    caption=f"📦 {title}, часть {part}",
                )
            sent_parts += 1

    if exported:
        await message.reply_text(
            f"✅ Готово: {exported} картинок в {sent_parts} ZIP. Пропущено: {skipped}."
        )
    else:
        await message.reply_text(
            "❌ Не нашлось оригинальных картинок для архива. GIF, видео и посты без доступного file_url пропущены."
        )


async def start_favorites_zip_export(message, user_id: int):
    if user_id in favorites_export_users:
        await message.reply_text("📦 Архив избранного уже собирается.")
        return

    now = time.monotonic()
    last_finished_at = favorites_export_last_finished_at.get(user_id, 0)
    remaining = FAVORITES_EXPORT_COOLDOWN_SECONDS - (now - last_finished_at)
    if remaining > 0:
        minutes = max(1, int((remaining + 59) // 60))
        await message.reply_text(
            f"📦 Экспорт уже недавно запускался. Попробуйте через {minutes} мин."
        )
        return

    favorites_export_users.add(user_id)
    try:
        await send_favorites_zip_export(message, user_id)
    finally:
        favorites_export_users.discard(user_id)
        favorites_export_last_finished_at[user_id] = time.monotonic()


async def start_collection_zip_export(message, user_id: int, collection_id: int):
    collection = await get_favorite_collection(user_id, collection_id)
    if not collection:
        await message.reply_text("❌ Коллекция не найдена.")
        return
    posts = await get_collection_favorites(user_id, collection_id, limit=None)
    if not posts:
        await message.reply_text("❌ Коллекция пуста.")
        return
    if user_id in favorites_export_users:
        await message.reply_text("📦 Другой архив уже собирается.")
        return
    favorites_export_users.add(user_id)
    try:
        await send_favorites_zip_export(
            message, user_id, favorites=posts, title=collection["name"]
        )
    finally:
        favorites_export_users.discard(user_id)
        favorites_export_last_finished_at[user_id] = time.monotonic()


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
    message, user_id: int, sub_query: str, index: int
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
        ), settings=settings
    )


async def send_subscription_gallery(
    message, user_id: int, sub_query: str, token: str, index: int = 0
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
        ),
        settings=settings,
    )


async def edit_subscription_gallery(
    query, user_id: int, sub_query: str, token: str, index: int
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
    )
    try:
        await query.edit_message_media(
            media=media_from_post(post, caption, should_spoiler(settings, post)),
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"Ошибка обновления галереи подписки: {e}")
        await send_post_media(query.message, post, caption, keyboard, settings=settings)


async def send_favorites_gallery(message, user_id: int, page: int = 0):
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


async def edit_favorites_gallery(query, user_id: int, index: int):
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


async def send_search_gallery(message, user_id: int, tags: str, page: int = 0):
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
            delivered = await send_post_media(
                message,
                post,
                keyboard=get_image_keyboard(
                    int(post.get("id") or 0),
                    query=tags,
                    show_tags_button=should_show_tags_button(settings),
                ),
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
    await message.reply_text(
        f"Показано: {len(delivered_ids)}. Страница источника: {page + 1}.",
        reply_markup=get_gallery_result_keyboard(
            next_callback,
            previous_callback,
            store_callback_payload(
                "gallery_bulk_fav", ",".join(str(post_id) for post_id in delivered_ids)
            ) if delivered_ids else None,
            store_callback_payload("preset_from", tags) if tags else None,
            store_callback_payload("subscribe", tags) if tags else None,
            store_callback_payload(
                "gallery_collection", ",".join(str(post_id) for post_id in delivered_ids)
            ) if delivered_ids else None,
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


async def show_collection(message, user_id: int, collection_id: int, index: int = 0):
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
            InlineKeyboardButton("📝 Заметка", callback_data=f"fav_note_{post['id']}"),
            InlineKeyboardButton(
                "➖ Из коллекции", callback_data=f"col_remove_{collection_id}_{post['id']}_{index}"
            ),
        ],
        [InlineKeyboardButton("📦 ZIP коллекции", callback_data=f"col_export_{collection_id}")],
        [InlineKeyboardButton("◀️ Коллекции", callback_data="fav_collections")],
    ])
    settings = await get_user_settings(user_id)
    await send_post_media(message, post, caption, keyboard, settings=settings)


async def show_collection_picker(message, user_id: int, post_id: int):
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
        f"🗂 {item['name'][:28]}", callback_data=f"col_add_{item['id']}_{post_id}"
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


async def show_search_presets(message, user_id: int):
    presets = await get_search_presets(user_id)
    rows = []
    lines = []
    for item in presets:
        lines.append(f"• *{md_text(item['name'])}*: `{md_code(item['query'])}`")
        rows.append([
            InlineKeyboardButton("▶️ " + item["name"][:24], callback_data=f"preset_run_{item['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"preset_del_{item['id']}"),
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


async def show_read_later(message, user_id: int):
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
    rows = []
    lines = []
    for post in posts[:10]:
        post_id = int(post.get("id") or 0)
        tags = str(post.get("tags") or "")[:45]
        lines.append(f"• `{post_id}` {md_text(tags)}")
        rows.append([
            InlineKeyboardButton(f"📤 {post_id}", callback_data=f"later_open_{post_id}"),
            InlineKeyboardButton("✅ Убрать", callback_data=f"later_del_{post_id}"),
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


async def send_recommendations(message, user_id: int):
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
    await message.reply_text(
        "✨ Подборка по частым тегам избранного: "
        + ", ".join(f"`{md_code(tag)}`" for tag, _count in profile[:3]),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"🚫 Не рекомендовать {tag[:24]}",
                callback_data=store_callback_payload("rec_hide", tag),
            )]
            for tag, _count in profile[:3]
        ]),
        parse_mode="Markdown",
    )
    return await send_search_gallery(message, user_id, tags)


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


async def show_subscription_options(message, user_id: int, sub_query: str):
    options = await get_subscription_options(user_id, sub_query)
    rating = options.get("rating_filter", "all")
    media_type = options.get("media_type", "all")
    orientation = options.get("orientation", "any")
    resolution = f"{options.get('min_width', 0)}×{options.get('min_height', 0)}"
    quality = options.get("quality_mode", "auto")
    extra_blacklist = str(options.get("extra_blacklist", ""))
    digest = options.get("digest_mode", "instant")
    token = store_callback_payload("sub_options", sub_query)
    await message.reply_text(
        f"🎛 *Фильтры подписки* `{md_code(sub_query)}`",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Rating: {rating}", callback_data=f"subopt_rating_{token}")],
            [InlineKeyboardButton(f"Тип: {media_type}", callback_data=f"subopt_type_{token}")],
            [InlineKeyboardButton(f"Ориентация: {orientation}", callback_data=f"subopt_orientation_{token}")],
            [InlineKeyboardButton(f"Разрешение: {resolution}", callback_data=f"subopt_resolution_{token}")],
            [InlineKeyboardButton(f"Качество: {quality}", callback_data=f"subopt_quality_{token}")],
            [InlineKeyboardButton(
                f"Чёрный список: {extra_blacklist[:20] or 'общий'}",
                callback_data=f"subopt_blacklist_{token}",
            )],
            [InlineKeyboardButton(
                "📨 Дайджест" if digest == "digest" else "⚡ Сразу",
                callback_data=f"subopt_digest_{token}",
            )],
            [InlineKeyboardButton("◀️ Подписки", callback_data="sub_manage")],
        ]),
        parse_mode="Markdown",
    )


async def send_digest_posts(message, user_id: int, posts: list[dict]) -> bool:
    if not posts:
        await message.reply_text("📨 Дайджест пока пуст.")
        return False
    settings = normalize_feature_settings(await get_user_settings(user_id))
    prepared = prepare_gallery_album_posts(posts, settings, 10)
    if len(prepared) > 1:
        media = []
        for index, post in enumerate(prepared):
            caption = "📨 Дайджест подписок" if index == 0 else ""
            media.append(media_from_post(post, caption, should_spoiler(settings, post)))
        try:
            await message.reply_media_group(media=media)
            return True
        except Exception as exc:
            logger.warning("Digest album failed, using sequential delivery: %s", exc)
    delivered = False
    for post in prepared or posts[:10]:
        delivered = await send_post_media(message, post, settings=settings) or delivered
    return delivered


async def send_digest_to_chat(bot, user_id: int, posts: list[dict]) -> bool:
    if not posts:
        return False
    settings = normalize_feature_settings(await get_user_settings(user_id))
    prepared = prepare_gallery_album_posts(posts, settings, 10)
    if len(prepared) > 1:
        media = [
            media_from_post(
                post,
                "📨 Дайджест подписок" if index == 0 else "",
                should_spoiler(settings, post),
            )
            for index, post in enumerate(prepared)
        ]
        try:
            await telegram_rate_limiter.wait_for_slot(user_id)
            await bot.send_media_group(chat_id=user_id, media=media)
            return True
        except RetryAfter as exc:
            telegram_rate_limiter.apply_retry_after(user_id, exc)
        except Exception as exc:
            logger.warning("Scheduled digest album failed, using sequential delivery: %s", exc)
    delivered = 0
    for index, post in enumerate(posts[:10]):
        caption = "📨 Дайджест подписок" if index == 0 else ""
        delivered += bool(
            await send_post_media_to_chat(
                bot, user_id, post, caption=caption, settings=settings
            )
        )
    return delivered > 0


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    state = user_states.get(user_id)
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
    if text in persistent_actions:
        user_states.pop(user_id, None)
        search_builders.pop(user_id, None)
        pending_preset_queries.pop(user_id, None)
        pending_bulk_posts.pop(user_id, None)
        pending_subscription_options.pop(user_id, None)
        state = None

    if text.lower() in {"отмена", "❌ отмена", "cancel", "/cancel"}:
        user_states.pop(user_id, None)
        search_builders.pop(user_id, None)
        pending_preset_queries.pop(user_id, None)
        pending_bulk_posts.pop(user_id, None)
        pending_subscription_options.pop(user_id, None)
        await update.message.reply_text(
            "Действие отменено.\n\n" + await build_main_menu_text(user_id),
            reply_markup=await get_user_main_keyboard(user_id),
        )

    elif text in {PERSISTENT_SEARCH, LEGACY_PERSISTENT_SEARCH}:
        await update.message.reply_text(
            "Главная → Поиск\n\nВыберите способ поиска.",
            reply_markup=get_search_hub_keyboard(),
            parse_mode="Markdown",
        )

    elif text == PERSISTENT_GALLERY:
        user_states[user_id] = "waiting_gallery"
        await update.message.reply_text(
            "🖼 Введите теги для подборки или `random` для случайных изображений.",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("search_hub"),
        )

    elif text in {PERSISTENT_RANDOM, LEGACY_PERSISTENT_RANDOM}:
        user_states.pop(user_id, None)
        schedule_background_task(context, send_random_image(update.message, user_id))

    elif text in {PERSISTENT_FAVORITES, LEGACY_PERSISTENT_FAVORITES}:
        user_states.pop(user_id, None)
        total = await count_favorites(user_id)
        later_count = (await get_user_storage_stats(user_id)).get("read_later", 0)
        await update.message.reply_text(
            f"Главная → Библиотека\n\nИзбранное: `{total}`\nНа потом: `{later_count}`",
            reply_markup=get_library_keyboard(),
            parse_mode="Markdown",
        )

    elif text == PERSISTENT_SUBSCRIPTIONS:
        user_states.pop(user_id, None)
        await update.message.reply_text(
            await build_subscriptions_menu_text(user_id),
            reply_markup=await get_user_subscriptions_keyboard(user_id),
            parse_mode="Markdown",
        )

    elif text in {PERSISTENT_MENU, LEGACY_PERSISTENT_MENU}:
        user_states.pop(user_id, None)
        await update.message.reply_text(
            await build_main_menu_text(user_id),
            reply_markup=await get_user_main_keyboard(user_id),
            parse_mode="Markdown",
        )

    elif text.lower() in RESTART_TEXT_COMMANDS:
        await request_restart(update, context)

    elif state == "waiting_search":
        user_states.pop(user_id, None)
        schedule_background_task(context, send_image(update.message, user_id, text))

    elif state == "waiting_gallery":
        user_states.pop(user_id, None)
        tags = "" if text.lower() in {"random", "рандом", "случайно"} else text
        schedule_background_task(context, send_search_gallery(update.message, user_id, tags))

    elif state == "waiting_builder_include":
        search_builders[user_id] = {"include": " ".join(text.split()[:12])}
        user_states[user_id] = "waiting_builder_exclude"
        await update.message.reply_text(
            "Введите исключаемые теги без минуса или отправьте `-`, если исключений нет.",
            reply_markup=get_cancel_keyboard("search_hub"),
        )

    elif state == "waiting_builder_exclude":
        user_states.pop(user_id, None)
        builder = search_builders.pop(user_id, {})
        include = builder.get("include", "")
        excluded = [] if text == "-" else text.split()[:12]
        built_query = " ".join([include] + [f"-{tag.lstrip('-')}" for tag in excluded]).strip()
        pending_preset_queries[user_id] = built_query
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
        user_states.pop(user_id, None)
        preset_query = pending_preset_queries.pop(user_id, "")
        settings = normalize_feature_settings(await get_user_settings(user_id))
        preset_id = await create_search_preset(user_id, text, preset_query, settings)
        await update.message.reply_text(
            "✅ Запрос сохранён." if preset_id else "❌ Пустое имя или такой запрос уже существует."
        )
        await show_search_presets(update.message, user_id)

    elif state == "waiting_bulk_collection_name":
        user_states.pop(user_id, None)
        post_ids = pending_bulk_posts.pop(user_id, [])
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
        user_states.pop(user_id, None)
        sub_query = pending_subscription_options.pop(user_id, "")
        options = await get_subscription_options(user_id, sub_query)
        options["extra_blacklist"] = "" if text == "-" else " ".join(text.lower().split()[:30])
        saved = await update_subscription_options(user_id, sub_query, options)
        await update.message.reply_text(
            "✅ Фильтр подписки обновлён." if saved else "❌ Подписка не найдена."
        )

    elif state == "waiting_collection_create":
        user_states.pop(user_id, None)
        collection_id = await create_favorite_collection(user_id, text)
        if collection_id:
            await update.message.reply_text(f"✅ Коллекция «{text[:40]}» создана.")
        else:
            await update.message.reply_text("❌ Пустое имя или коллекция уже существует.")
        await show_collections(update.message, user_id)

    elif state and state.startswith("waiting_collection_rename_"):
        user_states.pop(user_id, None)
        collection_id = state.replace("waiting_collection_rename_", "", 1)
        renamed = collection_id.isdigit() and await rename_favorite_collection(
            user_id, int(collection_id), text
        )
        await update.message.reply_text(
            "✅ Коллекция переименована." if renamed else "❌ Имя занято или коллекция не найдена."
        )
        await show_collections(update.message, user_id)

    elif state and state.startswith("waiting_favorite_note_"):
        user_states.pop(user_id, None)
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
        user_states.pop(user_id, None)
        normalized = text.lower().replace("×", "x").replace(" ", "")
        parts = normalized.split("x", 1)
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            await update.message.reply_text("❌ Формат: `1920x1080`.", parse_mode="Markdown")
        else:
            settings = await get_user_settings(user_id)
            settings["min_width"] = min(int(parts[0]), 10000)
            settings["min_height"] = min(int(parts[1]), 10000)
            await save_user_settings(user_id, settings)
            await update.message.reply_text(
                gallery_settings_text(settings),
                reply_markup=get_gallery_settings_keyboard(normalize_feature_settings(settings)),
                parse_mode="Markdown",
            )

    elif state == "waiting_bl_temp":
        user_states.pop(user_id, None)
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
        user_states.pop(user_id, None)
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
        user_states.pop(user_id, None)
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
        user_states.pop(user_id, None)
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
        user_states.pop(user_id, None)
        await show_favorite_search_results(update.message, user_id, text)

    elif state == "waiting_sub_new":
        user_states.pop(user_id, None)
        user_states[user_id] = f"waiting_sub_interval_{text}"
        await update.message.reply_text(
            f"🔔 Подписка на: `{md_code(text)}`\n\n"
            "Введите интервал в минутах от 1 до 120 (по умолчанию 10):",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard("subscriptions"),
        )

    elif state and state.startswith("waiting_sub_interval_update_"):
        sub_query = state.replace("waiting_sub_interval_update_", "", 1)
        user_states.pop(user_id, None)
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
        user_states.pop(user_id, None)

        interval = parse_subscription_interval(text)
        preview_text, preview_keyboard = get_subscription_preview(query, interval)
        await update.message.reply_text(
            preview_text,
            reply_markup=preview_keyboard,
            parse_mode="Markdown",
        )

    elif state == "waiting_bl_add":
        user_states.pop(user_id, None)
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
        user_states.pop(user_id, None)
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
        schedule_background_task(context, send_image(update.message, user_id, text))


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
    user_states.pop(user_id, None)
    search_builders.pop(user_id, None)
    pending_preset_queries.pop(user_id, None)
    pending_bulk_posts.pop(user_id, None)
    pending_subscription_options.pop(user_id, None)
    await update.message.reply_text(
        "Действие отменено.\n\n" + await build_main_menu_text(user_id),
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
    failures = await get_delivery_failures(limit=20)
    delivered = 0
    for failure in failures:
        ok = await send_post_media_to_chat(
            context.bot,
            failure["user_id"],
            failure["post"],
            failure["caption"],
        )
        if ok:
            delivered += 1
            await delete_delivery_failure(failure["id"])
    await update.message.reply_text(
        f"♻️ Повторено: {len(failures)}, доставлено: {delivered}, осталось: {len(failures) - delivered}."
    )


async def request_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global restart_requested

    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("❌ Недостаточно прав.")
        logger.warning("Unauthorized restart attempt user=%s", user_id)
        return

    restart_requested = True
    await update.message.reply_text("♻️ Перезапускаюсь...")
    logger.warning("Restart requested by admin user=%s", user_id)
    context.application.stop_running()


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only command to restart the bot via the launcher."""
    await request_restart(update, context)


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

    result = None
    caption = ""
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
                if updated and post_id:
                    await mark_post_sent(user_id, int(post_id))
                runtime_metrics.increment("subscription_digest_queued", int(queued))
                return bool(updated)
            keyboard = get_subscription_image_keyboard(
                post_id,
                query,
                should_show_tags_button(settings),
            )

            caption = ""
            if settings.get("show_caption", True):
                caption = await build_caption(settings, result, query, True)

            delivered = await send_post_media_to_chat(
                app.bot, user_id, result, caption, keyboard, settings=settings
            )
            if delivered:
                runtime_metrics.increment("subscription_delivered")
                updated = await update_subscription_time(user_id, query, processing_token)
                if updated and post_id:
                    await mark_post_sent(user_id, int(post_id))
                elif not updated:
                    logger.warning(
                        "Subscription claim expired before schedule update for user=%s query=%r",
                        user_id,
                        query,
                    )
            else:
                runtime_metrics.increment("subscription_failed")
                await save_delivery_failure(user_id, result, caption)
                await release_subscription_claim(user_id, query, processing_token)
            return bool(delivered)

        empty_count, backoff_minutes, should_notify = await mark_subscription_empty(
            user_id, query, processing_token
        )
        logger.info(
            "No new post for subscription user=%s query=%r; empty_count=%s backoff=%s",
            user_id,
            query,
            empty_count,
            backoff_minutes,
        )
        if should_notify:
            await send_text_to_chat(
                app.bot,
                user_id,
                text=(
                    f"🕒 По подписке `{md_code(query)}` пока нет новых постов.\n\n"
                    f"Я продолжу проверять ее реже: следующая проверка примерно через {backoff_minutes} мин. "
                    "Когда появится новый пост, подписка вернется к обычному интервалу."
                ),
                parse_mode="Markdown",
                reply_markup=await get_user_subscriptions_keyboard(user_id),
            )
        return False

    except APITemporaryError as e:
        await release_subscription_claim(user_id, query, processing_token)
        await note_upstream_failure(app, str(e))
        logger.warning(
            "Temporary Rule34 API error for subscription user=%s query=%r: %s",
            user_id,
            query,
            e,
        )
        return False
    except Exception as exc:
        if result:
            await save_delivery_failure(
                user_id, result, caption, error=f"{type(exc).__name__}: {exc}"
            )
        await release_subscription_claim(user_id, query, processing_token)
        logger.exception("Subscription processing error for user %s", user_id)
        return False


async def get_subscription_cached_image(
    user_id: int,
    query: str,
    blacklist: set,
    excluded_post_ids: set,
    settings: dict | None = None,
):
    cached_posts, _ = await get_subscription_cache(user_id, query)
    available_posts = [
        post for post in cached_posts
        if post.get("file_url")
        and post.get("id") not in excluded_post_ids
        and (settings is None or post_matches_preferences(post, settings))
    ]
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
            available_posts = [
                post for post in cached_posts
                if post.get("file_url")
                and post.get("id") not in excluded_post_ids
                and (settings is None or post_matches_preferences(post, settings))
            ]
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
                digest_posts = await pop_subscription_digest(digest_user_id, 10)
                if not await send_digest_to_chat(app.bot, digest_user_id, digest_posts):
                    for post in digest_posts:
                        await enqueue_subscription_digest(
                            digest_user_id, post.get("subscription_query", "digest"), post
                        )
            logger.info(
                "Subscription pass complete users=%s due=%s",
                len(subscriptions_by_user),
                len(due_subs),
            )
            await asyncio.sleep(SUBSCRIPTION_CHECK_INTERVAL_SECONDS)

        except Exception:
            logger.exception("Ошибка в фоновой задаче подписок")
            await asyncio.sleep(SUBSCRIPTION_CHECK_INTERVAL_SECONDS)


async def heartbeat_loop():
    started_at = time.monotonic()
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            logger.info(
                "Heartbeat alive uptime=%ss user_states=%s recent_posts=%s export_jobs=%s",
                int(time.monotonic() - started_at),
                len(user_states),
                len(recent_posts),
                len(favorites_export_users),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Heartbeat loop error")


async def post_init(application):
    global subscription_task, heartbeat_task, tag_translation_task

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

    # Запускаем фоновую задачу для подписок
    if subscription_task is None or subscription_task.done():
        subscription_task = asyncio.create_task(
            process_subscriptions(application))
        logger.info("Фоновая задача подписок запущена")

    if heartbeat_task is None or heartbeat_task.done():
        heartbeat_task = asyncio.create_task(heartbeat_loop())
        logger.info("Heartbeat task started")

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
    global subscription_task, heartbeat_task, tag_translation_task
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

    application = (
        Application.builder()
        .token(BOT_TOKEN)
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


if __name__ == "__main__":
    main()
