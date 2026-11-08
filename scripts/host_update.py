"""Safely deploy a public GitHub revision to the host's Docker Compose stack.

Run on the Docker host (not in the bot container). Keep this script and the
systemd units outside the image so a broken candidate cannot replace them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY = "EnoTiK11m/night_fun_drug_bot_tg"
SOURCE_URL = f"https://github.com/{REPOSITORY}.git"
PROJECT = Path(os.environ.get("NIGHT_BOT_PROJECT", "/opt/night_fun_drug_bot"))
STATE = PROJECT / "logs" / "deployed_commit.json"
PENDING = PROJECT / "logs" / "pending_update_issue.txt"


def run(*args: str, cwd: Path | None = None, timeout: int = 300) -> str:
    result = subprocess.run(
        args, cwd=cwd, text=True, capture_output=True, timeout=timeout,
        check=False,
    )
    if result.returncode:
        # Command output may contain credentials or user data.
        raise RuntimeError(f"{args[0]} {args[1] if len(args) > 1 else ''} exited {result.returncode}")
    return result.stdout.strip()


def compose(*args: str, override: Path | None = None, timeout: int = 300) -> str:
    command = ["docker", "compose", "-f", str(PROJECT / "docker-compose.yml")]
    if override:
        command += ["-f", str(override)]
    return run(*command, *args, cwd=PROJECT, timeout=timeout)


def container_image(service: str) -> str:
    container = compose("ps", "-q", service)
    if not container:
        raise RuntimeError(f"{service} container is missing")
    return run("docker", "inspect", "-f", "{{.Image}}", container)


def image_override(path: Path, image: str) -> None:
    # A validated Docker image reference contains no YAML control characters.
    if not image or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:._-/@" for ch in image):
        raise ValueError("invalid image reference")
    path.write_text(
        f'services:\n  bot:\n    image: "{image}"\n  backup:\n    image: "{image}"\n',
        encoding="utf-8",
    )


def healthy() -> bool:
    for service in ("bot", "backup"):
        container = compose("ps", "-q", service)
        if not container:
            return False
        state = run("docker", "inspect", "-f", "{{.State.Running}} {{.State.Restarting}}", container)
        if state != "true false":
            return False
    # Confirm the new bot can read the mounted database and its schema.
    result = compose(
        "exec", "-T", "bot", "python", "-c",
        "import sqlite3; from app.config import DB_PATH; c=sqlite3.connect('file:'+DB_PATH+'?mode=ro',uri=True); assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; c.execute('SELECT COUNT(*) FROM subscriptions').fetchone(); c.close()",
        timeout=60,
    )
    return result == ""


def report_failure(commit: str, stage: str, rollback: str) -> None:
    body = (
        f"Automatic deployment of `{commit[:12]}` failed on LXC 122.\n\n"
        f"Stage: `{stage}`\nRollback: {rollback}\n"
        "The updater kept or restored the previous Docker image. "
        "Inspect the host journal and deployment artifacts for details.\n"
    )
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    PENDING.write_text(body, encoding="utf-8")
    try:
        run(
            "gh", "issue", "create", "-R", REPOSITORY,
            "--title", f"Auto-update failed: {commit[:12]} ({stage})",
            "--body-file", str(PENDING), timeout=60,
        )
    except Exception:
        print(f"GitHub Issue submission failed; report saved at {PENDING}", file=sys.stderr)
    else:
        PENDING.unlink(missing_ok=True)


def deploy() -> None:
    if PENDING.exists():
        try:
            run("gh", "issue", "create", "-R", REPOSITORY,
                "--title", "Previous auto-update failure", "--body-file", str(PENDING), timeout=60)
            PENDING.unlink()
        except Exception:
            pass
    old_bot = container_image("bot")
    old_backup = container_image("backup")
    old_commit = ""
    if STATE.exists():
        old_commit = str(json.loads(STATE.read_text(encoding="utf-8")).get("commit", ""))
    commit = "unknown"
    stage = "clone"
    attempted_switch = False
    rollback_status = "old containers remained running"
    with tempfile.TemporaryDirectory(prefix="night-bot-update-") as temp:
        root = Path(temp)
        source = root / "source"
        candidate_override = root / "candidate.yml"
        rollback_override = root / "rollback.yml"
        try:
            run("git", "clone", "--depth", "1", "--branch", "main", SOURCE_URL, str(source), timeout=180)
            commit = run("git", "rev-parse", "HEAD", cwd=source)
            if commit == old_commit and healthy():
                return
            image = f"night-fun-drug-bot:candidate-{commit[:12]}"
            stage = "build"
            run("docker", "build", "-t", image, str(source), timeout=900)
            stage = "preflight"
            run("docker", "run", "--rm", "--entrypoint", "python", image,
                "-m", "compileall", "-q", "app", "bot.py", "scripts", timeout=120)
            run("docker", "run", "--rm", "--entrypoint", "python", image,
                "scripts/check_imports.py", timeout=120)
            stage = "database_backup"
            compose("exec", "-T", "backup", "python", "scripts/backup_sqlite.py",
                    "--output-dir", "/app/backups", "--verify-restore", "--keep", "7", timeout=180)
            image_override(candidate_override, image)
            stage = "switch"
            attempted_switch = True
            compose("up", "-d", "--no-build", "--force-recreate", "bot", "backup",
                    override=candidate_override, timeout=180)
            stage = "health"
            time.sleep(30)
            if not healthy():
                raise RuntimeError("candidate containers failed health check")
            # The host timer must pick up future updater fixes as well.
            source_script = source / "scripts" / "host_update.py"
            target_script = PROJECT / "scripts" / "host_update.py"
            if source_script.is_file() and target_script.parent.is_dir():
                replacement = target_script.with_suffix(".py.new")
                shutil.copy2(source_script, replacement)
                os.replace(replacement, target_script)
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps({"commit": commit, "image": image}) + "\n", encoding="utf-8")
            print(f"Deployed {commit[:12]}")
        except Exception:
            if attempted_switch:
                stage_at_failure = stage
                try:
                    # Preserve each old image independently if they differed.
                    image_override(rollback_override, old_bot)
                    if old_backup != old_bot:
                        content = rollback_override.read_text(encoding="utf-8")
                        rollback_override.write_text(
                            content.replace(f'backup:\n    image: "{old_bot}"',
                                            f'backup:\n    image: "{old_backup}"'), encoding="utf-8")
                    compose("up", "-d", "--no-build", "--force-recreate", "bot", "backup",
                            override=rollback_override, timeout=180)
                    time.sleep(10)
                    rollback_status = "previous images restarted" if healthy() else "rollback attempted; health check failed"
                except Exception:
                    rollback_status = "rollback command failed; manual intervention required"
                stage = stage_at_failure
            report_failure(commit, stage, rollback_status)
            raise


if __name__ == "__main__":
    try:
        deploy()
    except Exception as exc:
        print(f"Update failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
