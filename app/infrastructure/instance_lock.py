"""Single-instance process guard and bounded local startup recovery."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import shutil
import socket
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ORPHAN_TTL_SECONDS = 24 * 60 * 60
DEFAULT_ORPHAN_CLEANUP_BATCH = 100


class InstanceLockBusy(RuntimeError):
    """Raised when another process still owns the instance lock."""

    def __init__(self, lock_path: Path, *, owner_pid: int | None = None) -> None:
        self.lock_path = lock_path
        self.owner_pid = owner_pid
        owner = f" (PID {owner_pid})" if owner_pid is not None else ""
        super().__init__(f"Another bot instance owns the process lock{owner}")


class InstanceLockLifecycleError(RuntimeError):
    """Raised for invalid lock lifecycle or failed local startup checks."""


@dataclass(frozen=True)
class StartupCheck:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class StartupChecksResult:
    checks: tuple[StartupCheck, ...]
    free_bytes: int | None = None

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def failed_names(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.ok)


@dataclass(frozen=True)
class OrphanCleanupTarget:
    root: Path
    prefix: str
    directories: bool


@dataclass(frozen=True)
class OrphanCleanupResult:
    deleted: int
    errors: int
    skipped_reparse_points: int


@dataclass(frozen=True)
class InstanceStartupResult:
    checks: StartupChecksResult
    cleanup: OrphanCleanupResult
    wait_ms: int


def _absolute_path(path: str | os.PathLike[str], working_directory: Path) -> Path:
    value = Path(path)
    if not value.is_absolute():
        value = working_directory / value
    return value.resolve(strict=False)


def lock_path_for_database(
    db_path: str | os.PathLike[str],
    *,
    working_directory: str | os.PathLike[str] | None = None,
) -> Path:
    """Return a stable lock path keyed by the absolute primary SQLite path."""
    workdir = Path(working_directory or Path.cwd()).resolve(strict=False)
    absolute_db = _absolute_path(db_path, workdir)
    normalized = os.path.normcase(str(absolute_db))
    digest = hashlib.blake2s(normalized.encode("utf-8"), digest_size=10).hexdigest()
    return Path(tempfile.gettempdir()) / f"night_fun_drug_bot_{digest}.lock"


def _git_revision(project_root: Path) -> str | None:
    """Read a short Git revision without starting a subprocess."""
    try:
        git_dir = project_root / ".git"
        head = (git_dir / "HEAD").read_text(encoding="ascii").strip()
        if head.startswith("ref: "):
            ref_path = git_dir / head[5:]
            head = ref_path.read_text(encoding="ascii").strip()
        if 7 <= len(head) <= 64 and all(char in "0123456789abcdefABCDEF" for char in head):
            return head[:12]
    except (OSError, UnicodeError):
        return None
    return None


class InstanceLock:
    """A non-reentrant OS file lock whose descriptor stays open while held."""

    def __init__(
        self,
        lock_path: str | os.PathLike[str],
        *,
        working_directory: str | os.PathLike[str] | None = None,
        project_root: str | os.PathLike[str] = PROJECT_ROOT,
    ) -> None:
        self.path = Path(lock_path).resolve(strict=False)
        self.working_directory = Path(working_directory or Path.cwd()).resolve(strict=False)
        self.project_root = Path(project_root).resolve(strict=False)
        self._file = None
        self._held = False
        self.last_wait_ms = 0

    @property
    def held(self) -> bool:
        return self._held

    def _open_file(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            file = os.fdopen(descriptor, "r+b", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise
        try:
            file.seek(0)
            return file
        except BaseException:
            file.close()
            raise

    @staticmethod
    def _lock_file(file) -> None:
        file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_file(file) -> None:
        file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def _metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(),
            "hostname": socket.gethostname(),
            "working_directory": str(self.working_directory),
            "python_version": sys.version.split()[0],
        }
        revision = _git_revision(self.project_root)
        if revision:
            metadata["revision"] = revision
        return metadata

    def _write_metadata(self, file) -> None:
        payload = json.dumps(
            self._metadata(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        file.seek(0)
        file.write(payload)
        file.truncate()
        file.flush()
        os.fsync(file.fileno())
        file.seek(0)

    def _owner_pid(self) -> int | None:
        value = self.read_metadata()
        pid = value.get("pid") if isinstance(value, dict) else None
        return pid if isinstance(pid, int) and pid > 0 else None

    def read_metadata(self) -> dict[str, object]:
        """Read bounded public metadata, using the owning descriptor when possible."""
        try:
            if self._held and self._file is not None:
                self._file.seek(0)
                raw = self._file.read(4096)
                self._file.seek(0)
            else:
                with self.path.open("rb") as file:
                    raw = file.read(4096)
            value = json.loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def try_acquire(self) -> bool:
        if self._held or self._file is not None:
            raise InstanceLockLifecycleError("Instance lock is already held by this object")
        try:
            file = self._open_file()
        except OSError as exc:
            raise InstanceLockLifecycleError("Failed to open instance lock file") from exc
        try:
            self._lock_file(file)
        except OSError as exc:
            file.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise InstanceLockLifecycleError("Failed to acquire instance OS lock") from exc
        try:
            self._write_metadata(file)
        except BaseException as exc:
            try:
                self._unlock_file(file)
            finally:
                file.close()
            raise InstanceLockLifecycleError("Failed to write instance lock metadata") from exc
        self._file = file
        self._held = True
        return True

    async def acquire(
        self,
        *,
        wait_seconds: float = 20.0,
        retry_interval_seconds: float = 0.25,
    ) -> int:
        if self._held or self._file is not None:
            raise InstanceLockLifecycleError("Instance lock is already held by this object")
        wait_seconds = max(0.0, float(wait_seconds))
        retry_interval_seconds = max(0.01, float(retry_interval_seconds))
        started = time.monotonic()
        deadline = started + wait_seconds
        logged_wait = False
        while True:
            if self.try_acquire():
                self.last_wait_ms = max(0, round((time.monotonic() - started) * 1000))
                logger.info(
                    "instance_lock_acquired wait_ms=%s path=%s",
                    self.last_wait_ms,
                    self.path,
                )
                return self.last_wait_ms
            owner_pid = self._owner_pid()
            now = time.monotonic()
            if now >= deadline:
                self.last_wait_ms = max(0, round((now - started) * 1000))
                logger.error(
                    "instance_lock_busy wait_ms=%s owner_pid=%s",
                    self.last_wait_ms,
                    owner_pid,
                )
                raise InstanceLockBusy(self.path, owner_pid=owner_pid)
            if not logged_wait:
                logger.warning("instance_lock_waiting owner_pid=%s", owner_pid)
                logged_wait = True
            await asyncio.sleep(min(retry_interval_seconds, max(0.0, deadline - now)))

    def release(self) -> None:
        file = self._file
        if file is None:
            self._held = False
            return
        self._file = None
        self._held = False
        try:
            self._unlock_file(file)
        except OSError:
            logger.exception("instance_lock_release_failed")
        finally:
            try:
                file.close()
            except OSError:
                logger.exception("instance_lock_descriptor_close_failed")


def _directory_write_check(name: str, directory: Path) -> StartupCheck:
    probe_path: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise NotADirectoryError(str(directory))
        descriptor, probe_name = tempfile.mkstemp(prefix=".bot_startup_check_", dir=directory)
        os.close(descriptor)
        probe_path = Path(probe_name)
        probe_path.unlink()
        return StartupCheck(name, True)
    except OSError as exc:
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass
        return StartupCheck(name, False, type(exc).__name__)


def _database_path_check(name: str, path: Path) -> StartupCheck:
    parent_check = _directory_write_check(name, path.parent)
    if not parent_check.ok:
        return parent_check
    try:
        if path.exists():
            if not path.is_file():
                raise OSError("database path is not a file")
            with path.open("r+b"):
                pass
        return StartupCheck(name, True)
    except OSError as exc:
        return StartupCheck(name, False, type(exc).__name__)


def run_startup_checks(
    *,
    working_directory: str | os.PathLike[str],
    database_path: str | os.PathLike[str],
    callback_database_path: str | os.PathLike[str],
    zip_temp_root: str | os.PathLike[str] | None = None,
    backup_directory: str | os.PathLike[str] | None = None,
) -> StartupChecksResult:
    workdir = Path(working_directory).resolve(strict=False)
    database = _absolute_path(database_path, workdir)
    callback_database = _absolute_path(callback_database_path, workdir)
    zip_root = Path(zip_temp_root or tempfile.gettempdir()).resolve(strict=False)
    backup_root = Path(backup_directory or workdir / "backups").resolve(strict=False)
    checks = [
        _directory_write_check("working_directory", workdir),
        _database_path_check("sqlite_directory", database),
        _database_path_check("callback_payload_database", callback_database),
        _directory_write_check("zip_temp_root", zip_root),
        _directory_write_check("backup_directory", backup_root),
    ]
    free_bytes: int | None = None
    try:
        free_bytes = shutil.disk_usage(workdir).free
        checks.append(StartupCheck("disk_space", True, f"free_bytes={free_bytes}"))
    except OSError as exc:
        checks.append(StartupCheck("disk_space", False, type(exc).__name__))
    return StartupChecksResult(tuple(checks), free_bytes)


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(
        os.name == "nt"
        and getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _tree_contains_reparse_point(path: Path) -> bool:
    if _is_reparse_point(path):
        return True
    if not path.is_dir():
        return False
    try:
        for root, directories, files in os.walk(path, topdown=True, followlinks=False):
            for name in (*directories, *files):
                if _is_reparse_point(Path(root) / name):
                    return True
    except OSError:
        return True
    return False


def cleanup_orphan_temp_files(
    targets: Iterable[OrphanCleanupTarget],
    *,
    ttl_seconds: float = DEFAULT_ORPHAN_TTL_SECONDS,
    batch_size: int = DEFAULT_ORPHAN_CLEANUP_BATCH,
    now: float | None = None,
) -> OrphanCleanupResult:
    cutoff = (time.time() if now is None else float(now)) - max(0.0, ttl_seconds)
    limit = max(0, int(batch_size))
    candidates: list[tuple[float, str, Path, bool]] = []
    errors = 0
    for target in targets:
        if not target.root.exists():
            continue
        if _is_reparse_point(target.root):
            errors += 1
            continue
        try:
            entries = tuple(target.root.iterdir())
        except OSError:
            errors += 1
            continue
        for path in entries:
            if not path.name.startswith(target.prefix):
                continue
            try:
                info = path.lstat()
            except OSError:
                errors += 1
                continue
            is_directory = stat.S_ISDIR(info.st_mode)
            if is_directory != target.directories or info.st_mtime >= cutoff:
                continue
            candidates.append((info.st_mtime, path.name, path, is_directory))
    candidates.sort(key=lambda item: (item[0], item[1]))
    deleted = 0
    processed = 0
    skipped_reparse_points = 0
    for _mtime, _name, path, is_directory in candidates:
        if processed >= limit:
            break
        processed += 1
        try:
            if _tree_contains_reparse_point(path):
                skipped_reparse_points += 1
                continue
            if is_directory:
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted += 1
        except OSError:
            errors += 1
            logger.warning("startup_orphan_cleanup_failed path=%s", path, exc_info=True)
    return OrphanCleanupResult(deleted, errors, skipped_reparse_points)


def default_orphan_cleanup_targets(
    *,
    project_root: str | os.PathLike[str] = PROJECT_ROOT,
    temp_root: str | os.PathLike[str] | None = None,
) -> tuple[OrphanCleanupTarget, ...]:
    project = Path(project_root).resolve(strict=False)
    temporary = Path(temp_root or tempfile.gettempdir()).resolve(strict=False)
    return (
        OrphanCleanupTarget(temporary, "zip_export_", True),
        OrphanCleanupTarget(temporary, "download_photo_", False),
        OrphanCleanupTarget(project / "logs", "update_restart.tmp", False),
    )


class BotInstanceLifecycle:
    """Own the process lock throughout all application startup and shutdown."""

    def __init__(
        self,
        lock: InstanceLock,
        *,
        wait_seconds: float,
        retry_interval_seconds: float,
        startup_checks: Callable[[], StartupChecksResult],
        orphan_cleanup: Callable[[], OrphanCleanupResult],
    ) -> None:
        self.lock = lock
        self.wait_seconds = wait_seconds
        self.retry_interval_seconds = retry_interval_seconds
        self._startup_checks = startup_checks
        self._orphan_cleanup = orphan_cleanup
        self.result: InstanceStartupResult | None = None

    async def start(self) -> InstanceStartupResult:
        if self.result is not None or self.lock.held:
            raise InstanceLockLifecycleError("Instance lifecycle is already started")
        try:
            wait_ms = await self.lock.acquire(
                wait_seconds=self.wait_seconds,
                retry_interval_seconds=self.retry_interval_seconds,
            )
            checks = self._startup_checks()
            logger.info(
                "startup_checks_result ok=%s passed=%s failed=%s",
                int(checks.ok),
                sum(check.ok for check in checks.checks),
                len(checks.failed_names),
            )
            if not checks.ok:
                raise InstanceLockLifecycleError(
                    "Startup checks failed: " + ", ".join(checks.failed_names)
                )
            cleanup = self._orphan_cleanup()
            logger.info(
                "startup_orphan_cleanup deleted=%s errors=%s skipped_reparse=%s",
                cleanup.deleted,
                cleanup.errors,
                cleanup.skipped_reparse_points,
            )
            self.result = InstanceStartupResult(checks, cleanup, wait_ms)
            return self.result
        except (asyncio.CancelledError, InstanceLockBusy, InstanceLockLifecycleError):
            self.lock.release()
            raise
        except Exception as exc:
            self.lock.release()
            raise InstanceLockLifecycleError("Instance startup failed") from exc

    def close(self) -> None:
        self.lock.release()
        self.result = None


def create_instance_lifecycle(
    *,
    database_path: str | os.PathLike[str],
    wait_seconds: float,
    retry_interval_seconds: float,
    working_directory: str | os.PathLike[str] | None = None,
) -> BotInstanceLifecycle:
    workdir = Path(working_directory or Path.cwd()).resolve(strict=False)
    lock = InstanceLock(
        lock_path_for_database(database_path, working_directory=workdir),
        working_directory=workdir,
    )
    return BotInstanceLifecycle(
        lock,
        wait_seconds=wait_seconds,
        retry_interval_seconds=retry_interval_seconds,
        startup_checks=lambda: run_startup_checks(
            working_directory=workdir,
            database_path=database_path,
            callback_database_path=database_path,
            backup_directory=PROJECT_ROOT / "backups",
        ),
        orphan_cleanup=lambda: cleanup_orphan_temp_files(
            default_orphan_cleanup_targets(project_root=PROJECT_ROOT)
        ),
    )
