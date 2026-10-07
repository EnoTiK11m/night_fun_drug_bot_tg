# Night Fun Drug Bot TG

[![CI](https://github.com/EnoTiK11m/night_fun_drug_bot_tg/actions/workflows/ci.yml/badge.svg)](https://github.com/EnoTiK11m/night_fun_drug_bot_tg/actions/workflows/ci.yml)
[![Python 3.11](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![RU](https://img.shields.io/badge/lang-RU-0078D4?logo=googletranslate&logoColor=white)](README.md)

A Telegram bot for finding, viewing, and organizing media from [Rule34](https://rule34.xxx/) by tags.
Single posts and galleries share a personal library, filters, and subscriptions.
Simple interface mode keeps primary actions visible; advanced mode exposes more tools.

> [!WARNING]
> Adults only (18+). Content comes from an external service and is not stored in this repository.
> There is no built-in age verification. Operators are responsible for access restrictions and applicable rules.

## Features

- Tag and post-ID search, random posts, galleries of up to 10 items.
- Filters, sorting, media quality selection, and exclusion of viewed posts.
- Favorites, collections, notes, read-later queue, and ZIP export.
- Blacklist, autocomplete, saved queries, and recommendations based on favorites.
- Subscriptions with individual filters, pauses, digests, and delivery deduplication.
- Alternative media URLs, local download/upload, and link delivery when media is unavailable.
- Shared Rule34 and Telegram limiters, plus a Rule34 HTTP 403 circuit breaker.
- Administrator diagnostics, heartbeat, and Observability v2.

See the [command and feature reference](docs/COMMANDS.md) for interface details.

## Quick Start

Python 3.11+ is required. Dependencies are pinned in [requirements.txt](requirements.txt).

### 1. Get the project

```bash
git clone https://github.com/EnoTiK11m/night_fun_drug_bot_tg.git
cd night_fun_drug_bot_tg
```

### 2. Prepare the environment

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

### 3. Fill in `.env` and start the bot

After configuring the variables below:

```bash
python -m app.main
```

The SQLite database and tables are created automatically on first run.
Run one bot process per database.

### Windows Launcher

The watchdog uses Python from `.venv` when available:

```powershell
.\rule34.bat
```

For a single run without automatic restart:

```powershell
.\rule34.bat once
```

Hidden startup, exit codes, and logs: [Production](docs/PRODUCTION.en.md#windows).

### Docker

After creating `.env`:

```bash
docker compose up -d --build
```

The database is stored in `./data`, logs in `./logs`.
[Docker deployment and maintenance](docs/PRODUCTION.en.md#docker).

## Minimal Configuration

Three required variables from [.env.example](.env.example):

```env
BOT_TOKEN=your_telegram_bot_token
API_USER_ID=your_rule34_api_user_id
API_KEY=your_rule34_api_key
```

Create the Telegram token through [@BotFather](https://t.me/BotFather); Rule34 API credentials are in the service account settings.
`ADMIN_USER_IDS` is optional: set your IDs to enable administrator commands or leave it empty.
Replace the demonstration administrator value from `.env.example` before starting.
Private chats are open to everyone by default; groups are disabled.
All settings, validation, and access rules: [Configuration](docs/CONFIGURATION.md).
`.env`, databases, logs, and backups are excluded by [.gitignore](.gitignore).

## Main Commands

| Command | Purpose |
| --- | --- |
| `/search <tags>` | Find a post by tags |
| `/random` | Random post |
| `/gallery <tags>` | Gallery; `random` selects a random gallery |
| `/subscriptions` | Subscriptions, pauses, and digests |
| `/favorites` | Favorites and library |
| `/settings` | Interface, filters, and quality |
| `/health` | Administrator: DB, tasks, disk, and a Rule34 API check |
| `/diag`, `/diag errors` | Private administrator chat: local state and recent incidents |

All commands, permissions, and menu actions: [Commands](docs/COMMANDS.md).

## Reliability and Diagnostics

SQLite uses WAL; a process lock protects the database from a second instance.
The Rule34 limiter counts physical requests and retries; HTTP 403 opens a shared circuit breaker.
The Telegram limiter respects the full `RetryAfter` value.
Subscriptions renew claims and check delivery deduplication across each user's subscriptions.

Observability v2 correlates operations and attempts, recovery, DB phases, and writer health.
Optional tracing to `logs/logic_trace.jsonl` is disabled by default; `/diag` also works without tracing.
[Diagnostics and observation limits](docs/observability.md).

## Documentation

| Topic | Document |
| --- | --- |
| Environment and access | [Configuration](docs/CONFIGURATION.md) |
| Commands, interface, and features | [Commands](docs/COMMANDS.md) |
| Windows, Docker, backups, and recovery | [Production](docs/PRODUCTION.en.md) · [Русский](docs/PRODUCTION.md) |
| Trace, heartbeat, `/diag`, and errors | [Observability](docs/observability.md) |
| Components and contracts | [Architecture](docs/ARCHITECTURE.md) |
| Checks, CI, and contributions | [Development](docs/DEVELOPMENT.md) |

The detailed references are in Russian; the production runbook is available in both languages.
Operational limits, GIFs, ZIPs, and digest semantics: [Production](docs/PRODUCTION.en.md#limitations).

## Development

From the project root with dependencies installed:

```bash
python -m compileall -q app bot.py scripts tests
python scripts/check_imports.py
python tests/run_isolated_suite.py
```

CI runs these checks and `python -m pip check`.
Environment setup, isolated tests, and contribution flow: [Development](docs/DEVELOPMENT.md).

## License

MIT — [LICENSE](LICENSE). Author: [EnoTiK11m](https://github.com/EnoTiK11m).
