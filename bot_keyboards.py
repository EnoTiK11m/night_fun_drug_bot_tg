from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from bot_state import store_callback_payload
from database import get_user_settings


PERSISTENT_SEARCH = "🔍 Поиск"
PERSISTENT_GALLERY = "🖼 Подборка"
PERSISTENT_RANDOM = "🎲 Рандом"
PERSISTENT_FAVORITES = "⭐ Избранное"
PERSISTENT_SUBSCRIPTIONS = "🔔 Подписки"
PERSISTENT_MENU = "⚙️ Меню"


def get_persistent_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(PERSISTENT_SEARCH), KeyboardButton(PERSISTENT_GALLERY)],
            [KeyboardButton(PERSISTENT_RANDOM), KeyboardButton(PERSISTENT_FAVORITES)],
            [KeyboardButton(PERSISTENT_SUBSCRIPTIONS), KeyboardButton(PERSISTENT_MENU)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие или введите теги",
    )


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
    if query:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🔔 Подписаться",
                    callback_data=store_callback_payload("subscribe", query),
                )
            ]
        )
    if action_rows:
        keyboard.extend(action_rows)

    keyboard.append([get_favorite_button(post_id, sub_query)])
    keyboard.append([
        InlineKeyboardButton("🧠 Похожее", callback_data=f"similar_{post_id}"),
        InlineKeyboardButton("⏳ На потом", callback_data=f"later_add_{post_id}"),
    ])
    if show_tags_button:
        keyboard.append([get_tags_button(post_id)])
    keyboard.append([get_site_button(post_id)])
    keyboard.append([
        InlineKeyboardButton("🔎 Оригинал", callback_data=f"post_original_{post_id}")
    ])
    return InlineKeyboardMarkup(keyboard)


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


def get_main_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🔍 Поиск", callback_data="search"),
            InlineKeyboardButton("🧩 Конструктор", callback_data="search_builder"),
        ],
        [InlineKeyboardButton("🎲 Рандомная картинка", callback_data="random")],
        [InlineKeyboardButton("🖼 Галерея поиска", callback_data="gallery")],
        [
            InlineKeyboardButton("💾 Пресеты", callback_data="presets"),
            InlineKeyboardButton("✨ Рекомендации", callback_data="recommendations"),
        ],
        [InlineKeyboardButton("🔄 Ещё", callback_data="more")],
        [
            InlineKeyboardButton("🚫 Blacklist", callback_data="blacklist"),
            InlineKeyboardButton("📋 Подписки", callback_data="subscriptions"),
        ],
        [
            InlineKeyboardButton("🕘 История", callback_data="history"),
            InlineKeyboardButton("⭐ Избранное", callback_data="favorites"),
        ],
        [
            InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
            InlineKeyboardButton("📊 Статистика", callback_data="stats"),
        ],
        [
            InlineKeyboardButton("⏳ На потом", callback_data="later_list"),
            InlineKeyboardButton("💽 Хранилище", callback_data="storage"),
        ],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_blacklist_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("➕ Добавить тег", callback_data="bl_add")],
        [InlineKeyboardButton("➖ Удалить тег", callback_data="bl_remove")],
        [InlineKeyboardButton("📋 Показать список", callback_data="bl_show")],
        [InlineKeyboardButton("⏳ Временно скрыть", callback_data="bl_temp")],
        [InlineKeyboardButton("🧰 Готовые пресеты", callback_data="bl_presets")],
        [
            InlineKeyboardButton("📥 Импорт", callback_data="bl_import"),
            InlineKeyboardButton("📤 Экспорт", callback_data="bl_export"),
        ],
        [InlineKeyboardButton("💡 Похожие теги", callback_data="bl_suggest")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_subscriptions_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(
                "➕ Подписаться на текущий поиск", callback_data="sub_add_current"
            )
        ],
        [
            InlineKeyboardButton(
                "➕ Подписаться на новый поиск", callback_data="sub_add_new"
            )
        ],
        [InlineKeyboardButton("📋 Мои подписки", callback_data="sub_list")],
        [InlineKeyboardButton("⚙️ Управление подписками", callback_data="sub_manage")],
        [InlineKeyboardButton("📨 Отправить дайджест", callback_data="sub_digest_send")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_settings_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📝 Настройки описания", callback_data="settings_caption")],
        [InlineKeyboardButton("🖼 Галерея и фильтры", callback_data="settings_gallery")],
        [InlineKeyboardButton("📦 Качество медиа", callback_data="settings_quality")],
        [InlineKeyboardButton("🙈 Спойлеры", callback_data="settings_spoiler")],
        [
            InlineKeyboardButton(
                "⏸ Остановить все подписки на время",
                callback_data="settings_pause_subscriptions",
            )
        ],
        [
            InlineKeyboardButton(
                "▶️ Возобновить подписки",
                callback_data="settings_resume_subscriptions",
            )
        ],
        [InlineKeyboardButton("🔄 Сброс настроек", callback_data="settings_reset")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back")],
    ]
    return InlineKeyboardMarkup(keyboard)


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
        bulk.append(InlineKeyboardButton("💾 Пресет", callback_data=save_preset_callback))
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
