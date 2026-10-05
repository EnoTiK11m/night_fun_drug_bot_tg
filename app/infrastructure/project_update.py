"""Single-process, admin-triggered fast-forward project updates."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app.config import (
    DB_PATH,
    GIT_UPDATE_BRANCH,
    GIT_UPDATE_COMMAND_TIMEOUT_SECONDS,
    GIT_UPDATE_PIP_TIMEOUT_SECONDS,
    GIT_UPDATE_REMOTE,
)
from scripts.backup_sqlite import backup_database


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
UPDATE_MARKER_PATH = PROJECT_ROOT / "logs" / "update_restart.json"
COMMAND_OUTPUT_LIMIT = 8 * 1024
DEPENDENCY_FILES = (
    "requirements.txt",
    "pyproject.toml",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
    "setup.py",
    "setup.cfg",
)
SAFE_GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
update_operation_lock = asyncio.Lock()


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class VersionInfo:
    commit: str
    branch: str
    commit_date: str
    dirty: bool


@dataclass(frozen=True)
class UpdateCheckResult:
    current_commit: str
    remote_commit: str
    commits_behind: int


@dataclass(frozen=True)
class UpdateResult:
    status: str
    old_commit: str = ""
    new_commit: str = ""
    backup_path: str = ""
    changed_files: tuple[str, ...] = ()
    stage: str = ""
    returncode: int | None = None
    timed_out: bool = False
    details: str = ""


class UpdateCommandError(RuntimeError):
    def __init__(
        self,
        stage: str,
        returncode: int | None = None,
        *,
        timed_out: bool = False,
        details: str = "",
    ):
        super().__init__(stage)
        self.stage = stage
        self.returncode = returncode
        self.timed_out = timed_out
        self.details = details


def validate_update_target(remote: str, branch: str) -> None:
    for name, value in (("remote", remote), ("branch", branch)):
        if (
            not SAFE_GIT_REF_RE.fullmatch(value)
            or value.startswith("-")
            or ".." in value
            or "//" in value
        ):
            raise ValueError(f"Unsafe configured git {name}")


async def _read_limited(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    if stream is None:
        return b""
    limit = max(0, int(limit))
    stored = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        remaining = limit - len(stored)
        if remaining > 0:
            stored.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            truncated = True
    if truncated:
        marker = b"\n...[output truncated]"
        if limit == 0:
            stored = bytearray()
        elif limit <= len(marker):
            stored = bytearray(marker[-limit:])
        else:
            stored = stored[: limit - len(marker)]
            stored.extend(marker)
    return bytes(stored)


async def run_command(
    *args: str,
    timeout: int = GIT_UPDATE_COMMAND_TIMEOUT_SECONDS,
    output_limit: int = COMMAND_OUTPUT_LIMIT,
) -> CommandResult:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
    except (OSError, ValueError):
        raise UpdateCommandError("subprocess") from None
    stdout_task = asyncio.create_task(_read_limited(process.stdout, output_limit))
    stderr_task = asyncio.create_task(_read_limited(process.stderr, output_limit))
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        await asyncio.gather(stdout_task, stderr_task)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise UpdateCommandError("subprocess", timed_out=True) from None
    stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    return CommandResult(
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def _checked_command(stage: str, *args: str, timeout: int | None = None) -> str:
    try:
        result = await run_command(
            *args,
            timeout=timeout or GIT_UPDATE_COMMAND_TIMEOUT_SECONDS,
        )
    except UpdateCommandError as exc:
        raise UpdateCommandError(stage, timed_out=exc.timed_out) from None
    if result.returncode != 0:
        raise UpdateCommandError(stage, result.returncode)
    return result.stdout


def _runtime_paths() -> set[str]:
    paths = {"logs/", "backups/", "update_restart.json"}
    db_path = Path(DB_PATH)
    resolved = db_path.resolve() if db_path.is_absolute() else (PROJECT_ROOT / db_path).resolve()
    try:
        relative = resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        relative = ""
    if relative:
        paths.add(relative)
        paths.add(relative + "-wal")
        paths.add(relative + "-shm")
    return paths


def _status_paths(porcelain: str) -> tuple[str, ...]:
    allowed = _runtime_paths()
    changed: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"').replace("\\", "/")
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        if any(path == item or (item.endswith("/") and path.startswith(item)) for item in allowed):
            continue
        changed.append(path)
    return tuple(changed)


async def get_version_info() -> VersionInfo:
    commit = await _checked_command("version_commit", "git", "rev-parse", "--short", "HEAD")
    branch = await _checked_command("version_branch", "git", "branch", "--show-current")
    commit_date = await _checked_command(
        "version_date", "git", "show", "-s", "--format=%cI", "HEAD"
    )
    status = await _checked_command(
        "version_status", "git", "status", "--porcelain", "--untracked-files=normal"
    )
    return VersionInfo(commit, branch or "detached", commit_date, bool(_status_paths(status)))


async def check_for_updates() -> UpdateCheckResult:
    validate_update_target(GIT_UPDATE_REMOTE, GIT_UPDATE_BRANCH)
    await _checked_command("fetch", "git", "fetch", GIT_UPDATE_REMOTE)
    current = await _checked_command("local_commit", "git", "rev-parse", "HEAD")
    remote_ref = f"{GIT_UPDATE_REMOTE}/{GIT_UPDATE_BRANCH}"
    remote = await _checked_command("remote_commit", "git", "rev-parse", remote_ref)
    behind_text = await _checked_command(
        "commit_count", "git", "rev-list", "--count", f"HEAD..{remote_ref}"
    )
    try:
        behind = max(0, int(behind_text))
    except ValueError:
        raise UpdateCommandError("commit_count") from None
    return UpdateCheckResult(current, remote, behind)


async def create_database_backup() -> Path:
    source = Path(DB_PATH)
    if not source.is_absolute():
        source = PROJECT_ROOT / source
    backup_task = asyncio.create_task(asyncio.to_thread(
        backup_database, source, PROJECT_ROOT / "backups"
    ))
    try:
        backup_path = await asyncio.shield(backup_task)
    except asyncio.CancelledError:
        # sqlite3 backup runs in a thread and cannot be force-cancelled safely.
        # Keep the update lock until it has closed both database connections.
        await backup_task
        raise
    if not backup_path.is_file() or backup_path.stat().st_size <= 0:
        raise RuntimeError("SQLite backup verification failed")
    return backup_path


async def perform_update() -> UpdateResult:
    validate_update_target(GIT_UPDATE_REMOTE, GIT_UPDATE_BRANCH)
    started = asyncio.get_running_loop().time()
    old_commit = ""
    new_commit = ""
    stage = "work_tree"
    try:
        inside = await _checked_command(
            stage, "git", "rev-parse", "--is-inside-work-tree"
        )
        if inside.lower() != "true":
            raise UpdateCommandError(stage)

        stage = "status"
        porcelain = await _checked_command(
            stage, "git", "status", "--porcelain", "--untracked-files=normal"
        )
        changed = _status_paths(porcelain)
        if changed:
            return UpdateResult(status="dirty", changed_files=changed)

        stage = "fetch"
        await _checked_command(stage, "git", "fetch", GIT_UPDATE_REMOTE)
        stage = "local_commit"
        old_commit = await _checked_command(stage, "git", "rev-parse", "HEAD")
        remote_ref = f"{GIT_UPDATE_REMOTE}/{GIT_UPDATE_BRANCH}"
        stage = "remote_commit"
        remote_commit = await _checked_command(stage, "git", "rev-parse", remote_ref)
        if old_commit == remote_commit:
            return UpdateResult(status="current", old_commit=old_commit, new_commit=old_commit)

        stage = "backup"
        try:
            backup_path = await create_database_backup()
        except Exception:
            raise UpdateCommandError(stage) from None

        stage = "pull"
        await _checked_command(
            stage,
            "git",
            "pull",
            "--ff-only",
            GIT_UPDATE_REMOTE,
            GIT_UPDATE_BRANCH,
        )
        stage = "new_commit"
        new_commit = await _checked_command(stage, "git", "rev-parse", "HEAD")

        stage = "dependency_diff"
        dependency_output = await _checked_command(
            stage,
            "git",
            "diff",
            "--name-only",
            old_commit,
            new_commit,
            "--",
            *DEPENDENCY_FILES,
        )
        if dependency_output.strip():
            stage = "dependencies"
            await _checked_command(
                stage,
                sys.executable,
                "-m",
                "pip",
                "install",
                "-r",
                "requirements.txt",
                timeout=GIT_UPDATE_PIP_TIMEOUT_SECONDS,
            )

        stage = "compile"
        await _checked_command(
            stage,
            sys.executable,
            "-m",
            "compileall",
            "-q",
            "bot.py",
            "app",
        )
        stage = "imports"
        await _checked_command(
            stage,
            sys.executable,
            "-c",
            'import app.telegram.application as bot, app.telegram.media as bot_media, app.storage.database as database, app.integrations.rule34.client as api_handler, app.config as config, app.infrastructure.project_update as project_update',
        )
        logger.warning(
            "Project update completed old=%s new=%s elapsed=%.2fs",
            old_commit[:12],
            new_commit[:12],
            asyncio.get_running_loop().time() - started,
        )
        return UpdateResult(
            status="updated",
            old_commit=old_commit,
            new_commit=new_commit,
            backup_path=str(backup_path),
        )
    except UpdateCommandError as exc:
        logger.warning(
            "Project update failed stage=%s rc=%s timeout=%s old=%s new=%s elapsed=%.2fs",
            exc.stage,
            exc.returncode,
            exc.timed_out,
            old_commit[:12],
            new_commit[:12],
            asyncio.get_running_loop().time() - started,
        )
        return UpdateResult(
            status="error",
            old_commit=old_commit,
            new_commit=new_commit,
            stage=exc.stage,
            returncode=exc.returncode,
            timed_out=exc.timed_out,
            details=exc.details,
        )


def write_update_marker(admin_user_id: int, commit: str) -> None:
    if admin_user_id <= 0 or not COMMIT_RE.fullmatch(commit):
        raise ValueError("Invalid update marker")
    UPDATE_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = UPDATE_MARKER_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"admin_user_id": admin_user_id, "commit": commit[:12]}),
        encoding="utf-8",
    )
    os.replace(temporary, UPDATE_MARKER_PATH)


async def notify_update_marker(bot) -> bool:
    if not UPDATE_MARKER_PATH.is_file():
        return False
    try:
        marker = json.loads(UPDATE_MARKER_PATH.read_text(encoding="utf-8"))
        admin_user_id = int(marker["admin_user_id"])
        commit = str(marker["commit"])
        if admin_user_id <= 0 or not COMMIT_RE.fullmatch(commit):
            raise ValueError("Invalid update marker")
        await bot.send_message(
            chat_id=admin_user_id,
            text=f"✅ Бот запущен после обновления. Версия: {commit[:12]}",
        )
    except Exception:
        logger.exception("Failed to process update restart marker")
        return False
    UPDATE_MARKER_PATH.unlink(missing_ok=True)
    return True
