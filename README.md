# Night Fun Drug Bot TG

Telegram-бот для поиска и просмотра медиа через Rule34 API. Поддерживает
поиск по тегам, случайную выдачу, черный список, избранное, историю и
периодические подписки.

Проект предназначен только для совершеннолетних пользователей. Контент
загружается со стороннего сервиса; автор проекта его не размещает и не
контролирует.

## Возможности

- поиск по тегам и ID поста;
- случайная выдача без повторов;
- личный черный список тегов;
- избранное, галерея и экспорт изображений в ZIP;
- подписки на запросы с настраиваемым интервалом и паузой;
- резервные ссылки на медиа: original, sample и preview;
- ограничение доступа по ID пользователей и чатов;
- локальная SQLite-база, кэш и защита от повторной отправки;
- раздельные ротируемые логи и автоматический перезапуск на Windows.

## Требования

- Python 3.11 или новее;
- токен Telegram-бота от BotFather;
- `user_id` и API-ключ Rule34;
- SQLite входит в стандартную поставку Python.

Основные зависимости: `python-telegram-bot`, `aiohttp`, `aiosqlite` и
`python-dotenv`. Точные версии указаны в `requirements.txt`.

## Установка

Получите исходники и создайте виртуальное окружение:

```bash
git clone https://github.com/EnoTiK11m/night_fun_drug_bot_tg.git
cd night_fun_drug_bot_tg
python -m venv .venv
```

Windows:

```bat
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

Linux и macOS:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Для обновления существующей копии:

```bash
git pull --ff-only
python -m pip install -r requirements.txt
```

## Настройка

Скопируйте `.env.example` в `.env` и заполните секреты:

```env
BOT_TOKEN=your_telegram_bot_token
API_USER_ID=your_rule34_api_user_id
API_KEY=your_rule34_api_key
ADMIN_USER_IDS=123456789
```

Обязательные параметры:

- `BOT_TOKEN` — токен Telegram-бота;
- `API_USER_ID` — ID пользователя Rule34;
- `API_KEY` — API-ключ Rule34.

Дополнительные параметры:

- `SEARCH_COOLDOWN_SECONDS` — пауза между поисками, по умолчанию 3 с;
- `SUBSCRIPTION_CHECK_INTERVAL_SECONDS` — период проверки подписок,
  минимум 30 с, по умолчанию 120 с;
- `SUBSCRIPTION_MAX_POSTS_PER_USER_PASS` — максимум отправок за проход,
  от 1 до 45;
- `DB_PATH` — путь к SQLite-базе, по умолчанию `bot_data.db`;
- `ADMIN_USER_IDS` — ID администраторов через запятую;
- `ALLOWED_USER_IDS` — разрешенные личные пользователи через запятую;
- `ALLOWED_CHAT_IDS` — разрешенные чаты через запятую;
- `ALLOW_GROUP_CHATS` — разрешить группы: `true` или `false`.

Пустой `ALLOWED_USER_IDS` разрешает все личные чаты. Группы по умолчанию
запрещены. Администраторы всегда получают доступ и могут выполнить
`/restart`.

## Запуск

Обычный запуск:

```bash
python bot.py
```

На Windows `rule34.bat` автоматически перезапускает бот после сбоя.
`start_hidden.vbs` запускает тот же сценарий без окна консоли.

Запуск в Docker:

```bash
docker compose up -d --build
docker compose logs -f bot
```

Docker хранит базу в `data/`, а логи — в `logs/`. Не запускайте два
экземпляра бота с одной SQLite-базой.

## Команды бота

- `/start` — открыть главное меню;
- `/search <tags>` — найти пост по тегам;
- `/random` — показать случайный пост;
- `/id <post_id>` — открыть пост по ID;
- `/tags <query>` — найти подходящие теги;
- `/blacklist` — настроить черный список;
- `/favorites` — открыть избранное;
- `/history` — показать историю поиска;
- `/subscriptions` — управлять подписками;
- `/settings` — изменить настройки;
- `/restart` — перезапустить бот, доступно только администратору.

## Структура проекта

- `bot.py` — запуск, обработчики Telegram и фоновые задачи;
- `api_handler.py` — клиент Rule34 API, пагинация и тайм-ауты;
- `database.py` — схема SQLite, кэш, избранное и подписки;
- `bot_delivery.py` — ограничение скорости отправки в Telegram;
- `bot_media.py` — отправка медиа, повторы и резервные URL;
- `bot_formatting.py` — Markdown, подписи, интервалы и страницы;
- `bot_keyboards.py` — inline-клавиатуры и меню;
- `bot_state.py` — временное состояние и callback-данные;
- `config.py` — чтение и проверка переменных окружения;
- `scripts/backup_sqlite.py` — согласованная резервная копия базы;
- `tests/` — модульные и интеграционные тесты;
- `docs/PRODUCTION.md` — памятка по эксплуатации.

## Данные, логи и резервные копии

База создается автоматически. Для резервного копирования работающей базы:

```bash
python scripts/backup_sqlite.py --db bot_data.db --output-dir backups
```

Логи находятся в `logs/`:

- `info.log` — сообщения `INFO`;
- `warnings.log` — только `WARNING`;
- `errors.log` — `ERROR`, `CRITICAL` и traceback;
- `startup_output.log` и `startup_errors.log` — сырой вывод запуска.

`info.log` и `warnings.log` хранят по три архива размером до 5 МБ.
`errors.log` хранит пять архивов. Секреты маскируются форматтером.

## Проверка

```bash
python -m py_compile bot.py database.py api_handler.py config.py
python -m unittest discover -s tests -v
```

Те же проверки запускаются через GitHub Actions при push и pull request.

## Документация и поддержка

Инструкция по эксплуатации находится в
[`docs/PRODUCTION.md`](docs/PRODUCTION.md). Актуальные исходники и история
изменений доступны в
[репозитории GitHub](https://github.com/EnoTiK11m/night_fun_drug_bot_tg).

Об ошибках и пожеланиях сообщайте через
[GitHub Issues](https://github.com/EnoTiK11m/night_fun_drug_bot_tg/issues).
Исправления отправляйте отдельной веткой через pull request. Перед отправкой
запустите тесты и не включайте в коммит `.env`, базы и логи.

## Автор и лицензия

Разработчик: [EnoTiK11m](https://github.com/EnoTiK11m).

Copyright (c) 2026 EnoTiK11m. Проект распространяется по лицензии MIT.
Полный текст находится в файле [`LICENSE`](LICENSE).
