# Production Runbook

[README](../README.en.md) · [Русский](PRODUCTION.md) · [Configuration](CONFIGURATION.md) · [Observability](observability.md)

## Preparation

Use Python 3.11+ with [requirements.txt](../requirements.txt), or Docker Compose.
Create `.env` from [.env.example](../.env.example); set `BOT_TOKEN`, `API_USER_ID`,
and `API_KEY`. `ADMIN_USER_IDS` is optional but needed for administration:
replace the example value with your own IDs or leave it empty.
See [access rules](CONFIGURATION.md#accessadmin): administrators and explicitly
allowed chats bypass the remaining restrictions. Keep credentials, DB, backups,
and logs private. Operators must enforce 18+ access.
Run one process per SQLite DB and one polling deployment per Telegram token.

## Windows

```powershell
.\rule34.bat
.\rule34.bat once
```

[rule34.bat](../rule34.bat) changes to the project directory, selects
`.venv\Scripts\python.exe` when available (otherwise `python` from PATH), and runs
`python -m app.main`. Virtualenv activation is not needed for this launcher.

| Exit | Default watchdog | `once` |
| --- | --- | --- |
| `0`, clean shutdown | Exits | Exits |
| `42`, restart/update | Restarts immediately | Exits without restart |
| Unexpected nonzero code | Restarts after 10 seconds | Exits without restart |
| Manual interrupt: `130`, `-1073741510`, `3221225786` | Exits without restart | Exits without restart |

Missing Python or startup-log preparation failures stop the launcher.
[start_hidden.vbs](../start_hidden.vbs) runs `cmd /c rule34.bat` without a window;
see [the short guide](../START_HIDDEN_README.txt).
Use Ctrl+C in the visible console and wait for shutdown when stopping manually.
`/restart` requests shutdown followed by restart, not a permanent watchdog stop.
The hidden launcher has no separate stop command; use a visible process or a
process manager with controlled shutdown for maintenance.
Forced termination can lose queued trace events and leave send outcomes ambiguous.

## Docker

```bash
docker compose up -d --build
docker compose logs -f bot
docker compose stop bot
docker compose start bot
docker compose restart bot
docker compose down
```

[Compose](../docker-compose.yml) binds `./data` to `/app/data`, `./logs` to
`/app/logs`, and overrides `DB_PATH` with `/app/data/bot_data.db`.
The [image](../Dockerfile) uses Python 3.11-slim, non-root `appuser`, and
`python -m app.main`. Ensure writable bind mounts for appuser.
`unless-stopped` restarts after exit until explicitly stopped; unlike the
batch launcher, it can also restart after exit `0`.
`down` preserves host bind directories. Update the checkout and rerun
`up -d --build` for new code. Outbound access is needed to Telegram,
Rule34 API/CDN, and optionally Google Translate.

## SQLite and Single Instance

Default DB is `bot_data.db`; relative `DB_PATH` resolves from the project root.
Connections enable WAL, foreign keys, and a 30000 ms busy timeout.
A DB-specific process lock waits according to `INSTANCE_LOCK_WAIT_SECONDS`
and `INSTANCE_LOCK_RETRY_INTERVAL_SECONDS`. Do not bypass it or remove the
lock file of a running process. This is a single-process bot deployment.

## Backups and Restore

Use SQLite's backup API for a consistent backup, including while the DB is running:

```bash
python scripts/backup_sqlite.py --db bot_data.db --output-dir backups
```

For the Docker host DB, from a host with dependencies installed:

```bash
python scripts/backup_sqlite.py --db data/bot_data.db --output-dir backups
```

The [helper](../scripts/backup_sqlite.py) creates a timestamped `.db`.
Back up private configuration separately; retain logs under your own policy.
Verify backup readability and restore in an isolated copy.
For manual file copying, stop all DB users first; preserve `.db` and existing
`-wal`/`-shm` as one set. Do not copy only the main file during writes.
For restore, stop all processes, save the current set, replace it with the selected
backup, and start one instance. Do not mix restored DB files with old WAL/SHM.

## Updates

Private administrator commands:

- `/version`: commit, branch, date, and local changes.
- `/update_check`: fetch configured remote and compare commits; Git refs change, working files do not.
- `/update`: clean-tree check, fetch, SQLite backup, `git pull --ff-only`,
  requirements install if dependency files changed, compile/import checks, exit `42`.

Remote/branch come from ENV; Telegram cannot supply shell arguments.
Concurrent updates are rejected. There is no automatic rollback: failure after
pull can leave disk code updated. Review the failure stage and backup before restarting.
Exit `42` requires a watchdog/restart policy; `once` does not restart.
Shutdown notification waiting is bounded so a Telegram cooldown cannot indefinitely block the request.
The standard Docker image has no Git and excludes `.git`; rebuild/redeploy instead of using its Telegram updater.
For manual updates, back up, stop, update code/dependencies, run
[checks](DEVELOPMENT.md#локальные-проверки), and start the chosen launcher.

## Logs and Archives

| File in `logs/` | Content / retention |
| --- | --- |
| `info.log` | Operations and heartbeat; 5 MiB, 3 backups |
| `warnings.log` | Warnings; 5 MiB, 3 backups |
| `errors.log` | Errors and tracebacks; 5 MiB, 5 backups |
| `logic_trace.jsonl` | Optional Observability v2 JSONL |
| `logic_trace.jsonl.1`, `.2`, … | ENV size/count rotation; old rotated files cleaned when writer starts |
| `bat_launcher.log` | Launcher history; no built-in rotation |
| `startup_output.log`, `startup_errors.log` | Current batch launch stdout/stderr |
| `startup_output.previous.log`, `startup_errors.previous.log` | Previous launch copies, overwritten on next launch |

To archive, stop bot and launcher gracefully, wait for flush, copy the complete
set to a timestamped directory, and verify counts, sizes, and checksums.
If moving the set, recreate writable `logs/` before starting; do not move open writer files.
Set separate retention for archives and launcher logs; exclude archives from Git.
Trace may contain queries: [privacy and redaction](observability.md#trace-writer-security-and-lifecycle).

## Diagnostics and Recovery

Use `/diag` in a private admin chat for local runtime and a bounded read-only
subscription snapshot. `/diag errors` adds the latest 10 in-memory incidents.
`/health` checks DB and makes a Rule34 API request. Heartbeat runs every five
minutes; `process.heartbeat` contains details when tracing is enabled.
Missing heartbeat warrants checking the process/startup stage, not inferring a specific cause.

For startup failures inspect required ENV, Python/dependencies, current/previous
startup logs and the lock. For delayed subscriptions inspect active/paused, due,
claim, filters, cache, dedup, Telegram queue and full RetryAfter.
For slow SQLite inspect phases and other DB users: await duration does not prove lock wait.
Missing trace can reflect its disabled default, permissions, errors/drops or detail level.
Missing translations can reflect configuration or Google Translate availability;
English tags, blacklist, search and subscriptions remain usable.

Rule34 403 opens a shared breaker with 60 → 120 → 300 → 600 → 900 seconds
of backoff, then at most 900. One half-open probe is admitted after cooldown.
Only valid authenticated post responses from the current generation heal the API;
cache, autocomplete, and stale success do not.
The first 403 alerts immediately, repeats at most once per 900 seconds, and recovery alerts follow API success.
See [events and safe evidence](observability.md#rule34).
Telegram honors the full RetryAfter without an upper cap.
Subscription claims renew every 60 seconds; loss of ownership stops processing,
and claim/access are checked before send. Per-user serialization and DB history
support cross-subscription dedup, but Telegram send and SQLite acknowledgement are not atomic.

## Limitations

- One process per SQLite DB, long polling; no horizontal scaling with this deployment.
- Search/media depends on Rule34 API/CDN; cache fallback does not prove API recovery.
- GIF animation cannot be included in media groups: normal galleries use static previews,
  animation mode sends GIFs separately.
- Digests become due after five posts or six hours and can be requested manually.
  Confirmed items leave the queue, failed remain, ambiguous are deferred.
  Six hours is not a delivery guarantee; timeout does not prove nondelivery or exactly-once.
- ZIP supports static `.jpg`, `.jpeg`, `.png`, `.webp`, bounded by queue/workers,
  timeout, file/part counts, actual download bytes and temporary disk budget.
  Part target is at most 45 MiB. Parts already sent remain after cancellation/failure.
- No built-in age verification; operators control 18+ access.
- Registry/incidents reset on restart; disabled tracing, overflow and abrupt kill
  limit observation completeness. Send success does not prove readership.
