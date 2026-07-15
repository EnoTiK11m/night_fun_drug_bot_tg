# Night Fun Drug Bot TG

[![CI](https://github.com/EnoTiK11m/night_fun_drug_bot_tg/actions/workflows/ci.yml/badge.svg)](https://github.com/EnoTiK11m/night_fun_drug_bot_tg/actions/workflows/ci.yml)
[![Python 3.11](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![RU](https://img.shields.io/badge/lang-RU-0078D4?logo=googletranslate&logoColor=white)](README.md)

A Telegram bot for finding, viewing, and organizing media from
[Rule34](https://rule34.xxx/) by tags. It supports single-post results, albums,
favorites, collections, a blacklist, and automatic subscriptions.

> [!WARNING]
> This project is intended for adults only (18+). Content is provided by a
> third-party service and is not stored in this repository. Each deployment
> operator is responsible for access restrictions and compliance with all
> applicable rules.

## Table of Contents

- [Features](#features)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Docker](#docker)
- [Bot Commands](#bot-commands)
- [Project Structure](#project-structure)
- [Data, Logs, and Backups](#data-logs-and-backups)
- [Development and Verification](#development-and-verification)
- [Limitations](#limitations)
- [Contributing](#contributing)
- [License](#license)

## Features

### Search and Galleries

- search by tags or post ID, plus random results;
- galleries of up to 10 items with page navigation;
- newest, most popular, and random sorting modes;
- filters for rating, media type, orientation, and minimum resolution;
- `auto`, `preview`, `sample`, and `original` quality modes;
- exclusion of posts the user has already viewed;
- tag autocomplete;
- a query builder with required and excluded tags;
- saved search presets with their own filters;
- similar-result searches and paginated quick actions for every post tag;
- Telegram-compatible GIF handling: a static preview is used in albums and
  incompatible media is skipped.

### Personalization

- a compact persistent keyboard for primary actions;
- configurable image captions;
- configurable maximum download size;
- spoilers for all media or only posts rated `explicit`;
- search history and personal statistics;
- English and Russian labels when viewing post tags and the blacklist;
- an explanation of which blacklist tag blocked a post.

### Favorites and Collections

- saving posts to favorites;
- list and gallery views for favorites;
- custom collections;
- notes attached to saved posts;
- a separate “Read later” queue with automatic expiration;
- search by tags and note text;
- ZIP export for all favorites or a single collection.

### Blacklist

- permanent and temporary exclusions;
- adding or removing multiple tags with one command;
- ready-to-use presets;
- list import and export;
- similar-tag lookup through autocomplete.

### Subscriptions and Reliability

- periodic delivery of new posts for saved queries;
- per-subscription filters for rating, type, orientation, resolution, quality,
  and blacklist;
- accumulated digests sent after five posts or at least once every six hours;
- configurable intervals and temporary subscription pauses;
- result caching and duplicate-delivery prevention;
- backoff when no new posts are available;
- a failed-delivery queue with manual administrator retries;
- Telegram rate-limit handling and fallback media URLs;
- local download and upload when Telegram cannot fetch a URL;
- MIME, signature, and image-size validation, plus protection from unsafe
  addresses and redirects.

### Recommendations and Bulk Actions

- recommendations based on frequent favorite tags without sending the profile
  to an external AI service;
- exclusion of unwanted tags from future recommendations;
- saving an entire search gallery to favorites or a new collection;
- creating a preset or subscription directly from gallery results;
- storage usage statistics and cleanup of old history.

## Technology

- Python 3.11+
- [python-telegram-bot](https://python-telegram-bot.org/)
- [aiohttp](https://docs.aiohttp.org/)
- [aiosqlite](https://aiosqlite.omnilib.dev/)
- SQLite with WAL
- optional Docker Compose deployment

Exact Python dependency versions are pinned in
[`requirements.txt`](requirements.txt).

## Quick Start

### 1. Get the source code

```bash
git clone https://github.com/EnoTiK11m/night_fun_drug_bot_tg.git
cd night_fun_drug_bot_tg
```

### 2. Create a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Linux and macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3. Configure the environment

Copy the example configuration:

```powershell
Copy-Item .env.example .env
```

On Linux and macOS:

```bash
cp .env.example .env
```

Set at least the three required variables:

```env
BOT_TOKEN=your_telegram_bot_token
API_USER_ID=your_rule34_api_user_id
API_KEY=your_rule34_api_key
```

Create the Telegram token through [@BotFather](https://t.me/BotFather). Rule34
API credentials are available in the service account settings.

### 4. Start the bot

```bash
python bot.py
```

The SQLite database and required tables are created automatically on first run.

## Configuration

All settings are read from environment variables or `.env`.

| Variable | Required | Default | Purpose |
| --- | :---: | --- | --- |
| `BOT_TOKEN` | yes | — | Telegram bot token |
| `API_USER_ID` | yes | — | Rule34 API user ID |
| `API_KEY` | yes | — | Rule34 API key |
| `SEARCH_COOLDOWN_SECONDS` | no | `3` | Delay between user searches |
| `SUBSCRIPTION_CHECK_INTERVAL_SECONDS` | no | `120` | Subscription scan interval; minimum 30 seconds |
| `SUBSCRIPTION_MAX_POSTS_PER_USER_PASS` | no | `45` | Maximum subscriptions processed per user pass, from 1 to 45 |
| `DB_PATH` | no | `bot_data.db` | SQLite database path |
| `ADMIN_USER_IDS` | no | empty | Comma-separated administrator Telegram user IDs |
| `ALLOWED_USER_IDS` | no | empty | Comma-separated users allowed in private chats |
| `ALLOWED_CHAT_IDS` | no | empty | Comma-separated allowed Telegram chat IDs |
| `ALLOW_GROUP_CHATS` | no | `false` | Allow the bot in group chats |
| `TAG_TRANSLATION_ENABLED` | no | `true` | Translate displayed tags into Russian in the background |

When `ALLOWED_USER_IDS` is empty, all users may access the bot in private chats.
Groups are disabled by default. Users in `ADMIN_USER_IDS` are always allowed.

Do not commit `.env`, databases, logs, or backups. These paths are already
excluded by [`.gitignore`](.gitignore).

## Docker

Create `.env`, then run:

```bash
docker compose up -d --build
docker compose logs -f bot
```

Compose stores persistent data outside the container:

- `./data` — SQLite database;
- `./logs` — application logs.

Stop the deployment with:

```bash
docker compose down
```

Do not run multiple application instances against the same SQLite database.

## Windows Launcher

The project includes:

- [`rule34.bat`](rule34.bat) — starts the bot and restarts it after a failure or
  `/restart` command;
- [`start_hidden.vbs`](start_hidden.vbs) — runs the same launcher without a
  console window;
- [`START_HIDDEN_README.txt`](START_HIDDEN_README.txt) — a short guide to hidden
  startup.

## Bot Commands

### User Commands

| Command | Purpose |
| --- | --- |
| `/start` | Install the persistent keyboard and open the start screen |
| `/search <tags>` | Find one post by tags |
| `/random` | Get a random post |
| `/gallery <tags>` | Get up to 10 items; use `random` for a random gallery |
| `/id <post_id>` | Find a post by ID |
| `/tags <query>` | Find matching tag names |
| `/blacklist` | Open the blacklist menu |
| `/blacklist add <tags>` | Add one or more tags |
| `/blacklist remove <tags>` | Remove one or more tags |
| `/whyblocked <post_id or tags>` | Show blacklist matches |
| `/favorites` | Open favorites |
| `/collections` | Manage favorite collections |
| `/presets` | Manage search presets |
| `/recommendations` | Get recommendations based on favorites |
| `/later` | Open the “Read later” queue |
| `/storage` | Show user storage usage |
| `/history` | Show search history |
| `/stats` | Show personal statistics |
| `/subscriptions` | Manage automatic subscriptions |
| `/settings` | Configure captions, galleries, and media quality |

After the first `/start`, primary actions remain available on the persistent
keyboard without reopening the start menu.

### Administrator Commands

| Command | Purpose |
| --- | --- |
| `/health` | Check Rule34 API, SQLite, background tasks, and free disk space |
| `/adminstats` | Show runtime metrics and database statistics |
| `/retry_failed` | Retry up to 20 failed subscription deliveries |
| `/restart` | Exit with the restart code for an external launcher |

Administrator commands are available only to IDs in `ADMIN_USER_IDS`.
Automatic recovery after `/restart` requires `rule34.bat`, a Docker restart
policy, or another process manager.

## Project Structure

| Path | Responsibility |
| --- | --- |
| [`bot.py`](bot.py) | Telegram handlers, user flows, and background tasks |
| [`api_handler.py`](api_handler.py) | Asynchronous Rule34 API client, pagination, and autocomplete |
| [`database.py`](database.py) | SQLite schema and migrations, settings, cache, subscriptions, and favorites |
| [`bot_media.py`](bot_media.py) | Secure media delivery, fallback URLs, and retries |
| [`bot_delivery.py`](bot_delivery.py) | Delivery rate limiting and Telegram cooldowns |
| [`bot_features.py`](bot_features.py) | Gallery filters, quality selection, and runtime metrics |
| [`bot_keyboards.py`](bot_keyboards.py) | Persistent and inline keyboards |
| [`bot_formatting.py`](bot_formatting.py) | Captions, Markdown, and data formatting |
| [`bot_state.py`](bot_state.py) | Temporary conversation state and callback payloads |
| [`tag_translation.py`](tag_translation.py) | Background tag translation and translation-cache orchestration |
| [`config.py`](config.py) | Configuration loading and validation |
| [`scripts/backup_sqlite.py`](scripts/backup_sqlite.py) | Consistent backup of a running SQLite database |
| [`tests/`](tests) | Unit and integration tests |
| [`docs/PRODUCTION.en.md`](docs/PRODUCTION.en.md) | Short production runbook |

## Data, Logs, and Backups

By default, the database is stored in `bot_data.db`. The application enables
WAL, foreign keys, and a busy timeout for safe concurrent work within one
process.

Create a consistent backup of a local database with:

```bash
python scripts/backup_sqlite.py --db bot_data.db --output-dir backups
```

For Docker:

```bash
python scripts/backup_sqlite.py --db data/bot_data.db --output-dir backups
```

Main log files are stored in `logs/`:

- `info.log` — operational events and heartbeat messages;
- `warnings.log` — warnings;
- `errors.log` — errors and tracebacks;
- `startup_output.log` and `startup_errors.log` — Windows launcher output
  produced before logging is configured.

The application rotates operational logs. See
[`docs/PRODUCTION.en.md`](docs/PRODUCTION.en.md) for deployment and maintenance
recommendations.

Tag translations are cached in SQLite and populated gradually by a background
task. For missing translations, the bot sends only tag names to the external
Google Translate service; search queries, user IDs, and media are not shared.

## Development and Verification

Install and verify dependencies:

```bash
python -m pip install -r requirements.txt
python -m pip check
```

Check syntax and run tests:

```bash
python -m compileall -q bot.py api_handler.py database.py config.py bot_delivery.py bot_features.py bot_formatting.py bot_keyboards.py bot_media.py bot_state.py tag_translation.py
python -m unittest discover -s tests -v
```

GitHub Actions runs the same checks on pushes and pull requests.

## Limitations

- The bot uses long polling and expects one active process per SQLite database.
- Search and file availability depend on the Rule34 API and its CDN.
- Telegram does not support GIF animations inside media groups. Regular
  galleries use a static preview; animation mode sends GIFs individually.
- A digest is sent after five posts accumulate or after six hours and can also
  be requested manually from the subscription menu.
- ZIP exports include only supported static image formats and are constrained
  by Telegram file-size limits.
- The project has no built-in age verification; deployment operators must
  control access.

## Contributing

1. Create a separate branch.
2. Make the changes and add tests.
3. Run the local checks.
4. Open a pull request describing the behavior and verification steps.

Report bugs and feature requests through
[GitHub Issues](https://github.com/EnoTiK11m/night_fun_drug_bot_tg/issues).

## License

This project is distributed under the MIT License. See [`LICENSE`](LICENSE) for
the full text.

Author: [EnoTiK11m](https://github.com/EnoTiK11m).
