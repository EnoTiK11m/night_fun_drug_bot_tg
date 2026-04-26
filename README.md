# Night Fun Drug Bot TG

Telegram-бот для поиска медиа по тегам через Rule34 API. Поддерживает обычный поиск, повторную выдачу без дублей, подписки, blacklist, историю, избранное, настройки подписи и устойчивую отправку медиа с fallback-ссылками.

## Возможности

- Поиск постов по тегам.
- Кнопка "Ещё" для продолжения текущего поиска.
- Исключение уже отправленных `post_id` для пользователя.
- Пользовательский blacklist тегов.
- История последних поисков.
- Избранное с постраничным списком и галереей.
- Подписки на поисковые запросы с настраиваемым интервалом.
- Локальный SQLite-кэш постов и подписок.
- Atomic claim для фоновой обработки подписок, чтобы не ловить дубли.
- WAL, `busy_timeout` и `foreign_keys` для SQLite.
- Отправка `file_url -> sample_url -> preview_url`, если Telegram не принимает исходный файл.
- Отдельные файлы логов для `INFO` и `WARNING+`.
- Тесты для БД, подписок, Markdown escaping, медиа fallback и callback flow.

## Стек

- Python 3.11+
- `python-telegram-bot`
- `aiohttp`
- `aiosqlite`
- `python-dotenv`
- SQLite

## Установка

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Для Linux/macOS активация окружения:

```bash
source .venv/bin/activate
```

## Настройка

Создайте `.env` рядом с `bot.py` по примеру `.env.example`:

```env
BOT_TOKEN=your_telegram_bot_token
API_USER_ID=your_rule34_api_user_id
API_KEY=your_rule34_api_key
SEARCH_COOLDOWN_SECONDS=3
DB_PATH=bot_data.db
```

Обязательные переменные:

- `BOT_TOKEN` - токен Telegram-бота.
- `API_USER_ID` - Rule34 API user id.
- `API_KEY` - Rule34 API key.

Опциональные:

- `SEARCH_COOLDOWN_SECONDS` - задержка между обычными поисками пользователя.
- `DB_PATH` - путь к SQLite базе.

## Запуск

```bash
python bot.py
```

На Windows можно использовать:

```bat
rule34.bat
```

## Команды бота

- `/start` - главное меню.
- `/search <tags>` - поиск по тегам.
- `/blacklist` - управление blacklist.
- `/subscriptions` - управление подписками.
- `/history` - история поисков.
- `/favorites` - избранное.
- `/settings` - настройки подписи под медиа.
- `/tags <query>` - автодополнение тегов.
- `/id <post_id>` - открыть пост по ID.

## Как работают подписки

Подписка хранит поисковый запрос и интервал проверки. Фоновая задача периодически выбирает due-подписки, атомарно забирает их через `processing_token`, обрабатывает и освобождает claim.

Для подписок используется локальный пул постов:

1. Бот запрашивает до 1000 постов из Rule34 API.
2. Новые посты добавляются в `subscription_cache`, старые не удаляются.
3. Перед отправкой бот вычитает уже отправленные `sent_posts`.
4. Если API временно не отвечает, бот может использовать старый кэш.

Лог обновления кэша выглядит так:

```text
Refreshed subscription cache user=... query='tag' api=1000 new=37 total=1462 available=912
```

Значения:

- `api` - сколько валидных постов пришло из API за обновление.
- `new` - сколько из них новых для локального пула.
- `total` - сколько всего постов теперь в пуле подписки.
- `available` - сколько осталось доступно к отправке после вычитания `sent_posts`.

## SQLite

База создаётся автоматически при старте. По умолчанию используется `bot_data.db`.

Основные таблицы:

- `users`
- `blacklist`
- `subscriptions`
- `search_history`
- `favorites`
- `sent_posts`
- `subscription_posts`
- `subscription_cache`
- `post_cache`
- `user_settings`

Локальные файлы базы и WAL/SHM не должны попадать в Git.

## Логи

Логи пишутся в папку `logs/`:

- `logs/info.log` - только `INFO`.
- `logs/warnings.log` - `WARNING`, `ERROR`, `CRITICAL`.

Токен Telegram маскируется formatter-ом логов.

## Тесты

Проверка синтаксиса:

```bash
python -m py_compile bot.py database.py api_handler.py config.py
```

Полный прогон тестов:

```bash
python -m unittest discover -s tests -v
```

Тесты используют временную SQLite-базу там, где проверяется реальная БД.

## Что не коммитить

Не добавляйте в репозиторий:

- `.env`
- `bot_data.db`
- `bot_data.db-wal`
- `bot_data.db-shm`
- `logs/`
- `.venv/`
- `__pycache__/`

## Безопасность

Никогда не публикуйте реальные значения:

- Telegram bot token.
- Rule34 API user id.
- Rule34 API key.
- Локальную SQLite-базу с пользовательскими данными.
