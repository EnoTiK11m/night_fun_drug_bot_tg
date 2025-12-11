import aiosqlite
from typing import Set, Optional, List, Tuple, Dict, Any
import asyncio
import json

DB_PATH = "bot_data.db"


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
        await db.commit()


async def get_user_query(user_id: int) -> Optional[tuple]:
    """Получить последний запрос пользователя"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT last_query, last_pid FROM users WHERE user_id = ?",
            (user_id,)
        )
        return await cursor.fetchone()


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
            print(f"Error adding subscription: {e}")
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


async def update_subscription_time(user_id: int, query: str):
    """Обновить время последней отправки"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE subscriptions 
            SET last_sent = CURRENT_TIMESTAMP 
            WHERE user_id = ? AND query = ?
        """, (user_id, query.strip()))
        await db.commit()


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
