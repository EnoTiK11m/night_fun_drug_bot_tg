import asyncio
import contextlib
import logging
from app.observability.logic_trace import traced_flow, annotate
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

import aiohttp
from telegram.error import RetryAfter, TimedOut

from app.config import (
    ZIP_EXPORT_MAX_FILE_BYTES,
    ZIP_EXPORT_MAX_FILES,
    ZIP_EXPORT_MAX_PARTS,
    ZIP_EXPORT_MAX_TEMP_BYTES,
    ZIP_EXPORT_MAX_TOTAL_BYTES,
    ZIP_EXPORT_PART_BYTES,
    ZIP_EXPORT_PROGRESS_INTERVAL_SECONDS,
    ZIP_EXPORT_QUEUE_SIZE,
    ZIP_EXPORT_TIMEOUT_SECONDS,
    ZIP_EXPORT_WORKERS,
)
from app.telegram.media import (
    DownloadByteBudget,
    FileDownloadLimitExceeded,
    TotalDownloadLimitExceeded,
    download_photo_to_path,
    create_public_photo_session,
)
from app.telegram.delivery import execute_telegram_request, telegram_rate_limiter

logger = logging.getLogger(__name__)
ZIP_ENTRY_OVERHEAD_BYTES = 512

ZipJobState = Literal["queued", "running", "success", "partial", "cancelled", "limit", "download_error", "failed"]
ZipEnqueueState = Literal[
    "queued", "duplicate", "queue_full", "cooldown", "shutting_down", "cancelled"
]
ZipSourceKind = Literal["favorites", "collection"]


class ZipExportLoadError(Exception):
    pass


class ZipExportLimitExceeded(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ZipExportDownloadError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SourceLoader(Protocol):
    async def __call__(self, user_id: int, source_kind: ZipSourceKind, collection_id: int | None) -> "ZipExportSource":
        ...


class PostResolver(Protocol):
    async def __call__(self, post: dict[str, Any]) -> dict[str, Any]:
        ...


class CancelMarkupFactory(Protocol):
    def __call__(self, job_id: str) -> Any:
        ...


@dataclass(frozen=True, slots=True)
class ZipExportSource:
    title: str
    posts: list[dict[str, Any]]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ZipExportEnqueueResult:
    status: ZipEnqueueState
    job_id: str | None = None
    position: int | None = None
    retry_after_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ZipExportResult:
    status: ZipJobState
    exported_files: int
    skipped_files: int
    sent_parts: int
    downloaded_bytes: int
    message: str
    limit_reason: str | None = None


@dataclass(slots=True)
class ZipExportProgress:
    total_files: int | None = None
    exported_files: int = 0
    skipped_files: int = 0
    sent_parts: int = 0
    downloaded_bytes: int = 0
    queued_position: int | None = None


@dataclass(slots=True)
class ZipExportJob:
    job_id: str
    user_id: int
    chat_id: int
    source_kind: ZipSourceKind
    collection_id: int | None
    title_hint: str
    created_at: float
    status_message_id: int | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    tempdir: str | None = None
    progress: ZipExportProgress = field(default_factory=ZipExportProgress)


def _safe_post_id(value: Any) -> str:
    text = str(value or "unknown")
    sanitized = "".join(ch for ch in text if ch.isalnum() or ch in {"_", "-"})
    return sanitized[:64] or "unknown"


def _safe_zip_filename(index: int, post_id: Any, extension: str) -> str:
    clean_extension = extension if extension.startswith(".") else f".{extension}"
    clean_extension = "".join(ch for ch in clean_extension.lower() if ch.isalnum() or ch == ".")
    if clean_extension not in {".jpg", ".jpeg", ".png", ".webp"}:
        clean_extension = ".jpg"
    return f"{index:05d}_{_safe_post_id(post_id)}{clean_extension}"


def _format_megabytes(value: int) -> str:
    return f"{value / (1024 * 1024):.0f} МБ"


class JobStatusReporter:
    def __init__(
        self,
        manager: "ZipExportManager",
        job: ZipExportJob,
        *,
        progress_interval_seconds: float,
    ) -> None:
        self._manager = manager
        self._job = job
        self._progress_interval_seconds = progress_interval_seconds
        self._last_update_at = 0.0
        self._last_text = ""

    async def set_status_message(self, message_id: int | None) -> None:
        self._job.status_message_id = message_id

    async def queued(self, position: int) -> None:
        self._job.progress.queued_position = position
        text = f"📦 Экспорт поставлен в очередь. Позиция: {position}."
        await self._send(text=text, force=True, include_cancel=True)

    async def running(self) -> None:
        await self._send(text=self._build_running_text(), force=True, include_cancel=True)

    async def progress(self, force: bool = False) -> None:
        await self._send(text=self._build_running_text(), force=force, include_cancel=True)

    async def finished(self, result: ZipExportResult) -> None:
        await self._send(text=result.message, force=True, include_cancel=False)

    def _build_running_text(self) -> str:
        total = self._job.progress.total_files or 0
        return (
            f"📦 Экспорт: {self._job.progress.exported_files} из {total}\n"
            f"Архивов отправлено: {self._job.progress.sent_parts} · "
            f"скачано {_format_megabytes(self._job.progress.downloaded_bytes)}"
        )

    async def _send(self, *, text: str, force: bool, include_cancel: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_update_at < self._progress_interval_seconds:
            return
        if not force and text == self._last_text:
            return
        markup = self._manager._cancel_markup(self._job.job_id) if include_cancel else None
        try:
            if self._job.status_message_id is None:
                sent = await self._manager._telegram_request(
                    self._job.chat_id,
                    lambda: self._manager._bot.send_message(
                        chat_id=self._job.chat_id,
                        text=text,
                        reply_markup=markup,
                    ),
                    operation_name="zip_status_send",
                )
                self._job.status_message_id = getattr(sent, "message_id", None)
            else:
                await self._manager._telegram_request(
                    self._job.chat_id,
                    lambda: self._manager._bot.edit_message_text(
                        chat_id=self._job.chat_id,
                        message_id=self._job.status_message_id,
                        text=text,
                        reply_markup=markup,
                    ),
                    operation_name="zip_status_edit",
                    safe_to_retry_timeout=True,
                    ambiguous_bad_request_policy="edit_message_text",
                )
        except Exception:
            logger.warning("ZIP export status update failed user=%s job=%s", self._job.user_id, self._job.job_id)
            return
        self._last_update_at = now
        self._last_text = text


class ZipExportManager:
    """
    Single-process in-memory ZIP export manager.

    Jobs are not durable and are lost on process restart.
    """

    def __init__(
        self,
        *,
        application,
        source_loader: SourceLoader,
        post_resolver: PostResolver | None = None,
        cancel_markup_factory: CancelMarkupFactory | None = None,
        session_factory: Callable[[], aiohttp.ClientSession] | None = None,
        worker_count: int = ZIP_EXPORT_WORKERS,
        queue_capacity: int = ZIP_EXPORT_QUEUE_SIZE,
        export_timeout_seconds: int = ZIP_EXPORT_TIMEOUT_SECONDS,
        max_files: int = ZIP_EXPORT_MAX_FILES,
        max_file_bytes: int = ZIP_EXPORT_MAX_FILE_BYTES,
        max_total_bytes: int = ZIP_EXPORT_MAX_TOTAL_BYTES,
        part_limit_bytes: int = ZIP_EXPORT_PART_BYTES,
        max_parts: int = ZIP_EXPORT_MAX_PARTS,
        max_temp_bytes: int = ZIP_EXPORT_MAX_TEMP_BYTES,
        progress_interval_seconds: float = ZIP_EXPORT_PROGRESS_INTERVAL_SECONDS,
        favorites_cooldown_seconds: int = 0,
        monotonic: Callable[[], float] = time.monotonic,
        rate_limiter=telegram_rate_limiter,
    ) -> None:
        self._application = application
        self._bot = application.bot
        self._source_loader = source_loader
        self._post_resolver = post_resolver
        self._cancel_markup = cancel_markup_factory or (lambda _job_id: None)
        self._session_factory = session_factory or self._default_session_factory
        self._worker_count = max(1, int(worker_count))
        self._queue_capacity = max(1, int(queue_capacity))
        self._export_timeout_seconds = max(1, int(export_timeout_seconds))
        self._max_files = max(1, int(max_files))
        self._max_file_bytes = max(1, int(max_file_bytes))
        self._max_total_bytes = max(self._max_file_bytes, int(max_total_bytes))
        self._part_limit_bytes = max(1, int(part_limit_bytes))
        self._max_parts = max(1, int(max_parts))
        self._max_temp_bytes = max(1, int(max_temp_bytes))
        self._progress_interval_seconds = max(0.5, float(progress_interval_seconds))
        self._favorites_cooldown_seconds = max(0, int(favorites_cooldown_seconds))
        self._monotonic = monotonic
        self._rate_limiter = rate_limiter
        self._condition = asyncio.Condition()
        self._worker_tasks: list[asyncio.Task] = []
        self._jobs_by_id: dict[str, ZipExportJob] = {}
        self._job_id_by_user: dict[int, str] = {}
        self._pending_job_ids: deque[str] = deque()
        self._active_job_ids: set[str] = set()
        self._favorites_last_finished_at: dict[int, float] = {}
        self._closing = False

    def _default_session_factory(self) -> aiohttp.ClientSession:
        timeout = aiohttp.ClientTimeout(total=self._export_timeout_seconds)
        return create_public_photo_session(
            timeout=timeout,
            headers={"User-Agent": "night-fun-drug-bot/1.0"},
        )

    async def _telegram_request(
        self,
        chat_id: int,
        operation: Callable[[], Awaitable[Any]],
        *,
        operation_name: str,
        safe_to_retry_timeout: bool = False,
        ambiguous_bad_request_policy=None,
    ) -> Any:
        return await execute_telegram_request(
            operation,
            operation_name=operation_name,
            chat_id=chat_id,
            safe_to_retry_timeout=safe_to_retry_timeout,
            ambiguous_bad_request_policy=ambiguous_bad_request_policy,
            limiter=self._rate_limiter,
        )

    async def start(self) -> None:
        async with self._condition:
            if self._worker_tasks:
                return
            self._closing = False
            self._worker_tasks = [
                asyncio.create_task(self._worker_loop(index + 1), name=f"zip-export-worker-{index + 1}")
                for index in range(self._worker_count)
            ]

    async def stop(self) -> None:
        async with self._condition:
            if self._closing:
                return
            self._closing = True
            pending_jobs = [
                self._jobs_by_id[job_id]
                for job_id in self._pending_job_ids
                if job_id in self._jobs_by_id
            ]
            self._pending_job_ids.clear()
            for job in pending_jobs:
                job.cancel_event.set()
                job.ready_event.set()
                self._cleanup_job_registry(job)
            active_tasks = list({
                job.task for job in self._jobs_by_id.values()
                if job.task is not None and not job.task.done()
            })
            worker_tasks = list(self._worker_tasks)
            self._condition.notify_all()

        for job in pending_jobs:
            if job.status_message_id is not None:
                await self._safe_finish_status(
                    job,
                    ZipExportResult(
                        status="cancelled",
                        exported_files=job.progress.exported_files,
                        skipped_files=job.progress.skipped_files,
                        sent_parts=job.progress.sent_parts,
                        downloaded_bytes=job.progress.downloaded_bytes,
                        message="🛑 Экспорт отменён из-за остановки приложения.",
                    ),
                )

        for task in active_tasks:
            task.cancel()

        results = await asyncio.gather(*active_tasks, return_exceptions=True)
        async with self._condition:
            self._condition.notify_all()
        worker_results = await asyncio.gather(*worker_tasks, return_exceptions=True)
        results.extend(worker_results)
        for result in results:
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                logger.warning("ZIP export stop collected exception: %s", type(result).__name__)

        async with self._condition:
            self._worker_tasks.clear()

    async def enqueue_favorites(self, message, user_id: int, *, title: str = "Избранное") -> ZipExportEnqueueResult:
        return await self._enqueue_job(message, user_id, source_kind="favorites", collection_id=None, title_hint=title)

    async def enqueue_collection(
        self,
        message,
        user_id: int,
        collection_id: int,
        *,
        title: str = "Коллекция",
    ) -> ZipExportEnqueueResult:
        return await self._enqueue_job(
            message,
            user_id,
            source_kind="collection",
            collection_id=collection_id,
            title_hint=title,
        )

    async def cancel_for_user(self, user_id: int, job_id: str | None = None) -> bool:
        async with self._condition:
            current_job_id = self._job_id_by_user.get(user_id)
            if current_job_id is None or (job_id and current_job_id != job_id):
                return False
            job = self._jobs_by_id.get(current_job_id)
            if job is None:
                return False
            is_pending = current_job_id in self._pending_job_ids
            task = job.task
            if not is_pending and task is not None and task.done():
                return False
            job.cancel_event.set()
            handshake_pending = not job.ready_event.is_set()
            if is_pending:
                self._pending_job_ids.remove(current_job_id)
                self._cleanup_job_registry(job)
                self._condition.notify_all()
                pending_cancel = True
            else:
                pending_cancel = False

        if pending_cancel:
            if not handshake_pending:
                await self._safe_finish_status(
                    job,
                    ZipExportResult(
                        status="cancelled",
                        exported_files=job.progress.exported_files,
                        skipped_files=job.progress.skipped_files,
                        sent_parts=job.progress.sent_parts,
                        downloaded_bytes=job.progress.downloaded_bytes,
                        message="🛑 Экспорт отменён.",
                    ),
                )
            return True

        if task and not task.done():
            task.cancel()
        return True

    def stats(self) -> dict[str, int]:
        return {
            "queued": len(self._pending_job_ids),
            "active": len(self._active_job_ids),
            "tracked_users": len(self._job_id_by_user),
        }

    async def _enqueue_job(
        self,
        message,
        user_id: int,
        *,
        source_kind: ZipSourceKind,
        collection_id: int | None,
        title_hint: str,
    ) -> ZipExportEnqueueResult:
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        if not isinstance(chat_id, int):
            raise ValueError("ZIP export requires a message with an integer chat.id")

        now = self._monotonic()
        async with self._condition:
            if self._closing:
                return ZipExportEnqueueResult(status="shutting_down")
            self._cleanup_expired_cooldowns(now)
            existing_job_id = self._job_id_by_user.get(user_id)
            if existing_job_id and existing_job_id in self._jobs_by_id:
                position = self._queue_position(existing_job_id)
                return ZipExportEnqueueResult(status="duplicate", job_id=existing_job_id, position=position)
            if source_kind == "favorites" and self._favorites_cooldown_seconds > 0:
                retry_after = self._cooldown_retry_after(user_id, now)
                if retry_after > 0:
                    return ZipExportEnqueueResult(
                        status="cooldown",
                        retry_after_seconds=retry_after,
                    )
            if len(self._pending_job_ids) >= self._queue_capacity:
                return ZipExportEnqueueResult(status="queue_full")

            job_id = uuid.uuid4().hex
            job = ZipExportJob(
                job_id=job_id,
                user_id=user_id,
                chat_id=chat_id,
                source_kind=source_kind,
                collection_id=collection_id,
                title_hint=title_hint,
                created_at=now,
            )
            self._jobs_by_id[job_id] = job
            self._job_id_by_user[user_id] = job_id
            self._pending_job_ids.append(job_id)
            position = len(self._pending_job_ids)

        reporter = JobStatusReporter(
            self,
            job,
            progress_interval_seconds=self._progress_interval_seconds,
        )
        status_message_id = None
        try:
            status_message = await message.reply_text(
                f"📦 Экспорт поставлен в очередь. Позиция: {position}.",
                reply_markup=self._cancel_markup(job_id),
            )
            status_message_id = getattr(status_message, "message_id", None)
        except Exception:
            logger.warning("ZIP export queue message failed user=%s job=%s", user_id, job_id)
        except asyncio.CancelledError:
            async with self._condition:
                job.cancel_event.set()
                if job_id in self._pending_job_ids:
                    self._pending_job_ids.remove(job_id)
                self._cleanup_job_registry(job)
                job.ready_event.set()
                self._condition.notify_all()
            raise

        async with self._condition:
            still_registered = self._jobs_by_id.get(job_id) is job
            can_queue = still_registered and not job.cancel_event.is_set() and not self._closing
            if can_queue:
                job.status_message_id = status_message_id
            else:
                if job_id in self._pending_job_ids:
                    self._pending_job_ids.remove(job_id)
                self._cleanup_job_registry(job)
            job.ready_event.set()
            self._condition.notify_all()

        if not can_queue:
            if status_message_id is not None:
                job.status_message_id = status_message_id
                await self._safe_finish_status(
                    job,
                    ZipExportResult(
                        status="cancelled",
                        exported_files=0,
                        skipped_files=0,
                        sent_parts=0,
                        downloaded_bytes=0,
                        message="🛑 Экспорт отменён.",
                    ),
                )
            return ZipExportEnqueueResult(status="cancelled", job_id=job_id)

        await reporter.set_status_message(status_message_id)
        return ZipExportEnqueueResult(status="queued", job_id=job_id, position=position)

    async def _worker_loop(self, worker_index: int) -> None:
        try:
            while True:
                job = await self._next_job()
                if job is None:
                    return
                reporter = JobStatusReporter(
                    self,
                    job,
                    progress_interval_seconds=self._progress_interval_seconds,
                )
                job_task = asyncio.create_task(
                    self._execute_job(job, reporter),
                    name=f"zip-export-job-{job.job_id[:8]}",
                )
                job.task = job_task
                try:
                    result = await job_task
                except asyncio.CancelledError:
                    result = ZipExportResult(
                        status="cancelled",
                        exported_files=job.progress.exported_files,
                        skipped_files=job.progress.skipped_files,
                        sent_parts=job.progress.sent_parts,
                        downloaded_bytes=job.progress.downloaded_bytes,
                        message="🛑 Экспорт отменён.",
                    )
                    await self._finish_job(job, result)
                    if self._closing:
                        raise
                    continue
                except TimeoutError:
                    result = ZipExportResult(
                        status="limit",
                        exported_files=job.progress.exported_files,
                        skipped_files=job.progress.skipped_files,
                        sent_parts=job.progress.sent_parts,
                        downloaded_bytes=job.progress.downloaded_bytes,
                        limit_reason="timeout",
                        message="⏱ Экспорт остановлен: превышено время выполнения.",
                    )
                    await self._finish_job(job, result)
                except RetryAfter:
                    partial = job.progress.sent_parts > 0
                    result = ZipExportResult(
                        status="partial" if partial else "failed",
                        exported_files=job.progress.exported_files,
                        skipped_files=job.progress.skipped_files,
                        sent_parts=job.progress.sent_parts,
                        downloaded_bytes=job.progress.downloaded_bytes,
                        message=(
                            "⚠️ Экспорт остановлен из-за ограничения Telegram. "
                            "Уже отправленные ZIP-части сохранены."
                            if partial
                            else "⏳ Telegram временно ограничил отправку ZIP. Попробуйте позже."
                        ),
                    )
                    await self._finish_job(job, result)
                except TimedOut:
                    result = ZipExportResult(
                        status="partial",
                        exported_files=job.progress.exported_files,
                        skipped_files=job.progress.skipped_files,
                        sent_parts=job.progress.sent_parts,
                        downloaded_bytes=job.progress.downloaded_bytes,
                        message=(
                            "⚠️ Telegram не подтвердил отправку ZIP. "
                            "Чтобы избежать дубликатов, повторите экспорт позже."
                        ),
                    )
                    await self._finish_job(job, result)
                except Exception:
                    logger.exception("ZIP export worker failed user=%s job=%s worker=%s", job.user_id, job.job_id, worker_index)
                    result = ZipExportResult(
                        status="failed",
                        exported_files=job.progress.exported_files,
                        skipped_files=job.progress.skipped_files,
                        sent_parts=job.progress.sent_parts,
                        downloaded_bytes=job.progress.downloaded_bytes,
                        message="❌ Экспорт завершился ошибкой.",
                    )
                    await self._finish_job(job, result)
                else:
                    await self._finish_job(job, result)
                finally:
                    job.task = None
        except asyncio.CancelledError:
            raise

    @traced_flow("zip", metadata=lambda b: {"user_id": b["job"].user_id, "job_id": b["job"].job_id})
    async def _execute_job(
        self, job: ZipExportJob, reporter: JobStatusReporter
    ) -> ZipExportResult:
        async with asyncio.timeout(self._export_timeout_seconds):
            return await self._run_job(job, reporter)

    async def _next_job(self) -> ZipExportJob | None:
        async with self._condition:
            while not self._closing and not self._pending_job_ids:
                await self._condition.wait()
            if self._closing:
                return None
            while self._pending_job_ids:
                job_id = self._pending_job_ids[0]
                job = self._jobs_by_id.get(job_id)
                if job is None:
                    self._pending_job_ids.popleft()
                    continue
                if not job.ready_event.is_set():
                    await self._condition.wait()
                    if self._closing:
                        return None
                    continue
                self._pending_job_ids.popleft()
                if job.cancel_event.is_set():
                    self._cleanup_job_registry(job)
                    continue
                self._active_job_ids.add(job_id)
                return job
            return None

    async def _run_job(self, job: ZipExportJob, reporter: JobStatusReporter) -> ZipExportResult:
        source = await self._source_loader(job.user_id, job.source_kind, job.collection_id)
        if job.cancel_event.is_set():
            raise asyncio.CancelledError
        source_truncated = source.truncated or len(source.posts) > self._max_files
        posts = source.posts[:self._max_files]
        total_posts = len(posts)
        job.progress.total_files = total_posts
        await reporter.running()

        if not posts:
            return ZipExportResult(
                status="download_error",
                exported_files=0,
                skipped_files=0,
                sent_parts=0,
                downloaded_bytes=0,
                message="❌ Не найдено файлов для экспорта.",
            )

        tempdir = tempfile.mkdtemp(prefix=f"zip_export_{job.user_id}_{job.job_id[:8]}_")
        job.tempdir = tempdir
        session: aiohttp.ClientSession | None = None
        archive: zipfile.ZipFile | None = None
        archive_path: str | None = None
        current_download_path: str | None = None
        current_part = 1
        current_part_bytes = 0
        next_part_caption_index = 1
        byte_budget = DownloadByteBudget(self._max_total_bytes)
        file_limit_hit = False

        try:
            session = self._session_factory()
            archive_path = os.path.join(tempdir, f"export_{current_part}.zip")
            archive = zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_STORED)

            for index, post in enumerate(posts, start=1):
                if job.cancel_event.is_set():
                    raise asyncio.CancelledError
                try:
                    resolved_post = await self._resolve_post(post)
                    file_url = resolved_post.get("file_url")
                    if not file_url:
                        job.progress.skipped_files += 1
                        await reporter.progress()
                        continue

                    current_download_path = os.path.join(tempdir, f"download_{index}.bin")
                    meta = await download_photo_to_path(
                        file_url,
                        current_download_path,
                        session=session,
                        max_bytes=self._max_file_bytes,
                        cancel_event=job.cancel_event,
                        byte_budget=byte_budget,
                    )
                except asyncio.CancelledError:
                    job.progress.downloaded_bytes = byte_budget.consumed
                    raise
                except TotalDownloadLimitExceeded:
                    job.progress.downloaded_bytes = byte_budget.consumed
                    raise ZipExportLimitExceeded("total_bytes")
                except FileDownloadLimitExceeded as exc:
                    job.progress.downloaded_bytes = byte_budget.consumed
                    file_limit_hit = True
                    if current_download_path and os.path.exists(current_download_path):
                        with contextlib.suppress(OSError):
                            os.remove(current_download_path)
                    current_download_path = None
                    job.progress.skipped_files += 1
                    logger.warning(
                        "ZIP export item exceeded file limit user=%s job=%s post=%s type=%s",
                        job.user_id,
                        job.job_id,
                        _safe_post_id(post.get("id")),
                        type(exc).__name__,
                    )
                    await reporter.progress()
                    continue
                except (aiohttp.ClientError, OSError, ValueError) as exc:
                    job.progress.downloaded_bytes = byte_budget.consumed
                    if current_download_path and os.path.exists(current_download_path):
                        with contextlib.suppress(OSError):
                            os.remove(current_download_path)
                    current_download_path = None
                    job.progress.skipped_files += 1
                    logger.warning(
                        "ZIP export item skipped user=%s job=%s post=%s type=%s",
                        job.user_id,
                        job.job_id,
                        _safe_post_id(post.get("id")),
                        type(exc).__name__,
                    )
                    await reporter.progress()
                    continue

                job.progress.downloaded_bytes = byte_budget.consumed
                entry_footprint = meta.bytes_read + ZIP_ENTRY_OVERHEAD_BYTES
                if entry_footprint > self._part_limit_bytes:
                    raise ZipExportLimitExceeded("part_size")
                if current_part_bytes and current_part_bytes + entry_footprint > self._part_limit_bytes:
                    await self._finalize_part(job, source.title, archive, archive_path, next_part_caption_index)
                    archive = None
                    archive_path = None
                    job.progress.sent_parts += 1
                    next_part_caption_index += 1
                    current_part += 1
                    current_part_bytes = 0
                    if current_part > self._max_parts:
                        raise ZipExportLimitExceeded("part_count")
                    archive_path = os.path.join(tempdir, f"export_{current_part}.zip")
                    archive = zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_STORED)

                if current_part_bytes + entry_footprint > self._max_temp_bytes:
                    raise ZipExportLimitExceeded("temp_bytes")

                zip_filename = _safe_zip_filename(index, resolved_post.get("id"), meta.extension)
                with open(current_download_path, "rb") as source_file:
                    with archive.open(zip_filename, "w") as archive_file:
                        shutil.copyfileobj(source_file, archive_file, length=64 * 1024)
                current_part_bytes += entry_footprint
                job.progress.downloaded_bytes = byte_budget.consumed
                job.progress.exported_files += 1

                try:
                    os.remove(current_download_path)
                except OSError:
                    logger.warning("ZIP export temp file cleanup failed user=%s job=%s", job.user_id, job.job_id)
                current_download_path = None
                await reporter.progress()

            if archive is not None and archive_path is not None and job.progress.exported_files > 0:
                await self._finalize_part(job, source.title, archive, archive_path, next_part_caption_index)
                job.progress.sent_parts += 1
                archive = None
                archive_path = None

            if job.progress.exported_files == 0:
                if source_truncated or file_limit_hit:
                    reason = "file_count" if source_truncated else "file_size"
                    return ZipExportResult(
                        status="limit",
                        exported_files=0,
                        skipped_files=job.progress.skipped_files,
                        sent_parts=0,
                        downloaded_bytes=job.progress.downloaded_bytes,
                        limit_reason=reason,
                        message=self._limit_message(job, reason),
                    )
                return ZipExportResult(
                    status="download_error",
                    exported_files=0,
                    skipped_files=job.progress.skipped_files,
                    sent_parts=0,
                    downloaded_bytes=job.progress.downloaded_bytes,
                    message="❌ Не нашлось подходящих оригинальных изображений для архива.",
                )
            if job.progress.skipped_files or source_truncated:
                reason = "file_count" if source_truncated else (
                    "file_size" if file_limit_hit else None
                )
                return ZipExportResult(
                    status="partial",
                    exported_files=job.progress.exported_files,
                    skipped_files=job.progress.skipped_files,
                    sent_parts=job.progress.sent_parts,
                    downloaded_bytes=job.progress.downloaded_bytes,
                    limit_reason=reason,
                    message=(
                        f"⚠️ Частичный экспорт: {job.progress.exported_files} файлов, "
                        f"ZIP: {job.progress.sent_parts}, пропущено: {job.progress.skipped_files}."
                        + (
                            f" Обработано не более {self._max_files} кандидатов."
                            if source_truncated else ""
                        )
                ),
            )
            return ZipExportResult(
                status="success",
                exported_files=job.progress.exported_files,
                skipped_files=0,
                sent_parts=job.progress.sent_parts,
                downloaded_bytes=job.progress.downloaded_bytes,
                message=(
                    f"✅ Экспорт завершён: {job.progress.exported_files} файлов, "
                    f"ZIP: {job.progress.sent_parts}."
                ),
            )
        except asyncio.CancelledError:
            raise
        except ZipExportLimitExceeded as exc:
            if (
                archive is not None and archive_path is not None
                and current_part_bytes > 0 and os.path.exists(archive_path)
            ):
                await self._finalize_part(job, source.title, archive, archive_path, next_part_caption_index)
                job.progress.sent_parts += 1
                archive = None
                archive_path = None
                current_part_bytes = 0
            return ZipExportResult(
                status="limit" if job.progress.exported_files == 0 else "partial",
                exported_files=job.progress.exported_files,
                skipped_files=job.progress.skipped_files,
                sent_parts=job.progress.sent_parts,
                downloaded_bytes=job.progress.downloaded_bytes,
                limit_reason=exc.reason,
                message=self._limit_message(job, exc.reason),
            )
        except (aiohttp.ClientError, OSError, ValueError) as exc:
            if (
                archive is not None and archive_path is not None
                and current_part_bytes > 0 and os.path.exists(archive_path)
            ):
                await self._finalize_part(job, source.title, archive, archive_path, next_part_caption_index)
                job.progress.sent_parts += 1
                archive = None
                archive_path = None
                current_part_bytes = 0
            logger.warning("ZIP export download failed user=%s job=%s type=%s", job.user_id, job.job_id, type(exc).__name__)
            return ZipExportResult(
                status="download_error" if job.progress.exported_files == 0 else "partial",
                exported_files=job.progress.exported_files,
                skipped_files=job.progress.skipped_files + 1,
                sent_parts=job.progress.sent_parts,
                downloaded_bytes=job.progress.downloaded_bytes,
                message=(
                    "⚠️ Экспорт остановлен из-за ошибки загрузки."
                    if job.progress.exported_files
                    else "❌ Экспорт не выполнен из-за ошибки загрузки."
                ),
            )
        finally:
            if archive is not None:
                archive.close()
            if current_download_path and os.path.exists(current_download_path):
                with contextlib.suppress(OSError):
                    os.remove(current_download_path)
            if archive_path and os.path.exists(archive_path):
                with contextlib.suppress(OSError):
                    os.remove(archive_path)
            if session is not None and not session.closed:
                await session.close()
            if tempdir and os.path.isdir(tempdir):
                shutil.rmtree(tempdir, ignore_errors=True)
                job.tempdir = None

    async def _resolve_post(self, post: dict[str, Any]) -> dict[str, Any]:
        if self._post_resolver is None:
            return post
        return await self._post_resolver(post)

    async def _finalize_part(
        self,
        job: ZipExportJob,
        title: str,
        archive: zipfile.ZipFile,
        archive_path: str,
        part_number: int,
    ) -> None:
        archive.close()
        actual_size = os.path.getsize(archive_path)
        if actual_size > self._part_limit_bytes:
            with contextlib.suppress(OSError):
                os.remove(archive_path)
            raise ZipExportLimitExceeded("part_size")
        if actual_size > self._max_temp_bytes:
            with contextlib.suppress(OSError):
                os.remove(archive_path)
            raise ZipExportLimitExceeded("temp_bytes")
        caption = f"📦 {title}, часть {part_number}"
        with open(archive_path, "rb") as document:
            async def send_part():
                document.seek(0)
                return await self._bot.send_document(
                    chat_id=job.chat_id,
                    document=document,
                    filename=os.path.basename(archive_path),
                    caption=caption,
                )

            await self._telegram_request(
                job.chat_id,
                send_part,
                operation_name="zip_send_document",
            )
        with contextlib.suppress(OSError):
            os.remove(archive_path)

    def _partial_limit_result(self, job: ZipExportJob, reason: str) -> ZipExportResult:
        return ZipExportResult(
            status="partial" if job.progress.exported_files else "limit",
            exported_files=job.progress.exported_files,
            skipped_files=job.progress.skipped_files,
            sent_parts=job.progress.sent_parts,
            downloaded_bytes=job.progress.downloaded_bytes,
            limit_reason=reason,
            message=self._limit_message(job, reason),
        )

    def _limit_message(self, job: ZipExportJob, reason: str) -> str:
        if reason == "file_count":
            return f"⚠️ Экспорт остановлен: достигнут лимит файлов ({self._max_files})."
        if reason == "file_size":
            return f"⚠️ Экспорт остановлен: файл превысил лимит ({_format_megabytes(self._max_file_bytes)})."
        if reason == "total_bytes":
            return f"⚠️ Экспорт остановлен: достигнут лимит скачанных данных ({_format_megabytes(self._max_total_bytes)})."
        if reason == "part_count":
            return f"⚠️ Экспорт остановлен: достигнут лимит ZIP-частей ({self._max_parts})."
        if reason == "temp_bytes":
            return f"⚠️ Экспорт остановлен: достигнут лимит временных данных ({_format_megabytes(self._max_temp_bytes)})."
        if reason == "timeout":
            return "⏱ Экспорт остановлен: превышено время выполнения."
        if reason == "part_size":
            return f"⚠️ Экспорт остановлен: файл не помещается в ZIP-часть ({_format_megabytes(self._part_limit_bytes)})."
        return "⚠️ Экспорт остановлен из-за лимита ресурсов."

    async def _finish_job(self, job: ZipExportJob, result: ZipExportResult) -> None:
        await self._safe_finish_status(job, result)
        async with self._condition:
            self._cleanup_job_registry(job)
            if job.source_kind == "favorites" and result.status in {"success", "partial"}:
                self._favorites_last_finished_at[job.user_id] = self._monotonic()
            self._condition.notify_all()

    async def _safe_finish_status(self, job: ZipExportJob, result: ZipExportResult) -> None:
        reporter = JobStatusReporter(
            self,
            job,
            progress_interval_seconds=self._progress_interval_seconds,
        )
        await reporter.finished(result)

    def _cleanup_job_registry(self, job: ZipExportJob) -> None:
        self._active_job_ids.discard(job.job_id)
        self._jobs_by_id.pop(job.job_id, None)
        if self._job_id_by_user.get(job.user_id) == job.job_id:
            self._job_id_by_user.pop(job.user_id, None)

    def _queue_position(self, job_id: str) -> int | None:
        for index, queued_job_id in enumerate(self._pending_job_ids, start=1):
            if queued_job_id == job_id:
                return index
        if job_id in self._active_job_ids:
            return 0
        return None

    def _cooldown_retry_after(self, user_id: int, now: float) -> int:
        last_finished_at = self._favorites_last_finished_at.get(user_id)
        if last_finished_at is None:
            return 0
        remaining = self._favorites_cooldown_seconds - (now - last_finished_at)
        if remaining <= 0:
            self._favorites_last_finished_at.pop(user_id, None)
            return 0
        return max(1, int(remaining))

    def _cleanup_expired_cooldowns(self, now: float) -> None:
        if not self._favorites_last_finished_at:
            return
        expired_user_ids = [
            user_id
            for user_id, last_finished_at in self._favorites_last_finished_at.items()
            if now - last_finished_at >= self._favorites_cooldown_seconds
        ]
        for user_id in expired_user_ids:
            self._favorites_last_finished_at.pop(user_id, None)
