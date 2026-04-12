import aiosqlite
from typing import Set, Optional, List, Tuple, Dict, Any
import asyncio
import json
import logging

DB_PATH = "bot_data.db"
logger = logging.getLogger(__name__)


async def init_db():
    """Инициализация базы данных"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Существующие таблицы
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
                tags TEXT DEFAULT '',
                rating TEXT DEFAULT '',
                score INTEGER DEFAULT 0,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, query, post_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Новая таблица для настроек пользователя
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

        await db.commit()


async def get_user_blacklist(user_id: int) -> Set[str]:
    """Получить blacklist пользователя"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT tag FROM blacklist WHERE user_id = ?",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}


async def add_to_blacklist(user_id: int, tag: str) -> bool:
    """Добавить тег в blacklist"""
    tag = tag.lower().strip()
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO blacklist (user_id, tag) VALUES (?, ?)",
                (user_id, tag)
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_from_blacklist(user_id: int, tag: str) -> bool:
    """Удалить тег из blacklist"""
    tag = tag.lower().strip()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM blacklist WHERE user_id = ? AND tag = ?",
            (user_id, tag)
        )
        await db.commit()
        return cursor.rowcount > 0


async def save_user_query(user_id: int, query: str, pid: int = 0):
    """Сохранить последний запрос пользователя"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO users (user_id, last_query, last_pid)
            VALUES (?, ?, ?)
        """, (user_id, query, pid))
        await db.execute("""
            INSERT INTO search_history (user_id, query)
            VALUES (?, ?)
        """, (user_id, query.strip()))
        await db.commit()


async def get_user_query(user_id: int) -> Optional[tuple]:
    """Получить последний запрос пользователя"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT last_query, last_pid FROM users WHERE user_id = ?",
            (user_id,)
        )
        return await cursor.fetchone()


async def get_sent_post_ids(user_id: int) -> Set[int]:
    """Получить ID постов, которые уже отправлялись пользователю."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT post_id FROM sent_posts WHERE user_id = ?",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}


async def mark_post_sent(user_id: int, post_id: int):
    """Запомнить отправленный пользователю пост."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO sent_posts (user_id, post_id)
            VALUES (?, ?)
        """, (user_id, post_id))
        await db.commit()


async def get_search_history(user_id: int, limit: int = 10) -> List[str]:
    """Получить последние поисковые запросы пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
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


# Новые функции для настроек пользователя
async def get_user_settings(user_id: int) -> Dict[str, Any]:
    """Получить настройки пользователя"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT * FROM user_settings WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()

        if row:
            # Преобразуем строку JSON в словарь
            settings = {
                'show_caption': bool(row[1]),
                'show_search_query': bool(row[2]),
                'show_subscription_label': bool(row[3]),
                'show_id': bool(row[4]),
                'show_score': bool(row[5]),
                'show_rating': bool(row[6]),
                'show_tags': bool(row[7]),
            }

            # Добавляем настройки из JSON если есть
            if row[8]:
                try:
                    json_settings = json.loads(row[8])
                    settings.update(json_settings)
                except:
                    pass

            return settings
        else:
            # Создаем настройки по умолчанию
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
            return default_settings


async def save_user_settings(user_id: int, settings: Dict[str, Any]):
    """Сохранить настройки пользователя"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Разделяем основные настройки и дополнительные
        main_settings = {}
        json_settings = {}

        main_fields = ['show_caption', 'show_search_query', 'show_subscription_label',
                       'show_id', 'show_score', 'show_rating', 'show_tags']

        for key, value in settings.items():
            if key in main_fields:
                main_settings[key] = value
            else:
                json_settings[key] = value

        # Сохраняем основные настройки в отдельные колонки
        if main_settings:
            await db.execute("""
                INSERT OR REPLACE INTO user_settings 
                (user_id, show_caption, show_search_query, show_subscription_label, 
                 show_id, show_score, show_rating, show_tags, settings_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                main_settings.get('show_caption', True),
                main_settings.get('show_search_query', True),
                main_settings.get('show_subscription_label', True),
                main_settings.get('show_id', True),
                main_settings.get('show_score', True),
                main_settings.get('show_rating', True),
                main_settings.get('show_tags', True),
                json.dumps(json_settings)
            ))

        await db.commit()


async def update_user_setting(user_id: int, setting_name: str, value: Any):
    """Обновить одну настройку пользователя"""
    settings = await get_user_settings(user_id)
    settings[setting_name] = value
    await save_user_settings(user_id, settings)


# Функции для подписок (остаются без изменений)
async def add_subscription(user_id: int, query: str, interval_minutes: int = 10) -> bool:
    """Добавить подписку на поиск"""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("""
                INSERT OR REPLACE INTO subscriptions 
                (user_id, query, interval_minutes, is_active, last_sent)
                VALUES (?, ?, ?, 1, datetime('now', '-1 hour'))
            """, (user_id, query.strip(), interval_minutes))
            await db.commit()
            return True
        except Exception as e:
            logger.exception("Error adding subscription: %s", e)
            return False


async def remove_subscription(user_id: int, query: str) -> bool:
    """Удалить подписку"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND query = ?",
            (user_id, query.strip())
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_user_subscriptions(user_id: int) -> List[Tuple[str, int]]:
    """Получить все подписки пользователя"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT query, interval_minutes FROM subscriptions WHERE user_id = ? AND is_active = 1",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return [(row[0], row[1]) for row in rows]


async def get_all_user_subscriptions(user_id: int) -> List[Tuple[str, int, bool]]:
    """Получить все подписки пользователя, включая остановленные."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT query, interval_minutes, is_active FROM subscriptions WHERE user_id = ?",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return [(row[0], row[1], bool(row[2])) for row in rows]


async def update_subscription_time(user_id: int, query: str):
    """Обновить время последней отправки"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE subscriptions 
            SET last_sent = CURRENT_TIMESTAMP 
            WHERE user_id = ? AND query = ?
        """, (user_id, query.strip()))
        await db.commit()


async def update_subscription_interval(user_id: int, query: str, interval_minutes: int) -> bool:
    """Обновить интервал подписки."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE subscriptions
            SET interval_minutes = ?
            WHERE user_id = ? AND query = ?
        """, (interval_minutes, user_id, query.strip()))
        await db.commit()
        return cursor.rowcount > 0


async def get_due_subscriptions() -> List[Tuple[int, str, int]]:
    """Получить подписки, которые нужно отправить"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT user_id, query, interval_minutes 
            FROM subscriptions 
            WHERE is_active = 1 
            AND datetime(last_sent, '+' || interval_minutes || ' minutes') <= datetime('now')
            LIMIT 50
        """)
        return await cursor.fetchall()


async def toggle_subscription(user_id: int, query: str) -> Optional[bool]:
    """Включить/выключить подписку"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Сначала получим текущее состояние
        cursor = await db.execute(
            "SELECT is_active FROM subscriptions WHERE user_id = ? AND query = ?",
            (user_id, query.strip())
        )
        row = await cursor.fetchone()

        if row:
            new_state = not bool(row[0])
            await db.execute(
                "UPDATE subscriptions SET is_active = ? WHERE user_id = ? AND query = ?",
                (new_state, user_id, query.strip())
            )
            await db.commit()
            return new_state

        return None


async def add_favorite(user_id: int, post: Dict[str, Any]) -> bool:
    """Добавить пост в избранное."""
    post_id = post.get("id")
    if post_id is None:
        return False

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("""
                INSERT INTO favorites (user_id, post_id, file_url, tags, rating, score)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                int(post_id),
                post.get("file_url", ""),
                post.get("tags", ""),
                post.get("rating", ""),
                int(post.get("score", 0) or 0),
            ))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_favorite(user_id: int, post_id: int) -> bool:
    """Удалить пост из избранного."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM favorites WHERE user_id = ? AND post_id = ?",
            (user_id, post_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_favorites(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """Получить избранные посты пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT post_id, file_url, tags, rating, score, added_at
            FROM favorites
            WHERE user_id = ?
            ORDER BY added_at DESC
            LIMIT ?
        """, (user_id, limit))
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "file_url": row[1],
                "tags": row[2],
                "rating": row[3],
                "score": row[4],
                "added_at": row[5],
            }
            for row in rows
        ]


async def add_subscription_post(user_id: int, query: str, post: Dict[str, Any]) -> bool:
    """Сохранить пост, отправленный по подписке."""
    post_id = post.get("id")
    if post_id is None:
        return False

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("""
                INSERT INTO subscription_posts
                (user_id, query, post_id, file_url, tags, rating, score)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                query.strip(),
                int(post_id),
                post.get("file_url", ""),
                post.get("tags", ""),
                post.get("rating", ""),
                int(post.get("score", 0) or 0),
            ))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def get_subscription_posts(user_id: int, query: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Получить избранные посты конкретной подписки."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT sp.post_id, sp.file_url, sp.tags, sp.rating, sp.score, sp.sent_at
            FROM subscription_posts sp
            INNER JOIN favorites f
                ON f.user_id = sp.user_id
                AND f.post_id = sp.post_id
            WHERE sp.user_id = ? AND sp.query = ?
            ORDER BY sp.sent_at DESC
            LIMIT ?
        """, (user_id, query.strip(), limit))
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "file_url": row[1],
                "tags": row[2],
                "rating": row[3],
                "score": row[4],
                "sent_at": row[5],
            }
            for row in rows
        ]


async def remove_subscription_post(user_id: int, query: str, post_id: int) -> bool:
    """Удалить пост из истории конкретной подписки."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            DELETE FROM subscription_posts
            WHERE user_id = ? AND query = ? AND post_id = ?
        """, (user_id, query.strip(), post_id))
        await db.commit()
        return cursor.rowcount > 0
