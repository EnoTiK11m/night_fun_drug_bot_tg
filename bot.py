import logging
import asyncio
import hashlib
import time
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaPhoto,
    InputMediaVideo
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from config import BOT_TOKEN, SEARCH_COOLDOWN_SECONDS, validate_config
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
    update_subscription_interval,
    get_due_subscriptions,
    toggle_subscription,
    get_user_settings,
    update_user_setting,
    save_user_settings,
    get_search_history,
    get_sent_post_ids,
    mark_post_sent,
    add_favorite,
    remove_favorite,
    get_favorites,
    add_subscription_post,
    get_subscription_posts,
    remove_subscription_post
)
from api_handler import api

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния пользователей
user_states = {}
# Глобальная задача для подписок
subscription_task = None
callback_payloads = {}
user_last_search_at = {}
CALLBACK_TTL_SECONDS = 24 * 60 * 60
MAX_CAPTION_LENGTH = 1024
SUBSCRIPTION_MIN_INTERVAL = 1
SUBSCRIPTION_MAX_INTERVAL = 120
SUBSCRIPTION_DEFAULT_INTERVAL = 10


def store_callback_payload(action: str, payload: str) -> str:
    """Store large callback payloads behind compact Telegram callback_data."""
    cleanup_callback_payloads()
    token = hashlib.blake2s(
        f"{action}:{payload}".encode("utf-8"),
        digest_size=8
    ).hexdigest()
    callback_payloads[(action, token)] = (payload, time.monotonic())
    return f"{action}_{token}"


def get_callback_payload(action: str, data: str) -> str:
    cleanup_callback_payloads()
    token = data.replace(f"{action}_", "", 1)
    stored = callback_payloads.get((action, token))
    return stored[0] if stored else ""


def get_callback_payload_by_token(action: str, token: str) -> str:
    cleanup_callback_payloads()
    stored = callback_payloads.get((action, token))
    return stored[0] if stored else ""


def cleanup_callback_payloads():
    now = time.monotonic()
    expired = [
        key for key, (_, created_at) in callback_payloads.items()
        if now - created_at > CALLBACK_TTL_SECONDS
    ]
    for key in expired:
        callback_payloads.pop(key, None)


def md_code(value) -> str:
    return str(value).replace("\\", "").replace("`", "'")


def clamp_caption(caption: str) -> str:
    if len(caption) <= MAX_CAPTION_LENGTH:
        return caption
    return caption[:MAX_CAPTION_LENGTH - 3].rstrip() + "..."


def is_rate_limited(user_id: int) -> bool:
    if SEARCH_COOLDOWN_SECONDS <= 0:
        return False

    now = time.monotonic()
    last_at = user_last_search_at.get(user_id, 0)
    if now - last_at < SEARCH_COOLDOWN_SECONDS:
        return True

    user_last_search_at[user_id] = now
    return False


def parse_subscription_interval(value: str) -> int:
    if not value.isdigit():
        return SUBSCRIPTION_DEFAULT_INTERVAL

    interval = int(value)
    return max(
        SUBSCRIPTION_MIN_INTERVAL,
        min(interval, SUBSCRIPTION_MAX_INTERVAL)
    )


def media_from_post(post: dict, caption: str = ""):
    file_url = post.get("file_url", "")
    media_caption = caption if caption else None
    if file_url.lower().endswith((".mp4", ".webm")):
        return InputMediaVideo(file_url, caption=media_caption, parse_mode="Markdown")
    if file_url.lower().endswith(".gif"):
        return InputMediaAnimation(file_url, caption=media_caption, parse_mode="Markdown")
    return InputMediaPhoto(file_url, caption=media_caption, parse_mode="Markdown")


def build_subscription_gallery_caption(sub_query: str, post: dict, index: int, total: int) -> str:
    tags = post.get("tags", "")
    if len(tags) > 120:
        tags = tags[:120] + "..."

    return clamp_caption(
        f"🔔 Подписка: `{md_code(sub_query)}`\n"
        f"Фото {index + 1}/{total}\n"
        f"ID: `{md_code(post.get('id', 0))}`\n"
        f"Rating: {md_code(post.get('rating', ''))} | Score: {post.get('score', 0)}\n"
        f"Tags: `{md_code(tags)}`"
    )


def build_favorites_gallery_caption(post: dict, index: int, total: int) -> str:
    tags = post.get("tags", "")
    if len(tags) > 120:
        tags = tags[:120] + "..."

    return clamp_caption(
        f"⭐ Избранное\n"
        f"Фото {index + 1}/{total}\n"
        f"ID: `{md_code(post.get('id', 0))}`\n"
        f"Rating: {md_code(post.get('rating', ''))} | Score: {post.get('score', 0)}\n"
        f"Tags: `{md_code(tags)}`"
    )


def get_subscription_gallery_keyboard(token: str, index: int, total: int, post_id: int) -> InlineKeyboardMarkup:
    prev_index = (index - 1) % total
    next_index = (index + 1) % total
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("◀️", callback_data=f"sub_page_{token}_{prev_index}"),
            InlineKeyboardButton(f"{index + 1}/{total}", callback_data="noop"),
            InlineKeyboardButton("▶️", callback_data=f"sub_page_{token}_{next_index}")
        ],
        [
            InlineKeyboardButton("❌ Удалить", callback_data=f"sub_post_del_{token}_{post_id}_{index}")
        ],
        [
            InlineKeyboardButton("📋 Список", callback_data=f"sub_list_posts_{token}")
        ]
    ])


def get_favorites_gallery_keyboard(index: int, total: int, post_id: int) -> InlineKeyboardMarkup:
    prev_index = (index - 1) % total
    next_index = (index + 1) % total
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("◀️", callback_data=f"fav_page_{prev_index}"),
            InlineKeyboardButton(f"{index + 1}/{total}", callback_data="noop"),
            InlineKeyboardButton("▶️", callback_data=f"fav_page_{next_index}")
        ],
        [
            InlineKeyboardButton("❌ Удалить", callback_data=f"fav_del_{post_id}_{index}")
        ],
        [
            InlineKeyboardButton("📋 Список", callback_data="fav_list")
        ]
    ])


def get_main_keyboard() -> InlineKeyboardMarkup:
    """Главная клавиатура"""
    keyboard = [
        [InlineKeyboardButton("🔍 Поиск", callback_data="search")],
        [InlineKeyboardButton("🔄 Ещё", callback_data="more")],
        [
            InlineKeyboardButton("🚫 Blacklist", callback_data="blacklist"),
            InlineKeyboardButton("📋 Подписки", callback_data="subscriptions")
        ],
        [
            InlineKeyboardButton("🕘 История", callback_data="history"),
            InlineKeyboardButton("⭐ Избранное", callback_data="favorites")
        ],
        [
            InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
            InlineKeyboardButton("❓ Помощь", callback_data="help")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_blacklist_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура blacklist"""
    keyboard = [
        [InlineKeyboardButton("➕ Добавить тег", callback_data="bl_add")],
        [InlineKeyboardButton("➖ Удалить тег", callback_data="bl_remove")],
        [InlineKeyboardButton("📋 Показать список", callback_data="bl_show")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_subscriptions_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура подписок"""
    keyboard = [
        [InlineKeyboardButton("➕ Подписаться на текущий поиск",
                              callback_data="sub_add_current")],
        [InlineKeyboardButton("➕ Подписаться на новый поиск",
                              callback_data="sub_add_new")],
        [InlineKeyboardButton("📋 Мои подписки", callback_data="sub_list")],
        [InlineKeyboardButton("⚙️ Управление подписками",
                              callback_data="sub_manage")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_settings_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура настроек"""
    keyboard = [
        [InlineKeyboardButton("📝 Настройки описания",
                              callback_data="settings_caption")],
        [InlineKeyboardButton("🔄 Сброс настроек",
                              callback_data="settings_reset")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back")]
    ]
    return InlineKeyboardMarkup(keyboard)


async def get_caption_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Асинхронно получить клавиатуру настроек описания"""
    settings = await get_user_settings(user_id)

    # Создаем кнопки с текущим состоянием
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Показывать описание" if settings.get(
                    'show_caption', True) else "❌ Скрыть описание",
                callback_data="toggle_show_caption"
            )
        ],
        [
            InlineKeyboardButton(
                "✅ Запрос поиска" if settings.get(
                    'show_search_query', True) else "❌ Запрос поиска",
                callback_data="toggle_show_search_query"
            )
        ],
        [
            InlineKeyboardButton(
                "✅ Метка подписки" if settings.get(
                    'show_subscription_label', True) else "❌ Метка подписки",
                callback_data="toggle_show_subscription_label"
            )
        ],
        [
            InlineKeyboardButton(
                "✅ ID поста" if settings.get(
                    'show_id', True) else "❌ ID поста",
                callback_data="toggle_show_id"
            ),
            InlineKeyboardButton(
                "✅ Рейтинг" if settings.get(
                    'show_rating', True) else "❌ Рейтинг",
                callback_data="toggle_show_rating"
            )
        ],
        [
            InlineKeyboardButton(
                "✅ Очки" if settings.get('show_score', True) else "❌ Очки",
                callback_data="toggle_show_score"
            ),
            InlineKeyboardButton(
                "✅ Теги" if settings.get('show_tags', True) else "❌ Теги",
                callback_data="toggle_show_tags"
            )
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_image_keyboard(post_id: int, query: str = "") -> InlineKeyboardMarkup:
    """Клавиатура под изображением"""
    keyboard = [
        [
            InlineKeyboardButton("🔄 Ещё", callback_data="more"),
            InlineKeyboardButton("🔍 Новый поиск", callback_data="search")
        ],
        [
            InlineKeyboardButton("⭐ В избранное", callback_data=f"fav_{post_id}")
        ],
        [
            InlineKeyboardButton(
                "🌐 Открыть на сайте",
                url=f"https://rule34.xxx/index.php?page=post&s=view&id={post_id}"
            )
        ]
    ]

    # Добавляем кнопку подписки если есть запрос
    if query:
        keyboard.insert(0, [
            InlineKeyboardButton(
                "🔔 Подписаться",
                callback_data=store_callback_payload("subscribe", query)
            )
        ])

    return InlineKeyboardMarkup(keyboard)


def get_subscription_image_keyboard(post_id: int, sub_query: str = "") -> InlineKeyboardMarkup:
    """Клавиатура под постом из подписки."""
    favorite_callback = (
        store_callback_payload("sub_fav", f"{post_id}\n{sub_query}")
        if sub_query
        else f"fav_{post_id}"
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐ В избранное", callback_data=favorite_callback)
        ],
        [
            InlineKeyboardButton(
                "🌐 Открыть на сайте",
                url=f"https://rule34.xxx/index.php?page=post&s=view&id={post_id}"
            )
        ]
    ])


async def build_caption(settings: dict, result: dict, query: str = "", is_subscription: bool = False) -> str:
    """Построить описание на основе настроек"""
    caption_parts = []

    # Заголовок для подписки
    if is_subscription and settings.get('show_subscription_label', True):
        caption_parts.append("🔔 *Автоматическая рассылка*")

    # Запрос поиска
    if query and settings.get('show_search_query', True):
        caption_parts.append(f"Запрос: `{md_code(query)}`")

    # Основная информация о посте
    if settings.get('show_id', True):
        caption_parts.append(f"🆔 ID: `{md_code(result.get('id', 0))}`")

    if settings.get('show_score', True):
        caption_parts.append(f"📊 Score: {result.get('score', 0)}")

    if settings.get('show_rating', True):
        caption_parts.append(f"🏷 Rating: {md_code(result.get('rating', 'unknown'))}")

    # Теги
    if settings.get('show_tags', True):
        post_tags = result.get("tags", "")
        if len(post_tags) > 150:
            post_tags = post_tags[:150] + "..."
        caption_parts.append(f"🔖 Tags: `{md_code(post_tags)}`")

    # Если все описание выключено, возвращаем пустую строку
    if not caption_parts:
        return ""

    # Собираем все части
    if len(caption_parts) == 1:
        return clamp_caption(caption_parts[0])
    elif len(caption_parts) == 2:
        return clamp_caption(f"{caption_parts[0]}\n{caption_parts[1]}")
    else:
        # Первый элемент как заголовок, остальные как список
        return clamp_caption(f"{caption_parts[0]}\n" + "\n".join(caption_parts[1:]))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    await update.message.reply_text(
        "👋 Привет! Я бот для поиска изображений на rule34.\n\n"
        "🔍 *Основные функции:*\n"
        "• *Поиск* - поиск по тегам\n"
        "• *Подписки* - автоматическая отправка каждые 10 минут\n"
        "• *Blacklist* - фильтрация нежелательных тегов\n"
        "• *Настройки* - управление описанием картинок\n\n"
        "⚙️ *Настройки описания:*\n"
        "Вы можете выбрать какие элементы показывать в описании:\n"
        "- Запрос поиска\n- ID поста\n- Очки (score)\n- Рейтинг\n- Теги\n- Метку подписки\n\n"
        "⚠️ Бот предназначен для пользователей",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий кнопок"""
    query = update.callback_query
    await query.answer()

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
            parse_mode="Markdown"
        )

    elif data == "more":
        saved = await get_user_query(user_id)
        if saved and saved[0]:
            await send_image(query.message, user_id, saved[0], edit=False, is_more=True)
        else:
            await query.message.reply_text(
                "❌ Сначала выполните поиск!",
                reply_markup=get_main_keyboard()
            )

    elif data == "blacklist":
        await query.edit_message_text(
            "🚫 *Настройки Blacklist*\n\n"
            "Теги в blacklist будут исключены из результатов поиска.",
            reply_markup=get_blacklist_keyboard(),
            parse_mode="Markdown"
        )

    elif data == "subscriptions":
        await query.edit_message_text(
            "🔔 *Управление подписками*\n\n"
            "Вы можете подписаться на поиск, и бот будет автоматически "
            "присылать новые посты по расписанию!",
            reply_markup=get_subscriptions_keyboard(),
            parse_mode="Markdown"
        )

    elif data == "history":
        await show_history(query.message, user_id, edit=True)

    elif data == "favorites":
        await show_favorites(query.message, user_id, edit=True)

    elif data == "fav_list":
        await show_favorites(query.message, user_id, edit=False)

    elif data == "noop":
        return

    elif data == "settings":
        settings = await get_user_settings(user_id)
        caption_enabled = "✅ Включено" if settings.get(
            'show_caption', True) else "❌ Выключено"

        await query.edit_message_text(
            "⚙️ *Настройки бота*\n\n"
            f"Описание картинок: {caption_enabled}\n\n"
            "Вы можете настроить:\n"
            "• Какие элементы показывать в описании\n"
            "• Показывать ли запрос поиска\n"
            "• Метку автоматической рассылки\n"
            "• И многое другое",
            reply_markup=get_settings_keyboard(),
            parse_mode="Markdown"
        )

    elif data == "settings_caption":
        settings = await get_user_settings(user_id)

        # Формируем текст
        text = "📝 *Настройки описания картинок*\n\n"

        if settings.get('show_caption', True):
            text += "✅ Описание *включено*\n\n"

            # Собираем включенные и выключенные элементы
            elements = [
                ("show_search_query", "Запрос поиска"),
                ("show_subscription_label", "Метка подписки"),
                ("show_id", "ID поста"),
                ("show_score", "Очки (score)"),
                ("show_rating", "Рейтинг"),
                ("show_tags", "Теги")
            ]

            enabled = []
            disabled = []

            for setting_key, element_name in elements:
                if settings.get(setting_key, True):
                    enabled.append(f"✅ {element_name}")
                else:
                    disabled.append(f"❌ {element_name}")

            if enabled:
                text += "*Включено:*\n" + "\n".join(enabled) + "\n\n"

            if disabled:
                text += "*Выключено:*\n" + "\n".join(disabled)
        else:
            text += "❌ Описание *полностью отключено*\n\nНажмите '✅ Показывать описание' чтобы включить"

        # Получаем клавиатуру настроек
        keyboard = await get_caption_settings_keyboard(user_id)

        try:
            await query.edit_message_text(
                text=text,
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Error in settings_caption: {e}")
            await query.message.reply_text(
                text=text,
                reply_markup=keyboard,
                parse_mode="Markdown"
            )

    elif data == "settings_reset":
        # Сбрасываем настройки к значениям по умолчанию
        default_settings = {
            'show_caption': True,
            'show_search_query': True,
            'show_subscription_label': True,
            'show_id': True,
            'show_score': True,
            'show_rating': True,
            'show_tags': True,
        }
        await save_user_settings(user_id, default_settings)
        await query.edit_message_text(
            "✅ Настройки сброшены к значениям по умолчанию!",
            reply_markup=get_settings_keyboard(),
            parse_mode="Markdown"
        )

    elif data.startswith("toggle_"):
        setting_name = data.replace("toggle_", "")

        # Получаем текущие настройки
        settings = await get_user_settings(user_id)
        current_value = settings.get(setting_name, True)

        # Обновляем настройку
        await update_user_setting(user_id, setting_name, not current_value)

        # Если отключаем описание полностью, выключаем все остальные настройки
        if setting_name == "show_caption" and not current_value:
            await update_user_setting(user_id, "show_search_query", False)
            await update_user_setting(user_id, "show_subscription_label", False)
            await update_user_setting(user_id, "show_id", False)
            await update_user_setting(user_id, "show_score", False)
            await update_user_setting(user_id, "show_rating", False)
            await update_user_setting(user_id, "show_tags", False)
        # Если включаем описание, включаем основные настройки
        elif setting_name == "show_caption" and current_value:
            await update_user_setting(user_id, "show_id", True)
            await update_user_setting(user_id, "show_tags", True)

        # Обновляем сообщение
        settings = await get_user_settings(user_id)

        # Формируем текст
        text = "📝 *Настройки описания картинок*\n\n"

        if settings.get('show_caption', True):
            text += "✅ Описание *включено*\n\n"

            # Собираем включенные и выключенные элементы
            elements = [
                ("show_search_query", "Запрос поиска"),
                ("show_subscription_label", "Метка подписки"),
                ("show_id", "ID поста"),
                ("show_score", "Очки (score)"),
                ("show_rating", "Рейтинг"),
                ("show_tags", "Теги")
            ]

            enabled = []
            disabled = []

            for setting_key, element_name in elements:
                if settings.get(setting_key, True):
                    enabled.append(f"✅ {element_name}")
                else:
                    disabled.append(f"❌ {element_name}")

            if enabled:
                text += "*Включено:*\n" + "\n".join(enabled) + "\n\n"

            if disabled:
                text += "*Выключено:*\n" + "\n".join(disabled)
        else:
            text += "❌ Описание *полностью отключено*\n\nНажмите '✅ Показывать описание' чтобы включить"

        # Получаем обновленную клавиатуру
        keyboard = await get_caption_settings_keyboard(user_id)

        try:
            await query.edit_message_text(
                text=text,
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Error updating toggle: {e}")

    elif data == "sub_add_current":
        saved = await get_user_query(user_id)
        if saved and saved[0]:
            user_states[user_id] = f"waiting_sub_interval_{saved[0]}"
            await query.edit_message_text(
                f"🔔 Подписка на: `{saved[0]}`\n\n"
                "Введите интервал в минутах от 1 до 120 (по умолчанию 10):",
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text(
                "❌ Сначала выполните поиск!",
                reply_markup=get_subscriptions_keyboard()
            )

    elif data == "sub_add_new":
        user_states[user_id] = "waiting_sub_new"
        await query.edit_message_text(
            "🔔 Введите теги для подписки (через пробел):\n\n"
            "Пример: `anime girl`",
            parse_mode="Markdown"
        )

    elif data == "sub_list":
        subscriptions = await get_all_user_subscriptions(user_id)
        if subscriptions:
            subs_list = []
            for sub_query, interval, is_active in subscriptions:
                status = "✅ активна" if is_active else "⏸ остановлена"
                subs_list.append(
                    f"• `{md_code(sub_query)}` - каждые {interval} мин., {status}"
                )

            text = "📋 *Ваши подписки:*\n\n" + "\n".join(subs_list)
        else:
            text = "📋 У вас пока нет подписок."

        await query.edit_message_text(
            text,
            reply_markup=get_subscriptions_keyboard(),
            parse_mode="Markdown"
        )

    elif data == "sub_manage":
        subscriptions = await get_all_user_subscriptions(user_id)
        if not subscriptions:
            await query.edit_message_text(
                "❌ У вас нет подписок.",
                reply_markup=get_subscriptions_keyboard()
            )
            return

        # Создаем клавиатуру для управления
        keyboard = []
        for sub_query, interval, is_active in subscriptions:
            toggle_label = "⏸ Пауза" if is_active else "▶️ Запустить"
            keyboard.append([
                InlineKeyboardButton(
                    f"{toggle_label}: {sub_query[:18]}",
                    callback_data=store_callback_payload("sub_toggle", sub_query)
                ),
                InlineKeyboardButton(
                    f"⏱ {interval} мин.",
                    callback_data=store_callback_payload("sub_interval", sub_query)
                ),
                InlineKeyboardButton(
                    "📷 Фото",
                    callback_data=store_callback_payload("sub_posts", sub_query)
                ),
                InlineKeyboardButton(
                    f"❌ {sub_query[:18]}",
                    callback_data=store_callback_payload("sub_remove", sub_query)
                )
            ])

        keyboard.append([InlineKeyboardButton(
            "◀️ Назад", callback_data="subscriptions")])

        await query.edit_message_text(
            "⚙️ *Управление подписками*\n\n"
            "Нажмите на подписку чтобы удалить:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    elif data.startswith("sub_interval_"):
        sub_query = get_callback_payload("sub_interval", data)
        if not sub_query:
            await query.edit_message_text(
                "❌ Не удалось найти подписку. Откройте список подписок заново.",
                parse_mode="Markdown"
            )
            return

        user_states[user_id] = f"waiting_sub_interval_update_{sub_query}"
        await query.edit_message_text(
            f"⏱ Новый интервал для `{md_code(sub_query)}`\n\n"
            "Введите число минут от 1 до 120:",
            parse_mode="Markdown"
        )

    elif data.startswith("sub_posts_"):
        token = data.replace("sub_posts_", "", 1)
        sub_query = get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.edit_message_text(
                "❌ Не удалось найти подписку. Откройте список подписок заново.",
                parse_mode="Markdown"
            )
            return

        await show_subscription_posts_menu(query.message, user_id, sub_query, token)

    elif data.startswith("sub_list_posts_"):
        token = data.replace("sub_list_posts_", "", 1)
        sub_query = get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.message.reply_text("❌ Не удалось найти подписку. Откройте список заново.")
            return

        await show_subscription_posts_menu(query.message, user_id, sub_query, token, edit=False)

    elif data.startswith("sub_one_"):
        parts = data.split("_")
        if len(parts) < 4 or not parts[-1].isdigit():
            await query.message.reply_text("❌ Не удалось открыть пост.")
            return

        token = parts[2]
        index = int(parts[3])
        sub_query = get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.message.reply_text("❌ Не удалось найти подписку. Откройте список заново.")
            return

        await send_subscription_post_by_index(query.message, user_id, sub_query, index)

    elif data.startswith("sub_all_"):
        token = data.replace("sub_all_", "", 1)
        sub_query = get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.message.reply_text("❌ Не удалось найти подписку. Откройте список заново.")
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
            await query.message.reply_text("❌ Не удалось найти подписку. Откройте список заново.")
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
            await query.message.reply_text("❌ Не удалось найти подписку. Откройте список заново.")
            return

        await remove_subscription_post(user_id, sub_query, post_id)
        await remove_favorite(user_id, post_id)
        await edit_subscription_gallery(query, user_id, sub_query, token, index)

    elif data.startswith("sub_toggle_"):
        sub_query = get_callback_payload("sub_toggle", data)
        if not sub_query:
            await query.edit_message_text(
                "❌ Не удалось найти подписку. Откройте список подписок заново.",
                parse_mode="Markdown"
            )
            return

        new_state = await toggle_subscription(user_id, sub_query)
        if new_state is None:
            await query.edit_message_text(
                "❌ Подписка не найдена.",
                parse_mode="Markdown"
            )
        else:
            state_text = "запущена" if new_state else "остановлена"
            await query.edit_message_text(
                f"✅ Подписка `{md_code(sub_query)}` {state_text}.",
                reply_markup=get_subscriptions_keyboard(),
                parse_mode="Markdown"
            )

    elif data.startswith("subscribe_"):
        # Подписка из клавиатуры под изображением
        sub_query = get_callback_payload("subscribe", data)
        if not sub_query:
            await query.edit_message_text(
                "❌ Не удалось найти запрос для подписки. Попробуйте выполнить поиск заново.",
                parse_mode="Markdown"
            )
            return

        success = await add_subscription(user_id, sub_query, 10)

        if success:
            await query.edit_message_text(
                f"✅ Подписка на `{md_code(sub_query)}` активирована!\n\n"
                "Теперь вы будете получать новые посты каждые 10 минут.",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                "❌ Не удалось добавить подписку.",
                parse_mode="Markdown"
            )

    elif data.startswith("sub_remove_"):
        # Удаление подписки
        sub_query = get_callback_payload("sub_remove", data)
        if not sub_query:
            await query.edit_message_text(
                "❌ Не удалось найти подписку для удаления. Откройте список подписок заново.",
                parse_mode="Markdown"
            )
            return

        success = await remove_subscription(user_id, sub_query)

        if success:
            await query.edit_message_text(
                f"✅ Подписка на `{md_code(sub_query)}` удалена.",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                "❌ Подписка не найдена.",
                parse_mode="Markdown"
            )

    elif data.startswith("fav_remove_"):
        post_id_text = data.replace("fav_remove_", "", 1)
        if not post_id_text.isdigit():
            await query.message.reply_text("❌ Не удалось определить пост.")
            return

        removed = await remove_favorite(user_id, int(post_id_text))
        await query.message.reply_text(
            "✅ Удалено из избранного." if removed else "❌ Пост не найден в избранном."
        )

    elif data.startswith("fav_open_"):
        post_id_text = data.replace("fav_open_", "", 1)
        if not post_id_text.isdigit():
            await query.message.reply_text("❌ Не удалось определить пост.")
            return

        favorites = await get_favorites(user_id, limit=1000)
        post = next(
            (favorite for favorite in favorites if favorite["id"] == int(post_id_text)),
            None
        )
        if not post:
            await query.message.reply_text("❌ Пост не найден в избранном.")
            return

        caption = build_favorites_gallery_caption(post, favorites.index(post), len(favorites))
        await send_post_media(query.message, post, caption, get_image_keyboard(post["id"]))

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
        payload = get_callback_payload("sub_fav", data)
        if not payload or "\n" not in payload:
            await query.message.reply_text("❌ Не удалось определить пост подписки.")
            return

        post_id_text, sub_query = payload.split("\n", 1)
        if not post_id_text.isdigit():
            await query.message.reply_text("❌ Не удалось определить пост подписки.")
            return

        post_id = int(post_id_text)
        post = await api.get_post_by_id(post_id)
        if not post:
            await query.message.reply_text(
                f"❌ Пост с ID `{md_code(post_id)}` не найден.",
                parse_mode="Markdown"
            )
            return

        await add_favorite(user_id, post)
        await add_subscription_post(user_id, sub_query, post)
        await query.message.reply_text(
            f"⭐ Пост `{md_code(post_id)}` добавлен в избранное подписки `{md_code(sub_query)}`.",
            parse_mode="Markdown"
        )

    elif data.startswith("fav_"):
        post_id_text = data.replace("fav_", "", 1)
        if not post_id_text.isdigit():
            await query.message.reply_text("❌ Не удалось определить пост.")
            return

        post_id = int(post_id_text)
        post = await api.get_post_by_id(post_id)
        if not post:
            await query.message.reply_text(
                f"❌ Пост с ID `{md_code(post_id)}` не найден.",
                parse_mode="Markdown"
            )
            return

        added = await add_favorite(user_id, post)
        if added:
            await query.message.reply_text(
                f"⭐ Пост `{md_code(post_id)}` добавлен в избранное.",
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text(
                f"⭐ Пост `{md_code(post_id)}` уже есть в избранном.",
                parse_mode="Markdown"
            )

    elif data.startswith("hist_"):
        history_query = get_callback_payload("hist", data)
        if not history_query:
            await query.message.reply_text("❌ Не удалось найти запрос. Откройте историю заново.")
            return
        await send_image(query.message, user_id, history_query)

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
            tags_list = ", ".join(f"`{tag}`" for tag in sorted(blacklist))
            text = f"➖ Введите тег для удаления:\n\nВаши теги: {tags_list}"
        else:
            text = "➖ Ваш blacklist пуст"
        await query.edit_message_text(text, parse_mode="Markdown")

    elif data == "bl_show":
        blacklist = await get_user_blacklist(user_id)
        if blacklist:
            tags_list = "\n".join(f"• `{tag}`" for tag in sorted(blacklist))
            text = f"📋 *Ваш Blacklist:*\n\n{tags_list}"
        else:
            text = "📋 Ваш Blacklist пуст"

        await query.edit_message_text(
            text,
            reply_markup=get_blacklist_keyboard(),
            parse_mode="Markdown"
        )

    elif data == "back":
        user_states.pop(user_id, None)
        await query.edit_message_text(
            "Главное меню:",
            reply_markup=get_main_keyboard()
        )

    elif data == "help":
        await query.edit_message_text(
            "❓ *Помощь*\n\n"
            "*Команды:*\n"
            "`/start` - Запуск бота\n"
            "`/search <теги>` - Быстрый поиск\n"
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
            parse_mode="Markdown"
        )


async def send_image(message, user_id: int, tags: str, edit: bool = False, is_more: bool = False, is_subscription: bool = False):
    """Отправка изображения"""
    blacklist = await get_user_blacklist(user_id)
    settings = await get_user_settings(user_id)

    if not is_subscription and is_rate_limited(user_id):
        await message.reply_text(
            f"⏳ Подождите {SEARCH_COOLDOWN_SECONDS} сек. перед следующим поиском.",
            reply_markup=get_main_keyboard()
        )
        return False

    if not is_subscription:  # Не показываем статус для подписок
        status_msg = await message.reply_text("🔍 Ищу...")

    excluded_post_ids = await get_sent_post_ids(user_id)

    # Если это кнопка "ещё", используем улучшенную логику
    if is_more:
        result = await api.get_next_image(
            user_id,
            tags,
            blacklist,
            excluded_post_ids
        )
    else:
        result = await api.get_random_image(tags, blacklist, excluded_post_ids)
        # Сохраняем историю поиска для кнопки "ещё"
        if result:
            await api.save_search_state(
                user_id,
                tags,
                blacklist,
                result.get("id")
            )

    if not is_subscription:
        await status_msg.delete()

    if result:
        await save_user_query(user_id, tags)

        file_url = result.get("file_url", "")
        post_id = result.get("id", 0)
        if post_id:
            await mark_post_sent(user_id, int(post_id))

        # Строим описание на основе настроек
        caption = ""
        if settings.get('show_caption', True):
            caption = await build_caption(settings, result, tags, is_subscription)

        # Для подписок не добавляем кнопку подписки (чтобы избежать рекурсии)
        if is_subscription:
            keyboard = get_subscription_image_keyboard(post_id, tags)
        else:
            keyboard = get_image_keyboard(post_id, tags)

        try:
            if file_url.lower().endswith(('.mp4', '.webm')):
                await message.reply_video(
                    file_url,
                    caption=caption if caption else None,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            elif file_url.lower().endswith('.gif'):
                await message.reply_animation(
                    file_url,
                    caption=caption if caption else None,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            else:
                await message.reply_photo(
                    file_url,
                    caption=caption if caption else None,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
        except Exception as e:
            logger.error(f"Send error: {e}")
            if caption:
                await message.reply_text(
                    f"🖼 [Открыть изображение]({file_url})\n\n{caption}",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            else:
                await message.reply_text(
                    f"🖼 [Открыть изображение]({file_url})",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )

        return True
    else:
        if not is_subscription:
            if is_more:
                await message.reply_text(
                    "❌ Больше не найдено постов по этому запросу.\n\n"
                    "Попробуйте:\n"
                    "• Другие теги\n"
                    "• Новый поиск",
                    reply_markup=get_main_keyboard(),
                    parse_mode="Markdown"
                )
            else:
                await message.reply_text(
                    "❌ Ничего не найдено по запросу.\n\n"
                    "Попробуйте:\n"
                    "• Другие теги\n"
                    "• Проверить правильность написания\n"
                    "• Использовать `/tags` для поиска тегов",
                    reply_markup=get_main_keyboard(),
                    parse_mode="Markdown"
                )
        return False


async def send_post_media(message, post: dict, caption: str = "", keyboard=None):
    file_url = post.get("file_url", "")
    reply_markup = keyboard or get_subscription_image_keyboard(post.get("id", 0))
    if file_url.lower().endswith(('.mp4', '.webm')):
        await message.reply_video(
            file_url,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    elif file_url.lower().endswith('.gif'):
        await message.reply_animation(
            file_url,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    else:
        await message.reply_photo(
            file_url,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )


async def show_subscription_posts_menu(message, user_id: int, sub_query: str, token: str, edit: bool = True):
    posts = await get_subscription_posts(user_id, sub_query)
    if not posts:
        text = (
            f"⭐ Для подписки `{md_code(sub_query)}` пока нет избранных постов.\n\n"
            "Нажмите `⭐ В избранное` под постом из этой подписки, и он появится здесь."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Назад", callback_data="sub_manage")]
        ])
    else:
        text = f"⭐ *Избранное подписки* `{md_code(sub_query)}`\n\nВыберите номер или откройте просмотр всех."
        rows = []
        for row_start in range(0, min(len(posts), 20), 5):
            row = []
            for index in range(row_start, min(row_start + 5, len(posts), 20)):
                row.append(InlineKeyboardButton(
                    str(index + 1),
                    callback_data=f"sub_one_{token}_{index}"
                ))
            rows.append(row)

        rows.append([InlineKeyboardButton("▶️ Смотреть все", callback_data=f"sub_all_{token}")])
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data="sub_manage")])
        keyboard = InlineKeyboardMarkup(rows)

    if edit:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def send_subscription_post_by_index(message, user_id: int, sub_query: str, index: int):
    posts = await get_subscription_posts(user_id, sub_query)
    if index < 0 or index >= len(posts):
        await message.reply_text("❌ Пост не найден. Откройте список заново.")
        return

    post = posts[index]
    caption = build_subscription_gallery_caption(sub_query, post, index, len(posts))
    await send_post_media(message, post, caption, get_subscription_image_keyboard(post.get("id", 0)))


async def send_subscription_gallery(message, user_id: int, sub_query: str, token: str, index: int = 0):
    posts = await get_subscription_posts(user_id, sub_query)
    if not posts:
        await message.reply_text("❌ Для этой подписки пока нет избранных постов.")
        return

    index = max(0, min(index, len(posts) - 1))
    post = posts[index]
    caption = build_subscription_gallery_caption(sub_query, post, index, len(posts))
    await send_post_media(
        message,
        post,
        caption,
        get_subscription_gallery_keyboard(token, index, len(posts), post.get("id", 0))
    )


async def edit_subscription_gallery(query, user_id: int, sub_query: str, token: str, index: int):
    posts = await get_subscription_posts(user_id, sub_query)
    if not posts:
        await query.message.reply_text("❌ Для этой подписки больше нет избранных постов.")
        return

    index = max(0, min(index, len(posts) - 1))
    post = posts[index]
    caption = build_subscription_gallery_caption(sub_query, post, index, len(posts))
    keyboard = get_subscription_gallery_keyboard(token, index, len(posts), post.get("id", 0))
    try:
        await query.edit_message_media(
            media=media_from_post(post, caption),
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка обновления галереи подписки: {e}")
        await query.message.reply_text("❌ Не удалось обновить пост. Откройте просмотр заново.")


async def send_favorites_gallery(message, user_id: int, index: int = 0):
    favorites = await get_favorites(user_id, limit=1000)
    if not favorites:
        await message.reply_text("❌ Избранное пока пустое.")
        return

    index = max(0, min(index, len(favorites) - 1))
    post = favorites[index]
    caption = build_favorites_gallery_caption(post, index, len(favorites))
    await send_post_media(
        message,
        post,
        caption,
        get_favorites_gallery_keyboard(index, len(favorites), post.get("id", 0))
    )


async def edit_favorites_gallery(query, user_id: int, index: int):
    favorites = await get_favorites(user_id, limit=1000)
    if not favorites:
        await query.message.reply_text("❌ В избранном больше нет постов.")
        return

    index = max(0, min(index, len(favorites) - 1))
    post = favorites[index]
    caption = build_favorites_gallery_caption(post, index, len(favorites))
    keyboard = get_favorites_gallery_keyboard(index, len(favorites), post.get("id", 0))
    try:
        await query.edit_message_media(
            media=media_from_post(post, caption),
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка обновления галереи избранного: {e}")
        await query.message.reply_text("❌ Не удалось обновить пост. Откройте избранное заново.")


async def show_history(message, user_id: int, edit: bool = False):
    history = await get_search_history(user_id)
    if not history:
        text = "🕘 История поиска пока пустая."
        keyboard = get_main_keyboard()
    else:
        text = "🕘 *Последние запросы:*\n\n" + "\n".join(
            f"• `{md_code(item)}`" for item in history
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                item[:40],
                callback_data=store_callback_payload("hist", item)
            )]
            for item in history[:8]
        ] + [[InlineKeyboardButton("◀️ Назад", callback_data="back")]])

    if edit:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def show_favorites(message, user_id: int, edit: bool = False):
    favorites = await get_favorites(user_id, limit=1000)
    if not favorites:
        text = "⭐ Избранное пока пустое."
        keyboard = get_main_keyboard()
    else:
        lines = []
        keyboard_rows = []
        for favorite in favorites:
            post_id = favorite["id"]
            tags = favorite.get("tags", "")
            if len(tags) > 60:
                tags = tags[:60] + "..."
            lines.append(
                f"• `{md_code(post_id)}` rating: {md_code(favorite.get('rating', ''))} "
                f"score: {favorite.get('score', 0)}\n`{md_code(tags)}`"
            )
            keyboard_rows.append([
                InlineKeyboardButton(
                    f"📤 {post_id}",
                    callback_data=f"fav_open_{post_id}"
                ),
                InlineKeyboardButton(
                    "❌ Удалить",
                    callback_data=f"fav_remove_{post_id}"
                )
            ])

        text = "⭐ *Избранное:*\n\n" + "\n".join(lines)
        keyboard_rows.append([InlineKeyboardButton("▶️ Смотреть все", callback_data="fav_all")])
        keyboard_rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
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
        await send_image(update.message, user_id, text)

    elif state == "waiting_sub_new":
        user_states.pop(user_id, None)
        user_states[user_id] = f"waiting_sub_interval_{text}"
        await update.message.reply_text(
            f"🔔 Подписка на: `{text}`\n\n"
            "Введите интервал в минутах от 1 до 120 (по умолчанию 10):",
            parse_mode="Markdown"
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
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "❌ Подписка не найдена.",
                reply_markup=get_subscriptions_keyboard()
            )

    elif state and state.startswith("waiting_sub_interval_"):
        query = state.replace("waiting_sub_interval_", "", 1)
        user_states.pop(user_id, None)

        interval = parse_subscription_interval(text)

        success = await add_subscription(user_id, query, interval)

        if success:
            await update.message.reply_text(
                f"✅ Подписка на `{query}` активирована!\n\n"
                f"Вы будете получать новые посты каждые {interval} минут.",
                reply_markup=get_subscriptions_keyboard(),
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "❌ Не удалось добавить подписку.",
                reply_markup=get_subscriptions_keyboard()
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
                f"✅ Добавлены: {', '.join(f'`{t}`' for t in added)}")
        if already:
            msg_parts.append(
                f"⚠️ Уже были: {', '.join(f'`{t}`' for t in already)}")

        await update.message.reply_text(
            "\n".join(msg_parts) or "Ничего не добавлено",
            reply_markup=get_blacklist_keyboard(),
            parse_mode="Markdown"
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
                f"✅ Удалены: {', '.join(f'`{t}`' for t in removed)}")
        if not_found:
            msg_parts.append(
                f"⚠️ Не найдены: {', '.join(f'`{t}`' for t in not_found)}")

        await update.message.reply_text(
            "\n".join(msg_parts) or "Ничего не удалено",
            reply_markup=get_blacklist_keyboard(),
            parse_mode="Markdown"
        )

    else:
        # По умолчанию - поиск
        await send_image(update.message, user_id, text)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /search"""
    if context.args:
        tags = " ".join(context.args)
        await send_image(update.message, update.effective_user.id, tags)
    else:
        await update.message.reply_text(
            "Использование: `/search <теги>`\n"
            "Пример: `/search anime girl`",
            parse_mode="Markdown"
        )


async def subscriptions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /subscriptions"""
    await update.message.reply_text(
        "🔔 *Управление подписками*\n\n"
        "Вы можете подписаться на поиск, и бот будет автоматически "
        "присылать новые посты по расписанию!",
        reply_markup=get_subscriptions_keyboard(),
        parse_mode="Markdown"
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
    caption_enabled = "✅ Включено" if settings.get(
        'show_caption', True) else "❌ Выключено"

    await update.message.reply_text(
        "⚙️ *Настройки бота*\n\n"
        f"Описание картинок: {caption_enabled}\n\n"
        "Вы можете настроить:\n"
        "• Какие элементы показывать в описании\n"
        "• Показывать ли запрос поиска\n"
        "• Метку автоматической рассылки\n"
        "• И многое другое",
        reply_markup=get_settings_keyboard(),
        parse_mode="Markdown"
    )


async def tags_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /tags - поиск/автодополнение тегов"""
    if context.args:
        query = " ".join(context.args)
        suggestions = await api.autocomplete(query)

        if suggestions:
            tags_list = "\n".join(f"• `{tag}`" for tag in suggestions)
            await update.message.reply_text(
                f"🔖 *Найденные теги для* `{query}`:\n\n{tags_list}",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"❌ Теги по запросу `{query}` не найдены",
                parse_mode="Markdown"
            )
    else:
        await update.message.reply_text(
            "Использование: `/tags <запрос>`\n"
            "Пример: `/tags blon` → покажет теги начинающиеся на 'blon'",
            parse_mode="Markdown"
        )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /id - получить пост по ID"""
    if context.args and context.args[0].isdigit():
        post_id = int(context.args[0])

        status_msg = await update.message.reply_text("🔍 Ищу...")

        result = await api.get_post_by_id(post_id)

        await status_msg.delete()

        if result:
            user_id = update.effective_user.id
            settings = await get_user_settings(user_id)

            file_url = result.get("file_url", "")

            # Строим описание на основе настроек
            caption = ""
            if settings.get('show_caption', True):
                caption = await build_caption(settings, result, f"id:{post_id}")

            keyboard = get_image_keyboard(post_id)

            try:
                if file_url.lower().endswith(('.mp4', '.webm')):
                    await update.message.reply_video(
                        file_url, caption=caption if caption else None,
                        parse_mode="Markdown", reply_markup=keyboard
                    )
                else:
                    await update.message.reply_photo(
                        file_url, caption=caption if caption else None,
                        parse_mode="Markdown", reply_markup=keyboard
                    )
            except:
                if caption:
                    await update.message.reply_text(
                        f"🖼 [Открыть]({file_url})\n\n{caption}",
                        parse_mode="Markdown", reply_markup=keyboard
                    )
                else:
                    await update.message.reply_text(
                        f"🖼 [Открыть]({file_url})",
                        parse_mode="Markdown", reply_markup=keyboard
                    )
        else:
            await update.message.reply_text(f"❌ Пост с ID `{post_id}` не найден")
    else:
        await update.message.reply_text(
            "Использование: `/id <номер>`\n"
            "Пример: `/id 1234567`",
            parse_mode="Markdown"
        )


async def blacklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /blacklist"""
    await update.message.reply_text(
        "🚫 *Настройки Blacklist*",
        reply_markup=get_blacklist_keyboard(),
        parse_mode="Markdown"
    )


async def process_subscriptions(app):
    """Фоновая задача для обработки подписок"""
    logger.info("Запущена фоновая задача для подписок")

    while True:
        try:
            # Получаем подписки которые нужно отправить
            due_subs = await get_due_subscriptions()

            for user_id, query, interval in due_subs:
                try:
                    # Отправляем пост
                    logger.info(
                        f"Отправляем подписку пользователю {user_id}: {query}")

                    # Получаем blacklist пользователя
                    blacklist = await get_user_blacklist(user_id)

                    # Получаем настройки пользователя
                    settings = await get_user_settings(user_id)

                    # Ищем пост
                    excluded_post_ids = await get_sent_post_ids(user_id)
                    result = await api.get_random_image(
                        query,
                        blacklist,
                        excluded_post_ids
                    )

                    if result:
                        # Обновляем время отправки
                        await update_subscription_time(user_id, query)

                        file_url = result.get("file_url", "")
                        post_id = result.get("id", 0)
                        if post_id:
                            await mark_post_sent(user_id, int(post_id))
                        keyboard = get_subscription_image_keyboard(post_id, query)

                        # Строим описание на основе настроек
                        caption = ""
                        if settings.get('show_caption', True):
                            caption = await build_caption(settings, result, query, True)

                        # Отправляем сообщение
                        try:
                            if file_url.lower().endswith(('.mp4', '.webm')):
                                await app.bot.send_video(
                                    chat_id=user_id,
                                    video=file_url,
                                    caption=caption if caption else None,
                                    parse_mode="Markdown",
                                    reply_markup=keyboard
                                )
                            elif file_url.lower().endswith('.gif'):
                                await app.bot.send_animation(
                                    chat_id=user_id,
                                    animation=file_url,
                                    caption=caption if caption else None,
                                    parse_mode="Markdown",
                                    reply_markup=keyboard
                                )
                            else:
                                await app.bot.send_photo(
                                    chat_id=user_id,
                                    photo=file_url,
                                    caption=caption if caption else None,
                                    parse_mode="Markdown",
                                    reply_markup=keyboard
                                )
                        except Exception as send_error:
                            logger.error(
                                f"Ошибка отправки подписки: {send_error}")
                            # Если не удалось отправить медиа, отправляем текст
                            if caption:
                                await app.bot.send_message(
                                    chat_id=user_id,
                                    text=f"🔔 *Автоматическая рассылка*\n\n{caption}",
                                    parse_mode="Markdown",
                                    reply_markup=keyboard
                                )

                except Exception as user_error:
                    logger.error(
                        f"Ошибка обработки подписки для {user_id}: {user_error}")

            # Ждем 1 минуту перед следующей проверкой
            await asyncio.sleep(60)

        except Exception as e:
            logger.error(f"Ошибка в фоновой задаче подписок: {e}")
            await asyncio.sleep(60)


async def post_init(application):
    """Инициализация после запуска"""
    await init_db()

    # Запускаем фоновую задачу для подписок
    global subscription_task
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


def main():
    """Запуск бота"""
    missing_config = validate_config()
    if missing_config:
        logger.error(
            "Не установлены обязательные переменные окружения: %s",
            ", ".join(missing_config)
        )
        return

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("blacklist", blacklist_command))
    application.add_handler(CommandHandler(
        "subscriptions", subscriptions_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("favorites", favorites_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("tags", tags_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        message_handler
    ))

    logger.info("Бот запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
