# Host auto-update (Docker deployment)

The bot's Telegram `/update` command requires Git inside the running process and
does not work in the production Docker image. On LXC 122, use the host updater
instead. It checks GitHub every 15 minutes via a systemd timer.

Before enabling the timer, install Docker, Git, Python 3 and GitHub CLI on the
host; authenticate `gh` for an account allowed to create Issues in
`EnoTiK11m/night_fun_drug_bot_tg`. The service reads that account's
`/home/codex/.config/gh` configuration. Keep it private.

Install from this repository on the LXC:

```sh
sudo install -m 755 scripts/host_update.py /opt/night_fun_drug_bot/scripts/host_update.py
sudo install -m 644 deploy/night-fun-drug-bot-update.service /etc/systemd/system/
sudo install -m 644 deploy/night-fun-drug-bot-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now night-fun-drug-bot-update.timer
```

The updater clones `main` to a temporary directory, builds a candidate Docker
image, runs compile and import checks, and creates a verified SQLite backup
while the old bot is still running. It then replaces the Compose containers.
If startup or the database health check fails, it recreates both services from
their previous image IDs. It creates a GitHub Issue describing the failed stage
and rollback result. If GitHub is unavailable, it keeps the report in
`logs/pending_update_issue.txt` and retries the submission next time.

Inspect a run with `sudo journalctl -u night-fun-drug-bot-update.service -n 100`.
Run once with `sudo systemctl start night-fun-drug-bot-update.service`.
The current deployed revision is recorded in `logs/deployed_commit.json`.
