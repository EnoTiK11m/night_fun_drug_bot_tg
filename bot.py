import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from config import BOT_TOKEN
from database import (
    init_db,
    get_user_blacklist,
    add_to_blacklist,
    remove_from_blacklist,
    save_user_query,
    get_user_query,
    add_subscription,
    remove_subscription,
    get_user_subscriptions,
    update_subscription_time,
    get_due_subscriptions,
    toggle_subscription,
    get_user_settings,
    update_user_setting,
    save_user_settings
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
                "🔔 Подписаться", callback_data=f"subscribe_{query}")
        ])

    return InlineKeyboardMarkup(keyboard)


async def build_caption(settings: dict, result: dict, query: str = "", is_subscription: bool = False) -> str:
    """Построить описание на основе настроек"""
    caption_parts = []

    # Заголовок для подписки
    if is_subscription and settings.get('show_subscription_label', True):
        caption_parts.append("🔔 *Автоматическая рассылка*")

    # Запрос поиска
    if query and settings.get('show_search_query', True):
        caption_parts.append(f"Запрос: `{query}`")

    # Основная информация о посте
    if settings.get('show_id', True):
        caption_parts.append(f"🆔 ID: `{result.get('id', 0)}`")

    if settings.get('show_score', True):
        caption_parts.append(f"📊 Score: {result.get('score', 0)}")

    if settings.get('show_rating', True):
        caption_parts.append(f"🏷 Rating: {result.get('rating', 'unknown')}")

    # Теги
    if settings.get('show_tags', True):
        post_tags = result.get("tags", "")
        if len(post_tags) > 150:
            post_tags = post_tags[:150] + "..."
        caption_parts.append(f"🔖 Tags: `{post_tags}`")

    # Если все описание выключено, возвращаем пустую строку
    if not caption_parts:
        return ""

    # Собираем все части
    if len(caption_parts) == 1:
        return caption_parts[0]
    elif len(caption_parts) == 2:
        return f"{caption_parts[0]}\n{caption_parts[1]}"
    else:
        # Первый элемент как заголовок, остальные как список
        return f"{caption_parts[0]}\n" + "\n".join(caption_parts[1:])


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
                "Введите интервал в минутах (по умолчанию 10):",
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
        subscriptions = await get_user_subscriptions(user_id)
        if subscriptions:
            subs_list = []
            for sub_query, interval in subscriptions:
                subs_list.append(f"• `{sub_query}` - каждые {interval} мин.")

            text = "📋 *Ваши подписки:*\n\n" + "\n".join(subs_list)
        else:
            text = "📋 У вас пока нет подписок."

        await query.edit_message_text(
            text,
            reply_markup=get_subscriptions_keyboard(),
            parse_mode="Markdown"
        )

    elif data == "sub_manage":
        subscriptions = await get_user_subscriptions(user_id)
        if not subscriptions:
            await query.edit_message_text(
                "❌ У вас нет активных подписок.",
                reply_markup=get_subscriptions_keyboard()
            )
            return

        # Создаем клавиатуру для управления
        keyboard = []
        for sub_query, interval in subscriptions:
            keyboard.append([
                InlineKeyboardButton(
                    f"❌ {sub_query[:20]}...",
                    callback_data=f"sub_remove_{sub_query}"
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

    elif data.startswith("subscribe_"):
        # Подписка из клавиатуры под изображением
        sub_query = data.replace("subscribe_", "", 1)
        success = await add_subscription(user_id, sub_query, 10)

        if success:
            await query.edit_message_text(
                f"✅ Подписка на `{sub_query}` активирована!\n\n"
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
        sub_query = data.replace("sub_remove_", "", 1)
        success = await remove_subscription(user_id, sub_query)

        if success:
            await query.edit_message_text(
                f"✅ Подписка на `{sub_query}` удалена.",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                "❌ Подписка не найдена.",
                parse_mode="Markdown"
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

    if not is_subscription:  # Не показываем статус для подписок
        status_msg = await message.reply_text("🔍 Ищу...")

    # Если это кнопка "ещё", используем улучшенную логику
    if is_more:
        result = await api.get_next_image(user_id, tags, blacklist)
    else:
        result = await api.get_random_image(tags, blacklist)
        # Сохраняем историю поиска для кнопки "ещё"
        if result:
            await api.save_search_state(user_id, tags, blacklist)

    if not is_subscription:
        await status_msg.delete()

    if result:
        await save_user_query(user_id, tags)

        file_url = result.get("file_url", "")
        post_id = result.get("id", 0)

        # Строим описание на основе настроек
        caption = ""
        if settings.get('show_caption', True):
            caption = await build_caption(settings, result, tags, is_subscription)

        # Для подписок не добавляем кнопку подписки (чтобы избежать рекурсии)
        if is_subscription:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🌐 Открыть на сайте",
                    url=f"https://rule34.xxx/index.php?page=post&s=view&id={post_id}"
                )
            ]])
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
            "Введите интервал в минутах (по умолчанию 10):",
            parse_mode="Markdown"
        )

    elif state and state.startswith("waiting_sub_interval_"):
        query = state.replace("waiting_sub_interval_", "", 1)
        user_states.pop(user_id, None)

        try:
            interval = int(text) if text.isdigit() else 10
            interval = max(1, min(interval, 1440))  # Ограничиваем 1-1440 минут
        except:
            interval = 10

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
                    result = await api.get_random_image(query, blacklist)

                    if result:
                        # Обновляем время отправки
                        await update_subscription_time(user_id, query)

                        file_url = result.get("file_url", "")
                        post_id = result.get("id", 0)

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
                                    parse_mode="Markdown"
                                )
                            elif file_url.lower().endswith('.gif'):
                                await app.bot.send_animation(
                                    chat_id=user_id,
                                    animation=file_url,
                                    caption=caption if caption else None,
                                    parse_mode="Markdown"
                                )
                            else:
                                await app.bot.send_photo(
                                    chat_id=user_id,
                                    photo=file_url,
                                    caption=caption if caption else None,
                                    parse_mode="Markdown"
                                )
                        except Exception as send_error:
                            logger.error(
                                f"Ошибка отправки подписки: {send_error}")
                            # Если не удалось отправить медиа, отправляем текст
                            if caption:
                                await app.bot.send_message(
                                    chat_id=user_id,
                                    text=f"🔔 *Автоматическая рассылка*\n\n{caption}",
                                    parse_mode="Markdown"
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
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не установлен в .env файле!")
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
