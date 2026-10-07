# Night Fun Drug Bot TG

[![CI](https://github.com/EnoTiK11m/night_fun_drug_bot_tg/actions/workflows/ci.yml/badge.svg)](https://github.com/EnoTiK11m/night_fun_drug_bot_tg/actions/workflows/ci.yml)
[![Python 3.11](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![ENG](https://img.shields.io/badge/lang-ENG-0078D4?logo=googletranslate&logoColor=white)](README.en.md)

Telegram-бот для поиска, просмотра и организации медиа из [Rule34](https://rule34.xxx/) по тегам.
Одиночная выдача и галереи объединены с личной библиотекой, фильтрами и подписками.
Простой режим интерфейса оставляет основные действия, расширенный открывает дополнительные инструменты.

> [!WARNING]
> Только для совершеннолетних пользователей (18+). Контент поступает из стороннего сервиса и не хранится в репозитории.
> Встроенной проверки возраста нет; оператор отвечает за ограничение доступа и соблюдение применимых правил.

## Возможности

- Поиск по тегам и ID, случайные посты, галереи до 10 элементов.
- Фильтры, сортировка, выбор качества и исключение просмотренных постов.
- Избранное, коллекции, заметки, «Посмотреть позже» и ZIP-экспорт.
- Чёрный список, автодополнение, сохранённые запросы и рекомендации по избранному.
- Подписки с индивидуальными фильтрами, паузой, дайджестом и защитой от повторной доставки.
- Резервные media URL, скачивание и загрузка файла, доставка ссылки при недоступном медиа.
- Общие лимитеры Rule34 и Telegram, circuit breaker для Rule34 HTTP 403.
- Административная диагностика, heartbeat и Observability v2.

Подробности интерфейса и функций: [справочник команд](docs/COMMANDS.md).

## Быстрый запуск

Нужен Python 3.11+. Версии зависимостей закреплены в [requirements.txt](requirements.txt).

### 1. Получите проект

```bash
git clone https://github.com/EnoTiK11m/night_fun_drug_bot_tg.git
cd night_fun_drug_bot_tg
```

### 2. Подготовьте окружение

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

### 3. Заполните `.env` и запустите бот

После настройки переменных ниже:

```bash
python -m app.main
```

SQLite-база и таблицы создаются автоматически при первом запуске.
На одну базу должен работать один процесс бота.

### Windows launcher

Watchdog использует Python из `.venv`, если окружение существует:

```powershell
.\rule34.bat
```

Для одного запуска без автоматического перезапуска:

```powershell
.\rule34.bat once
```

Скрытый запуск, коды завершения и логи: [Production](docs/PRODUCTION.md#windows).

### Docker

После создания `.env`:

```bash
docker compose up -d --build
```

База хранится в `./data`, логи — в `./logs`.
[Развёртывание и обслуживание Docker](docs/PRODUCTION.md#docker).

## Минимальная конфигурация

Три обязательные переменные из [.env.example](.env.example):

```env
BOT_TOKEN=your_telegram_bot_token
API_USER_ID=your_rule34_api_user_id
API_KEY=your_rule34_api_key
```

Токен Telegram создаётся через [@BotFather](https://t.me/BotFather), данные Rule34 API — в настройках аккаунта сервиса.
`ADMIN_USER_IDS` необязателен: укажите свои ID для административных команд или оставьте пустым.
Замените демонстрационное значение администратора в `.env.example` перед запуском.
По умолчанию личные чаты открыты всем, группы запрещены.
Все параметры, диапазоны и правила доступа: [Configuration](docs/CONFIGURATION.md).
`.env`, базы, логи и backups исключены из Git через [.gitignore](.gitignore).

## Основные команды

| Команда | Назначение |
| --- | --- |
| `/search <tags>` | Найти пост по тегам |
| `/random` | Случайный пост |
| `/gallery <tags>` | Галерея; `random` — случайная |
| `/subscriptions` | Подписки, пауза и дайджест |
| `/favorites` | Избранное и библиотека |
| `/settings` | Интерфейс, фильтры и качество |
| `/health` | Администратор: DB, задачи, диск и проверка Rule34 API |
| `/diag`, `/diag errors` | Администратор в личном чате: состояние и последние инциденты |

Все команды, права и действия меню: [Commands](docs/COMMANDS.md).

## Надёжность и диагностика

SQLite работает с WAL; межпроцессный lock защищает базу от второго экземпляра.
Rule34 limiter учитывает физические запросы и повторы, а HTTP 403 открывает общий circuit breaker.
Telegram limiter соблюдает полный `RetryAfter`.
Подписки продлевают claims и проверяют дедупликацию между подписками пользователя.

Observability v2 связывает операции и попытки, показывает восстановление, DB-фазы и состояние writer.
Дополнительный trace `logs/logic_trace.jsonl` выключен по умолчанию; `/diag` доступен и без него.
[Диагностика и ограничения наблюдений](docs/observability.md).

## Документация

| Раздел | Документ |
| --- | --- |
| Переменные окружения и доступ | [Configuration](docs/CONFIGURATION.md) |
| Команды, интерфейс и функции | [Commands](docs/COMMANDS.md) |
| Windows, Docker, backups и восстановление | [Production](docs/PRODUCTION.md) · [English](docs/PRODUCTION.en.md) |
| Trace, heartbeat, `/diag` и ошибки | [Observability](docs/observability.md) |
| Компоненты и контракты | [Architecture](docs/ARCHITECTURE.md) |
| Проверки, CI и contribution flow | [Development](docs/DEVELOPMENT.md) |

Эксплуатационные ограничения, GIF, ZIP и семантика дайджеста: [Production](docs/PRODUCTION.md#ограничения).

## Разработка

Из корня проекта с установленными зависимостями:

```bash
python -m compileall -q app bot.py scripts tests
python scripts/check_imports.py
python tests/run_isolated_suite.py
```

CI выполняет эти проверки и `python -m pip check`.
Подготовка окружения, изоляция тестов и участие в разработке: [Development](docs/DEVELOPMENT.md).

## License

MIT — [LICENSE](LICENSE). Автор: [EnoTiK11m](https://github.com/EnoTiK11m).
