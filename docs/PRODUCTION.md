# Production Runbook

## Recommended Runtime

Use Docker Compose for a single-host deployment:

```bash
docker compose up -d --build
docker compose logs -f bot
```

The compose file keeps the SQLite database in `./data` and logs in `./logs`.
Both directories should be backed up and should not be committed.

## Required Environment

Create `.env` from `.env.example` and set:

- `BOT_TOKEN`
- `API_USER_ID`
- `API_KEY`
- `ADMIN_USER_IDS`

`DB_PATH` is set by `docker-compose.yml` to `/app/data/bot_data.db`.

Optional access controls:

- `ALLOWED_USER_IDS` - comma-separated Telegram user IDs. Empty means all users
  are allowed in private chats.
- `ALLOWED_CHAT_IDS` - comma-separated chat IDs that may use the bot.
- `ALLOW_GROUP_CHATS` - set to `true` only when group usage is intentional.

Admins from `ADMIN_USER_IDS` are always allowed.

## Backups

Prefer the online backup helper:

```bash
python scripts/backup_sqlite.py --db data/bot_data.db --output-dir backups
```

If you copy files manually, stop the container first. At minimum, back up:

- `data/bot_data.db`
- `data/bot_data.db-wal`
- `data/bot_data.db-shm`

## Local Windows Launcher

`rule34.bat` is suitable for local Windows use. It exits on clean shutdown,
restarts immediately for `/restart`, and restarts after 10 seconds on an
unexpected non-zero exit.

## Operational Notes

- Run only one bot process against one SQLite database.
- Rotate and retain `logs/` according to your host policy.
- Keep `.env` and database backups private.
- For public usage, add an explicit adult-content access policy and keep the bot
  restricted to intended chats/users.
- Watch `logs/info.log` for the periodic heartbeat. Missing heartbeat entries
  usually mean the bot process is stopped or stuck before the event loop starts.
