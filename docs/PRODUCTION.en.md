# Production Runbook

[![RU](https://img.shields.io/badge/lang-RU-0078D4?logo=googletranslate&logoColor=white)](PRODUCTION.md)

## Recommended Runtime

Use Docker Compose for a single-host deployment:

```bash
docker compose up -d --build
docker compose logs -f bot
```

The Compose configuration stores the SQLite database in `./data` and
application logs in `./logs`. Both directories should be backed up and must not
be committed to Git.

## Required Environment Variables

Create `.env` from `.env.example` and set:

- `BOT_TOKEN`;
- `API_USER_ID`;
- `API_KEY`;
- `ADMIN_USER_IDS`.

`DB_PATH` is set by `docker-compose.yml` to `/app/data/bot_data.db`.

Optional access controls:

- `ALLOWED_USER_IDS` — comma-separated Telegram user IDs. An empty value allows
  all users to access the bot in private chats;
- `ALLOWED_CHAT_IDS` — comma-separated chat IDs in which the bot may operate;
- `ALLOW_GROUP_CHATS` — set to `true` only when group usage is intentional.

Administrators listed in `ADMIN_USER_IDS` are always allowed.

## Backups

Use the helper script to create a consistent backup of a running database:

```bash
python scripts/backup_sqlite.py --db data/bot_data.db --output-dir backups
```

If files are copied manually, stop the container first. Back up at least:

- `data/bot_data.db`;
- `data/bot_data.db-wal`;
- `data/bot_data.db-shm`.

## Local Windows Launcher

Use `rule34.bat` for local Windows operation. It exits after a clean shutdown,
restarts the bot immediately after `/restart`, and starts it again after 10
seconds when the process exits unexpectedly with a non-zero code.

## Operational Recommendations

- Run only one bot process against one SQLite database.
- Rotate and retain `logs/` according to the host policy.
- Keep `.env` and database backups private.
- For public deployments, state the adult-content restriction explicitly and
  limit access to the intended users and chats.
- Watch `logs/info.log` for periodic heartbeat entries. Missing heartbeats
  usually mean that the bot process has stopped or is stuck before the event
  loop starts.
- Tag translation requires outbound HTTPS access to Google Translate. Set
  `TAG_TRANSLATION_ENABLED=false` to disable it; stored English tags, the
  blacklist, search, and subscriptions will continue to work.
