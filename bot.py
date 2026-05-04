import logging
import asyncio
import time
import random
import os
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
from telegram.error import BadRequest
from config import BOT_TOKEN, SEARCH_COOLDOWN_SECONDS, validate_config
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
    get_image_keyboard,
    get_main_keyboard,
    get_random_image_keyboard,
    get_settings_keyboard,
    get_subscription_gallery_keyboard,
    get_subscription_image_keyboard,
    get_subscriptions_keyboard,
)
from bot_media import (
    get_media_url_candidates,
    send_post_media as send_post_media_with_retries,
    send_post_media_to_chat as send_post_media_to_chat_with_retries,
)
from bot_state import (
    get_callback_payload,
    get_callback_payload_by_token,
    get_remembered_post,
    minimal_post,
    recent_posts,
    remember_post,
    store_callback_payload,
)
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
)
from api_handler import api, APITemporaryError


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        message = super().format(record)
        if BOT_TOKEN:
            message = message.replace(BOT_TOKEN, "<BOT_TOKEN>")
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
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(
            os.path.join("logs", "info.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        ),
        RotatingFileHandler(
            os.path.join("logs", "warnings.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        ),
    ]
    handlers[0].setLevel(logging.INFO)
    handlers[1].setLevel(logging.INFO)
    handlers[2].setLevel(logging.WARNING)
    handlers[1].addFilter(ExactLevelFilter(logging.INFO))
    for handler in handlers:
        handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)
    for handler in handlers:
        root_logger.addHandler(handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)


configure_logging()
logger = logging.getLogger(__name__)


# Состояния пользователей
user_states = {}
# Глобальная задача для подписок
subscription_task = None
user_last_search_at = {}
MEDIA_SEND_RETRIES = 2
SUBSCRIPTION_CONCURRENCY = 5


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
            "Главное меню:\n\n"
            f"⏸ Подписки приостановлены, осталось: {remaining}."
        )
    return "Главное меню:"


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


def media_from_post(post: dict, caption: str = ""):
    candidates = get_media_url_candidates(post)
    file_url = candidates[0][1] if candidates else ""
    media_caption = caption if caption else None
    if file_url.lower().endswith((".mp4", ".webm")):
        return InputMediaVideo(file_url, caption=media_caption, parse_mode="Markdown")
    if file_url.lower().endswith(".gif"):
        return InputMediaAnimation(
            file_url, caption=media_caption, parse_mode="Markdown"
        )
    return InputMediaPhoto(file_url, caption=media_caption, parse_mode="Markdown")


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


async def send_full_post_tags(message, post_id: int):
    post = await get_known_post(post_id) or minimal_post(post_id)
    for text in build_full_tags_messages(post):
        await message.reply_text(text, parse_mode="Markdown")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user_id = update.effective_user.id
    pause_until = await get_subscription_pause_until(user_id)
    remaining = format_remaining_pause(pause_until)
    pause_text = (
        f"\n\n⏸ Подписки приостановлены, осталось: {remaining}."
        if remaining
        else ""
    )
    await update.message.reply_text(
        "👋 Привет! Я бот для поиска изображений на rule34.\n\n"
        "🔍 *Основные функции:*\n"
        "• *Поиск* - поиск по тегам\n"
        "• *Рандомная картинка* - случайный пост с учётом blacklist\n"
        "• *Подписки* - автоматическая отправка каждые 10 минут\n"
        "• *Blacklist* - фильтрация нежелательных тегов\n"
        "• *Настройки* - управление описанием картинок\n\n"
        "⚙️ *Настройки описания:*\n"
        "Вы можете выбрать какие элементы показывать в описании:\n"
        "- Запрос поиска\n- ID поста\n- Очки (score)\n- Рейтинг\n- Теги\n- Метку подписки\n\n"
        "⚠️ Бот предназначен для пользователей"
        f"{pause_text}",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


async def safe_query_answer(query):
    try:
        await query.answer()
    except BadRequest as exc:
        if "Query is too old" in str(exc) or "query id is invalid" in str(exc):
            logger.warning("Ignoring expired callback query answer: %s", exc)
            return
        raise


def schedule_background_task(context: ContextTypes.DEFAULT_TYPE, coroutine):
    application = getattr(context, "application", None)
    if application and hasattr(application, "create_task"):
        application.create_task(coroutine)
    else:
        asyncio.create_task(coroutine)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий кнопок"""
    query = update.callback_query
    await safe_query_answer(query)

    user_id = query.from_user.id
    data = query.data

    if data == "search":
        user_states[user_id] = "waiting_search"
        await query.edit_message_text(
            "🔍 Введите теги для поиска (через пробел):\n\n"
            "Примеры:\n"
            "• `anime girl`\n"
            "• `2girls blonde_hair`\n"
            "• `solo male`\n\n"
            "💡 Используй `_` для тегов из нескольких слов",
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

    elif data == "blacklist":
        await query.edit_message_text(
            "🚫 *Настройки Blacklist*\n\n"
            "Теги в blacklist будут исключены из результатов поиска.",
            reply_markup=get_blacklist_keyboard(),
            parse_mode="Markdown",
        )

    elif data == "subscriptions":
        await query.edit_message_text(
            "🔔 *Управление подписками*\n\n"
            "Вы можете подписаться на поиск, и бот будет автоматически "
            "присылать новые посты по расписанию!",
            reply_markup=get_subscriptions_keyboard(),
            parse_mode="Markdown",
        )

    elif data == "history":
        await show_history(query.message, user_id, edit=True)

    elif data == "favorites":
        await show_favorites(query.message, user_id, edit=True)

    elif data == "fav_gallery":
        await send_favorites_gallery(query.message, user_id)

    elif data == "fav_list":
        await show_favorites_list(query.message, user_id, edit=False, page=0)

    elif data == "fav_find":
        user_states[user_id] = "waiting_fav_tag"
        await query.edit_message_text(
            "🔎 Введите тег для поиска в избранном:\n\n"
            "Пример: `blonde_hair`",
            parse_mode="Markdown",
        )

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
        remaining = format_remaining_pause(await get_subscription_pause_until(user_id))
        pause_line = (
            f"\nПодписки на паузе: осталось {remaining}\n"
            if remaining
            else ""
        )

        await query.edit_message_text(
            "⚙️ *Настройки бота*\n\n"
            f"Описание картинок: {caption_enabled}\n\n"
            f"{pause_line}"
            "Вы можете настроить:\n"
            "• Какие элементы показывать в описании\n"
            "• Показывать ли запрос поиска\n"
            "• Метку автоматической рассылки\n"
            "• И многое другое",
            reply_markup=get_settings_keyboard(),
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

    elif data == "settings_reset":
        await save_user_settings(user_id, DEFAULT_CAPTION_SETTINGS)
        await query.edit_message_text(
            "✅ Настройки сброшены к значениям по умолчанию!",
            reply_markup=get_settings_keyboard(),
            parse_mode="Markdown",
        )

    elif data == "settings_pause_subscriptions":
        user_states[user_id] = "waiting_pause_subscriptions"
        await query.edit_message_text(
            "⏸ На сколько остановить все активные подписки?\n\n"
            "Можно написать в минутах или коротко: `30`, `2ч`, `1д`.\n"
            "Максимум: 7 дней.",
            parse_mode="Markdown",
        )

    elif data == "settings_resume_subscriptions":
        resumed_count = await resume_all_active_subscriptions(user_id)
        await query.edit_message_text(
            "▶️ Подписки возобновлены.\n\n"
            f"Активных подписок: {resumed_count}.",
            reply_markup=get_settings_keyboard(),
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
            )
        else:
            await query.message.reply_text(
                "❌ Сначала выполните поиск!", reply_markup=get_subscriptions_keyboard()
            )

    elif data == "sub_add_new":
        user_states[user_id] = "waiting_sub_new"
        await query.edit_message_text(
            "🔔 Введите теги для подписки (через пробел):\n\n" "Пример: `anime girl`",
            parse_mode="Markdown",
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
            text, reply_markup=get_subscriptions_keyboard(), parse_mode="Markdown"
        )

    elif data == "sub_manage":
        subscriptions = await get_all_user_subscriptions(user_id)
        if not subscriptions:
            await query.edit_message_text(
                "❌ У вас нет подписок.", reply_markup=get_subscriptions_keyboard()
            )
            return

        keyboard = []
        for sub_query, interval, is_active, empty_count, next_check_at in subscriptions:
            wait_marker = " 🕒" if is_active and empty_count else ""
            toggle_label = "⏸ Пауза" if is_active else "▶️ Запустить"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"{toggle_label}{wait_marker}: {sub_query[:18]}",
                        callback_data=store_callback_payload("sub_toggle", sub_query),
                    ),
                    InlineKeyboardButton(
                        f"⏱ {interval} мин.",
                        callback_data=store_callback_payload("sub_interval", sub_query),
                    ),
                    InlineKeyboardButton(
                        "📷 Фото",
                        callback_data=store_callback_payload("sub_posts", sub_query),
                    ),
                    InlineKeyboardButton(
                        f"❌ {sub_query[:18]}",
                        callback_data=store_callback_payload("sub_remove", sub_query),
                    ),
                ]
            )

        keyboard.append(
            [InlineKeyboardButton("◀️ Назад", callback_data="subscriptions")]
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
                reply_markup=get_subscriptions_keyboard(),
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

        success = await add_subscription(user_id, sub_query, 10)

        if success:
            await query.message.reply_text(
                await build_subscription_added_text(sub_query, 10, user_id),
                parse_mode="Markdown",
            )
        else:
            await query.message.reply_text(
                "❌ Не удалось добавить подписку.", parse_mode="Markdown"
            )

    elif data.startswith("sub_remove_"):
        # Удаление подписки
        sub_query = get_callback_payload("sub_remove", data)
        if not sub_query:
            await query.edit_message_text(
                "❌ Не удалось найти подписку для удаления. Откройте список подписок заново.",
                parse_mode="Markdown",
            )
            return

        success = await remove_subscription(user_id, sub_query)

        if success:
            await query.edit_message_text(
                f"✅ Подписка на `{md_code(sub_query)}` удалена.", parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                "Подписка не найдена.", parse_mode="Markdown"
            )
    elif data.startswith("fav_remove_"):
        payload_parts = data.replace("fav_remove_", "", 1).split("_")
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
        )

    elif data == "fav_all":
        await send_favorites_gallery(query.message, user_id)

    elif data.startswith("fav_page_"):
        index_text = data.replace("fav_page_", "", 1)
        if not index_text.isdigit():
            await query.message.reply_text("❌ Не удалось открыть пост.")
            return

        await edit_favorites_gallery(query, user_id, int(index_text))

    elif data.startswith("fav_del_"):
        parts = data.split("_")
        if len(parts) < 4 or not parts[2].isdigit() or not parts[3].isdigit():
            await query.message.reply_text("❌ Не удалось удалить пост.")
            return

        post_id = int(parts[2])
        index = int(parts[3])
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
                parse_mode="Markdown",
            )
        else:
            await query.message.reply_text(
                f"⭐ Пост `{md_code(post_id)}` добавлен в избранное.",
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
            "➕ Введите тег для добавления в blacklist:\n\n"
            "💡 Можно ввести несколько тегов через пробел"
        )

    elif data == "bl_remove":
        user_states[user_id] = "waiting_bl_remove"
        blacklist = await get_user_blacklist(user_id)
        if blacklist:
            tags_list = ", ".join(f"`{md_code(tag)}`" for tag in sorted(blacklist))
            text = f"➖ Введите тег для удаления:\n\nВаши теги: {tags_list}"
        else:
            text = "➖ Ваш blacklist пуст"
        await query.edit_message_text(text, parse_mode="Markdown")

    elif data == "bl_show":
        blacklist = await get_user_blacklist(user_id)
        if blacklist:
            tags_list = "\n".join(f"• `{md_code(tag)}`" for tag in sorted(blacklist))
            text = f"📋 *Ваш Blacklist:*\n\n{tags_list}"
        else:
            text = "📋 Ваш Blacklist пуст"

        await query.edit_message_text(
            text, reply_markup=get_blacklist_keyboard(), parse_mode="Markdown"
        )

    elif data == "back":
        user_states.pop(user_id, None)
        await query.edit_message_text(
            await build_main_menu_text(user_id),
            reply_markup=get_main_keyboard(),
        )

    elif data == "help":
        await query.edit_message_text(
            "❓ *Помощь*\n\n"
            "*Команды:*\n"
            "`/start` - Запуск бота\n"
            "`/search <теги>` - Быстрый поиск\n"
            "`/random` - Случайная картинка с учётом blacklist\n"
            "`/blacklist` - Управление blacklist\n"
            "`/tags <запрос>` - Поиск тегов\n"
            "`/id <номер>` - Получить пост по ID\n"
            "`/subscriptions` - Управление подписками\n"
            "`/settings` - Настройки бота\n\n"
            "*Настройки описания:*\n"
            "Вы можете выбрать какие элементы показывать:\n"
            "- Запрос поиска\n- ID поста\n- Очки (score)\n"
            "- Рейтинг\n- Теги\n- Метку подписки\n\n"
            "*Подписки:*\n"
            "Подпишитесь на поиск, и бот будет автоматически "
            "присылать новые посты по расписанию!\n\n"
            "*Поиск:*\n"
            "Вводите теги через пробел.\n"
            "Используйте `_` для тегов из нескольких слов.\n"
            "Пример: `blonde_hair blue_eyes 1girl`\n\n"
            "*Blacklist:*\n"
            "Добавляйте теги, которые не хотите видеть.",
            reply_markup=get_main_keyboard(),
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
            "❌ Не удалось найти случайную картинку с учётом blacklist.",
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
        else:
            result = await api.get_random_image(tags, blacklist, excluded_post_ids)
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

        delivered = await send_post_media(message, result, caption, keyboard)
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


async def send_post_media(message, post: dict, caption: str = "", keyboard=None):
    return await send_post_media_with_retries(
        message,
        post,
        caption,
        keyboard,
        retries=MEDIA_SEND_RETRIES,
    )


async def send_post_media_to_chat(bot, chat_id: int, post: dict, caption: str = "", keyboard=None):
    return await send_post_media_to_chat_with_retries(
        bot,
        chat_id,
        post,
        caption,
        keyboard,
        retries=MEDIA_SEND_RETRIES,
    )


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
        )
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
            media=media_from_post(post, caption), reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка обновления галереи подписки: {e}")
        await send_post_media(query.message, post, caption, keyboard)


async def send_favorites_gallery(message, user_id: int, index: int = 0):
    total = await count_favorites(user_id)
    if total <= 0:
        await message.reply_text("❌ Избранное пока пустое.")
        return

    index = max(0, min(index, total - 1))
    post = await get_favorite_by_index(user_id, index)
    if not post:
        await message.reply_text("❌ Избранное пока пустое.")
        return

    settings = await get_user_settings(user_id)
    caption = build_favorites_gallery_caption(post, index, total)
    await send_post_media(
        message,
        post,
        caption,
        get_favorites_gallery_keyboard(
            index,
            total,
            post.get("id", 0),
            should_show_tags_button(settings),
        ),
    )


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
            media=media_from_post(post, caption), reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка обновления галереи избранного: {e}")
        await send_post_media(query.message, post, caption, keyboard)


async def show_history(message, user_id: int, edit: bool = False):
    history = await get_search_history(user_id)
    if not history:
        text = "🕘 История поиска пока пустая.\n\n" + await build_main_menu_text(user_id)
        keyboard = get_main_keyboard()
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
        text = "⭐ Избранное пока пустое.\n\n" + await build_main_menu_text(user_id)
        keyboard = get_main_keyboard()
    else:
        text = f"⭐ *Избранное:* {total}"
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🖼 Галерея", callback_data="fav_gallery")],
                [InlineKeyboardButton("📋 Список", callback_data="fav_list")],
                [InlineKeyboardButton("🔎 Найти по тегу", callback_data="fav_find")],
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
        keyboard_rows.append(
            [InlineKeyboardButton("◀️ Назад", callback_data="favorites")])
        keyboard = InlineKeyboardMarkup(keyboard_rows)

    if edit:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    state = user_states.get(user_id)

    if state == "waiting_search":
        user_states.pop(user_id, None)
        schedule_background_task(context, send_image(update.message, user_id, text))

    elif state == "waiting_pause_subscriptions":
        user_states.pop(user_id, None)
        pause_minutes = parse_pause_minutes(text)
        paused_count = await pause_all_active_subscriptions(user_id, pause_minutes)
        await update.message.reply_text(
            "⏸ Подписки остановлены на "
            f"{format_pause_duration(pause_minutes)}.\n\n"
            f"Затронуто активных подписок: {paused_count}.\n"
            "Новые подписки во время паузы тоже начнут работать только после неё.",
            reply_markup=get_settings_keyboard(),
        )

    elif state == "waiting_fav_tag":
        user_states.pop(user_id, None)
        await show_favorites_list(
            update.message,
            user_id,
            edit=False,
            page=0,
            tag_filter=text,
        )

    elif state == "waiting_sub_new":
        user_states.pop(user_id, None)
        user_states[user_id] = f"waiting_sub_interval_{text}"
        await update.message.reply_text(
            f"🔔 Подписка на: `{md_code(text)}`\n\n"
            "Введите интервал в минутах от 1 до 120 (по умолчанию 10):",
            parse_mode="Markdown",
        )

    elif state and state.startswith("waiting_sub_interval_update_"):
        sub_query = state.replace("waiting_sub_interval_update_", "", 1)
        user_states.pop(user_id, None)
        interval = parse_subscription_interval(text)

        success = await update_subscription_interval(user_id, sub_query, interval)
        if success:
            await update.message.reply_text(
                f"✅ Интервал подписки `{md_code(sub_query)}` изменён на {interval} мин.",
                reply_markup=get_subscriptions_keyboard(),
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "❌ Подписка не найдена.", reply_markup=get_subscriptions_keyboard()
            )

    elif state and state.startswith("waiting_sub_interval_"):
        query = state.replace("waiting_sub_interval_", "", 1)
        user_states.pop(user_id, None)

        interval = parse_subscription_interval(text)

        success = await add_subscription(user_id, query, interval)

        if success:
            await update.message.reply_text(
                await build_subscription_added_text(query, interval, user_id),
                reply_markup=get_subscriptions_keyboard(),
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "❌ Не удалось добавить подписку.",
                reply_markup=get_subscriptions_keyboard(),
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
    await update.message.reply_text(
        "🔔 *Управление подписками*\n\n"
        "Вы можете подписаться на поиск, и бот будет автоматически "
        "присылать новые посты по расписанию!",
        reply_markup=get_subscriptions_keyboard(),
        parse_mode="Markdown",
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
        "⚙️ *Настройки бота*\n\n"
        f"Описание картинок: {caption_enabled}\n\n"
        "Вы можете настроить:\n"
        "• Какие элементы показывать в описании\n"
        "• Показывать ли запрос поиска\n"
        "• Метку автоматической рассылки\n"
        "• И многое другое",
        reply_markup=get_settings_keyboard(),
        parse_mode="Markdown",
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
            )

            await send_post_media(update.message, result, caption, keyboard)
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
    await update.message.reply_text(
        "🚫 *Настройки Blacklist*",
        reply_markup=get_blacklist_keyboard(),
        parse_mode="Markdown",
    )


async def process_one_subscription(app, subscription):
    """Process one due subscription after atomically claiming it."""
    user_id, query, interval, empty_count = subscription
    processing_token = await claim_due_subscription(user_id, query)
    if not processing_token:
        return

    try:
        logger.info("Отправляем подписку пользователю %s: %s", user_id, query)

        blacklist = await get_user_blacklist(user_id)
        settings = await get_user_settings(user_id)
        excluded_post_ids = await get_sent_post_ids(user_id)
        result = await get_subscription_cached_image(
            user_id, query, blacklist, excluded_post_ids
        )

        if result:
            await remember_and_cache_post(result)
            post_id = result.get("id", 0)
            keyboard = get_subscription_image_keyboard(
                post_id,
                query,
                should_show_tags_button(settings),
            )

            caption = ""
            if settings.get("show_caption", True):
                caption = await build_caption(settings, result, query, True)

            delivered = await send_post_media_to_chat(
                app.bot, user_id, result, caption, keyboard
            )
            if delivered:
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
                await release_subscription_claim(user_id, query, processing_token)
            return

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
            await app.bot.send_message(
                chat_id=user_id,
                text=(
                    f"🕒 По подписке `{md_code(query)}` пока нет новых постов.\n\n"
                    f"Я продолжу проверять ее реже: следующая проверка примерно через {backoff_minutes} мин. "
                    "Когда появится новый пост, подписка вернется к обычному интервалу."
                ),
                parse_mode="Markdown",
                reply_markup=get_subscriptions_keyboard(),
            )

    except APITemporaryError as e:
        await release_subscription_claim(user_id, query, processing_token)
        logger.warning(
            "Temporary Rule34 API error for subscription user=%s query=%r: %s",
            user_id,
            query,
            e,
        )
    except Exception:
        await release_subscription_claim(user_id, query, processing_token)
        logger.exception("Subscription processing error for user %s", user_id)


async def get_subscription_cached_image(user_id: int, query: str, blacklist: set, excluded_post_ids: set):
    cached_posts, _ = await get_subscription_cache(user_id, query)
    available_posts = [
        post for post in cached_posts
        if post.get("file_url") and post.get("id") not in excluded_post_ids
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
                if post.get("file_url") and post.get("id") not in excluded_post_ids
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

    async def guarded(subscription):
        async with semaphore:
            await process_one_subscription(app, subscription)

    while True:
        try:
            await release_stale_subscription_claims()
            due_subs = await get_due_subscriptions()
            await asyncio.gather(*(guarded(subscription) for subscription in due_subs))
            await asyncio.sleep(60)

        except Exception:
            logger.exception("Ошибка в фоновой задаче подписок")
            await asyncio.sleep(60)


async def post_init(application):
    global subscription_task

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
        BotCommand("blacklist", "Черный список"),
        BotCommand("subscriptions", "Подписки"),
        BotCommand("history", "История"),
        BotCommand("favorites", "Избранное"),
        BotCommand("settings", "Настройки"),
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


async def post_shutdown(application):
    """Очистка при завершении"""
    # Останавливаем фоновую задачу
    global subscription_task
    if subscription_task and not subscription_task.done():
        subscription_task.cancel()
        try:
            await subscription_task
        except asyncio.CancelledError:
            pass

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
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("random", random_command))
    application.add_handler(CommandHandler("blacklist", blacklist_command))
    application.add_handler(CommandHandler(
        "subscriptions", subscriptions_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("favorites", favorites_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("tags", tags_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler)
    )
    application.add_error_handler(error_handler)

    logger.info("Бот запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
