from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from bot_state import store_callback_payload
from database import get_user_settings


PERSISTENT_SEARCH = "🔎 Найти"
PERSISTENT_GALLERY = "🖼 Подборка"
PERSISTENT_RANDOM = "🎲 Случайное"
PERSISTENT_FAVORITES = "⭐ Библиотека"
PERSISTENT_SUBSCRIPTIONS = "🔔 Подписки"
PERSISTENT_MENU = "☰ Все разделы"

LEGACY_PERSISTENT_SEARCH = "🔍 Поиск"
LEGACY_PERSISTENT_RANDOM = "🎲 Рандом"
LEGACY_PERSISTENT_FAVORITES = "⭐ Моя библиотека"
LEGACY_PERSISTENT_MENU = "☰ Меню"


def get_persistent_keyboard(interface_mode: str = "simple") -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(PERSISTENT_SEARCH), KeyboardButton(PERSISTENT_RANDOM)],
        [KeyboardButton(PERSISTENT_FAVORITES), KeyboardButton(PERSISTENT_MENU)],
    ]
    if interface_mode == "advanced":
        rows.insert(
            1,
            [KeyboardButton(PERSISTENT_GALLERY), KeyboardButton(PERSISTENT_SUBSCRIPTIONS)],
        )
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите раздел или отправьте теги",
    )


def get_onboarding_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Найти изображение", callback_data="search")],
        [InlineKeyboardButton("🎲 Показать случайное", callback_data="random")],
        [InlineKeyboardButton("📖 Как пользоваться", callback_data="context_help_start")],
    ])


def get_cancel_keyboard(back_callback: str = "back") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_input")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=back_callback)],
    ])


def get_tags_button(post_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton("🏷 Все теги", callback_data=f"post_tags_{post_id}")


def get_site_button(post_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        "🌐 Открыть на сайте",
        url=f"https://rule34.xxx/index.php?page=post&s=view&id={post_id}",
    )


def get_favorite_button(post_id: int, sub_query: str = "") -> InlineKeyboardButton:
    favorite_callback = f"sub_fav_{post_id}" if sub_query else f"fav_{post_id}"
    return InlineKeyboardButton("⭐ В избранное", callback_data=favorite_callback)


def build_post_keyboard(
    post_id: int,
    action_rows: list[list[InlineKeyboardButton]] | None = None,
    query: str = "",
    sub_query: str = "",
    show_tags_button: bool = True,
) -> InlineKeyboardMarkup:
    keyboard = []
    if action_rows:
        keyboard.extend(action_rows)

    keyboard.append([
        get_favorite_button(post_id, sub_query),
        InlineKeyboardButton("⏳ На потом", callback_data=f"later_add_{post_id}"),
    ])
    keyboard.append([
        InlineKeyboardButton("🧠 Похожее", callback_data=f"similar_{post_id}"),
        InlineKeyboardButton("••• Ещё", callback_data=f"post_more_{post_id}"),
    ])
    if query:
        keyboard.append([InlineKeyboardButton(
            "🔔 Подписаться",
            callback_data=store_callback_payload("subscribe", query),
        )])
    return InlineKeyboardMarkup(keyboard)


def get_post_more_keyboard(
    post_id: int, show_tags_button: bool = True
) -> InlineKeyboardMarkup:
    rows = []
    if show_tags_button:
        rows.append([get_tags_button(post_id)])
    rows.extend([
        [InlineKeyboardButton("🔎 Оригинал", callback_data=f"post_original_{post_id}")],
        [get_site_button(post_id)],
        [InlineKeyboardButton("◀️ Назад", callback_data=f"post_compact_{post_id}")],
    ])
    return InlineKeyboardMarkup(rows)


def get_subscription_gallery_keyboard(
    token: str,
    index: int,
    total: int,
    post_id: int,
    show_tags_button: bool = True,
) -> InlineKeyboardMarkup:
    prev_index = (index - 1) % total
    next_index = (index + 1) % total
    keyboard = [
        [
            InlineKeyboardButton("◀️", callback_data=f"sub_page_{token}_{prev_index}"),
            InlineKeyboardButton(f"{index + 1}/{total}", callback_data="noop"),
            InlineKeyboardButton("▶️", callback_data=f"sub_page_{token}_{next_index}"),
        ],
        [
            InlineKeyboardButton(
                "❌ Удалить",
                callback_data=f"sub_post_del_{token}_{post_id}_{index}",
            )
        ],
    ]
    if show_tags_button:
        keyboard.append([get_tags_button(post_id)])
    keyboard.append(
        [InlineKeyboardButton("📋 Список", callback_data=f"sub_list_posts_{token}")]
    )
    return InlineKeyboardMarkup(keyboard)


def get_favorites_gallery_keyboard(
    index: int,
    total: int,
    post_id: int,
    show_tags_button: bool = True,
) -> InlineKeyboardMarkup:
    prev_index = (index - 1) % total
    next_index = (index + 1) % total
    keyboard = [
        [
            InlineKeyboardButton("◀️", callback_data=f"fav_page_{prev_index}"),
            InlineKeyboardButton(f"{index + 1}/{total}", callback_data="noop"),
            InlineKeyboardButton("▶️", callback_data=f"fav_page_{next_index}"),
        ],
        [
            InlineKeyboardButton(
                "❌ Удалить", callback_data=f"fav_del_{post_id}_{index}"
            ),
            InlineKeyboardButton("🗂 В коллекцию", callback_data=f"fav_col_pick_{post_id}"),
        ],
        [InlineKeyboardButton("📝 Заметка", callback_data=f"fav_note_{post_id}")],
    ]
    if show_tags_button:
        keyboard.append([get_tags_button(post_id)])
    keyboard.append([InlineKeyboardButton("📋 Список", callback_data="fav_list")])
    return InlineKeyboardMarkup(keyboard)


def get_main_keyboard(interface_mode: str = "simple") -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🔎 Поиск", callback_data="search_hub"),
            InlineKeyboardButton("🖼 Подборка", callback_data="gallery"),
        ],
        [
            InlineKeyboardButton("⭐ Библиотека", callback_data="library"),
            InlineKeyboardButton("🔔 Подписки", callback_data="subscriptions"),
        ],
        [
            InlineKeyboardButton("🚫 Чёрный список", callback_data="blacklist"),
            InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
        ],
    ]
    if interface_mode == "advanced":
        keyboard.append([
            InlineKeyboardButton("👤 Мои данные", callback_data="my_data"),
            InlineKeyboardButton("❓ Помощь", callback_data="help"),
        ])
    else:
        keyboard.append([InlineKeyboardButton("❓ Помощь", callback_data="help")])
    return InlineKeyboardMarkup(keyboard)


def get_data_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Активность", callback_data="stats"),
            InlineKeyboardButton("💽 Хранилище", callback_data="storage"),
        ],
        [InlineKeyboardButton("📤 Экспорт библиотеки", callback_data="fav_export")],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="back")],
    ])


def get_help_keyboard(interface_mode: str = "simple") -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🔎 Поиск", callback_data="search_hub"),
            InlineKeyboardButton("⭐ Библиотека", callback_data="library"),
        ],
        [
            InlineKeyboardButton("🔔 Подписки", callback_data="subscriptions"),
            InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
        ],
        [InlineKeyboardButton("🚫 Чёрный список", callback_data="blacklist")],
    ]
    if interface_mode == "advanced":
        rows.append([InlineKeyboardButton("👤 Мои данные", callback_data="my_data")])
    rows.append([InlineKeyboardButton("⬅️ Главное меню", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def get_library_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Смотреть избранное", callback_data="fav_gallery")],
        [
            InlineKeyboardButton("🗂 Коллекции", callback_data="fav_collections"),
            InlineKeyboardButton("🕓 На потом", callback_data="later_list"),
        ],
        [
            InlineKeyboardButton("🔎 Найти в библиотеке", callback_data="fav_find"),
            InlineKeyboardButton("✨ Рекомендации", callback_data="recommendations"),
        ],
        [InlineKeyboardButton("ℹ️ Как устроена библиотека", callback_data="context_help_library")],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="back")],
    ])


def get_search_hub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔎 Поиск по тегам", callback_data="search"),
            InlineKeyboardButton("🎲 Случайное", callback_data="random"),
        ],
        [InlineKeyboardButton("🖼 Подборка изображений", callback_data="gallery")],
        [
            InlineKeyboardButton("🧩 Конструктор запроса", callback_data="search_builder"),
            InlineKeyboardButton("💾 Сохранённые запросы", callback_data="presets"),
        ],
        [InlineKeyboardButton("🕘 История поиска", callback_data="history")],
        [InlineKeyboardButton("ℹ️ Как работает поиск", callback_data="context_help_search")],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="back")],
    ])


def get_blacklist_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("➕ Добавить", callback_data="bl_add"),
            InlineKeyboardButton("➖ Удалить", callback_data="bl_remove"),
        ],
        [InlineKeyboardButton("📋 Посмотреть чёрный список", callback_data="bl_show")],
        [InlineKeyboardButton("🕓 Скрыть тег временно", callback_data="bl_temp")],
        [
            InlineKeyboardButton("🧰 Готовые наборы", callback_data="bl_presets"),
            InlineKeyboardButton("💡 Подобрать похожие", callback_data="bl_suggest"),
        ],
        [
            InlineKeyboardButton("📥 Импорт", callback_data="bl_import"),
            InlineKeyboardButton("📤 Экспорт", callback_data="bl_export"),
        ],
        [InlineKeyboardButton("ℹ️ Как работает чёрный список", callback_data="context_help_blacklist")],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="back")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_subscriptions_keyboard(
    subscriptions_paused: bool = False,
    has_digest_posts: bool = False,
) -> InlineKeyboardMarkup:
    subscription_control = (
        InlineKeyboardButton(
            "▶️ Возобновить все подписки",
            callback_data="settings_resume_subscriptions",
        )
        if subscriptions_paused
        else InlineKeyboardButton(
            "⏸ Приостановить все подписки",
            callback_data="settings_pause_subscriptions",
        )
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "➕ Новая подписка", callback_data="sub_add_new"
            )
        ],
        [
            InlineKeyboardButton(
                "🔎 Подписаться на текущий поиск", callback_data="sub_add_current"
            )
        ],
        [
            InlineKeyboardButton("📋 Мои подписки", callback_data="sub_list"),
            InlineKeyboardButton("⚙️ Управление", callback_data="sub_manage"),
        ],
        [subscription_control],
    ]
    if has_digest_posts:
        keyboard.append([
            InlineKeyboardButton(
                "📨 Отправить накопленный дайджест",
                callback_data="sub_digest_send",
            )
        ])
    keyboard.extend([
        [InlineKeyboardButton("ℹ️ Как работают подписки", callback_data="context_help_subscriptions")],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="back")],
    ])
    return InlineKeyboardMarkup(keyboard)


def get_settings_keyboard(settings: dict | None = None) -> InlineKeyboardMarkup:
    settings = settings or {}
    caption_state = "включены ✅" if settings.get("show_caption", True) else "выключены ❌"
    spoiler_labels = {
        "off": "выключены",
        "explicit": "только explicit",
        "all": "для всех",
    }
    quality_labels = {
        "auto": "автоматически",
        "preview": "preview",
        "sample": "sample",
        "original": "оригинал",
    }
    interface_mode = settings.get("interface_mode", "simple")
    interface_label = "простой" if interface_mode == "simple" else "расширенный"
    keyboard = [
        [
            InlineKeyboardButton(f"📝 Подписи: {caption_state}", callback_data="settings_caption"),
        ],
        [
            InlineKeyboardButton(
                f"🙈 Спойлеры: {spoiler_labels.get(settings.get('spoiler_mode'), 'выключены')}",
                callback_data="settings_spoiler",
            ),
        ],
        [
            InlineKeyboardButton(
                f"🖼 Подборка: {settings.get('gallery_size', 10)} изображений",
                callback_data="settings_gallery",
            ),
            InlineKeyboardButton(
                f"📦 Качество: {quality_labels.get(settings.get('quality_mode'), 'автоматически')}",
                callback_data="settings_quality",
            ),
        ],
        [InlineKeyboardButton(f"🧭 Интерфейс: {interface_label}", callback_data="settings_interface_mode")],
        [InlineKeyboardButton("🔄 Сбросить настройки", callback_data="settings_reset")],
        [InlineKeyboardButton("ℹ️ Что можно настроить", callback_data="context_help_settings")],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="back")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_favorites_album_keyboard(
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton("◀️", callback_data=f"fav_page_{page - 1}")
        )
    navigation.append(
        InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
    )
    if page + 1 < total_pages:
        navigation.append(
            InlineKeyboardButton("▶️", callback_data=f"fav_page_{page + 1}")
        )
    return InlineKeyboardMarkup([
        navigation,
        [
            InlineKeyboardButton("📋 Список", callback_data="fav_list"),
            InlineKeyboardButton("⭐ Меню", callback_data="favorites"),
        ],
    ])


def get_gallery_settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    labels = {"random": "случайно", "new": "новые", "popular": "популярные"}
    ratings = {"all": "все", "s": "safe", "q": "questionable", "e": "explicit"}
    types = {"all": "все", "images": "изображения", "animations": "GIF", "videos": "видео"}
    orientations = {"any": "любая", "portrait": "вертикальная", "landscape": "горизонтальная", "square": "квадрат"}
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"↕️ Сортировка: {labels.get(settings.get('gallery_sort'), 'случайно')}",
            callback_data="gallery_cycle_sort",
        )],
        [InlineKeyboardButton(
            f"🔞 Rating: {ratings.get(settings.get('rating_filter'), 'все')}",
            callback_data="gallery_cycle_rating",
        )],
        [InlineKeyboardButton(
            f"🎞 Тип: {types.get(settings.get('media_type'), 'все')}",
            callback_data="gallery_cycle_type",
        )],
        [InlineKeyboardButton(
            f"📐 Ориентация: {orientations.get(settings.get('orientation'), 'любая')}",
            callback_data="gallery_cycle_orientation",
        )],
        [
            InlineKeyboardButton("➖", callback_data="gallery_size_down"),
            InlineKeyboardButton(f"В альбоме: {settings.get('gallery_size', 10)}", callback_data="noop"),
            InlineKeyboardButton("➕", callback_data="gallery_size_up"),
        ],
        [InlineKeyboardButton("📏 Минимальное разрешение", callback_data="gallery_resolution")],
        [InlineKeyboardButton("◀️ Назад", callback_data="settings")],
    ])


def get_quality_settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    labels = {"auto": "авто", "preview": "preview", "sample": "sample", "original": "оригинал"}
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🖼 Качество: {labels.get(settings.get('quality_mode'), 'авто')}",
            callback_data="quality_cycle_mode",
        )],
        [
            InlineKeyboardButton("➖", callback_data="quality_max_down"),
            InlineKeyboardButton(f"Лимит: {settings.get('max_file_mb', 10)} MiB", callback_data="noop"),
            InlineKeyboardButton("➕", callback_data="quality_max_up"),
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="settings")],
    ])


def get_gallery_result_keyboard(
    next_callback: str,
    previous_callback: str | None = None,
    bulk_favorite_callback: str | None = None,
    save_preset_callback: str | None = None,
    subscribe_callback: str | None = None,
    collection_callback: str | None = None,
) -> InlineKeyboardMarkup:
    navigation = []
    if previous_callback:
        navigation.append(InlineKeyboardButton("⬅️ Предыдущая", callback_data=previous_callback))
    navigation.append(InlineKeyboardButton("Следующая ➡️", callback_data=next_callback))
    rows = [
        navigation,
        [InlineKeyboardButton("🔍 Новый запрос", callback_data="gallery")],
        [InlineKeyboardButton("⚙️ Фильтры", callback_data="settings_gallery")],
    ]
    bulk = []
    if bulk_favorite_callback:
        bulk.append(InlineKeyboardButton("⭐ Сохранить все", callback_data=bulk_favorite_callback))
    if save_preset_callback:
        bulk.append(InlineKeyboardButton("💾 Сохранить запрос", callback_data=save_preset_callback))
    if bulk:
        rows.append(bulk)
    secondary = []
    if subscribe_callback:
        secondary.append(InlineKeyboardButton("🔔 Подписка", callback_data=subscribe_callback))
    if collection_callback:
        secondary.append(InlineKeyboardButton("🗂 В коллекцию", callback_data=collection_callback))
    if secondary:
        rows.append(secondary)
    rows.append([InlineKeyboardButton("◀️ Меню", callback_data="back")])
    return InlineKeyboardMarkup(rows)


async def get_caption_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    settings = await get_user_settings(user_id)

    keyboard = [
        [
            InlineKeyboardButton(
                (
                    "✅ Показывать описание"
                    if settings.get("show_caption", True)
                    else "❌ Скрыть описание"
                ),
                callback_data="toggle_show_caption",
            )
        ],
        [
            InlineKeyboardButton(
                (
                    "✅ Запрос поиска"
                    if settings.get("show_search_query", True)
                    else "❌ Запрос поиска"
                ),
                callback_data="toggle_show_search_query",
            )
        ],
        [
            InlineKeyboardButton(
                (
                    "✅ Метка подписки"
                    if settings.get("show_subscription_label", True)
                    else "❌ Метка подписки"
                ),
                callback_data="toggle_show_subscription_label",
            )
        ],
        [
            InlineKeyboardButton(
                "✅ ID поста" if settings.get("show_id", True) else "❌ ID поста",
                callback_data="toggle_show_id",
            ),
            InlineKeyboardButton(
                "✅ Рейтинг"
                if settings.get("show_rating", True)
                else "❌ Рейтинг",
                callback_data="toggle_show_rating",
            ),
        ],
        [
            InlineKeyboardButton(
                "✅ Очки" if settings.get("show_score", True) else "❌ Очки",
                callback_data="toggle_show_score",
            ),
            InlineKeyboardButton(
                "✅ Теги" if settings.get("show_tags", True) else "❌ Теги",
                callback_data="toggle_show_tags",
            ),
        ],
        [
            InlineKeyboardButton(
                (
                    "✅ Кнопка всех тегов"
                    if settings.get("show_tags_button", True)
                    else "❌ Кнопка всех тегов"
                ),
                callback_data="toggle_show_tags_button",
            )
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="settings")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_image_keyboard(
    post_id: int,
    query: str = "",
    show_tags_button: bool = True,
) -> InlineKeyboardMarkup:
    return build_post_keyboard(
        post_id,
        action_rows=[
            [
                InlineKeyboardButton("🔄 Ещё", callback_data="more"),
                InlineKeyboardButton("🔍 Новый поиск", callback_data="search"),
            ]
        ],
        query=query,
        show_tags_button=show_tags_button,
    )


def get_random_image_keyboard(
    post_id: int,
    show_tags_button: bool = True,
) -> InlineKeyboardMarkup:
    return build_post_keyboard(
        post_id,
        action_rows=[
            [
                InlineKeyboardButton("🎲 Ещё рандом", callback_data="random"),
                InlineKeyboardButton("🔍 Новый поиск", callback_data="search"),
            ]
        ],
        show_tags_button=show_tags_button,
    )


def get_subscription_image_keyboard(
    post_id: int,
    sub_query: str = "",
    show_tags_button: bool = True,
) -> InlineKeyboardMarkup:
    return build_post_keyboard(
        post_id,
        sub_query=sub_query,
        show_tags_button=show_tags_button,
    )
