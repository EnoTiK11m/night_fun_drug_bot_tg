import asyncio
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from telegram.error import RetryAfter, TimedOut

import bot_media
import bot_zip_export
from bot_media import DownloadedPhotoMeta
from bot_zip_export import (
    JobStatusReporter,
    ZipExportJob,
    ZipExportManager,
    ZipExportResult,
    ZipExportSource,
    _safe_zip_filename,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"payload"


class FakeSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeRateLimiter:
    def __init__(self):
        self.wait_calls = []
        self.retry_calls = []

    async def wait_for_slot(self, user_id):
        self.wait_calls.append(user_id)
        return True

    def apply_retry_after(self, user_id, error):
        self.retry_calls.append((user_id, error))
        return 0.0


def message(chat_id):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=chat_id + 1000)),
    )


def success_result():
    return ZipExportResult("success", 1, 0, 1, len(PNG), "success")


async def eventually(predicate, attempts=100):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


class ZipExportManagerTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self, **kwargs):
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
            edit_message_text=AsyncMock(),
            send_document=AsyncMock(),
        )
        defaults = dict(
            application=SimpleNamespace(bot=bot),
            source_loader=AsyncMock(return_value=ZipExportSource("saved", [])),
            session_factory=FakeSession,
            worker_count=1,
            queue_capacity=10,
            export_timeout_seconds=30,
            rate_limiter=FakeRateLimiter(),
        )
        defaults.update(kwargs)
        return ZipExportManager(**defaults)

    async def asyncTearDown(self):
        # Every test owns and stops its manager explicitly; this also exposes leaks.
        await asyncio.sleep(0)

    async def test_fifo_worker_limit_and_different_users(self):
        manager = self.make_manager(worker_count=2)
        started = []
        releases = {user: asyncio.Event() for user in (1, 2, 3)}

        async def run(job, _reporter):
            started.append(job.user_id)
            await releases[job.user_id].wait()
            return success_result()

        manager._run_job = run
        await manager.start()
        try:
            results = [await manager.enqueue_favorites(message(i), i) for i in (1, 2, 3)]
            self.assertEqual([r.status for r in results], ["queued"] * 3)
            await eventually(lambda: len(started) == 2)
            self.assertEqual(started, [1, 2])
            self.assertEqual(manager.stats(), {"queued": 1, "active": 2, "tracked_users": 3})
            releases[1].set()
            await eventually(lambda: len(started) == 3)
            self.assertEqual(started, [1, 2, 3])
            releases[2].set()
            releases[3].set()
            await eventually(lambda: manager.stats()["tracked_users"] == 0)
        finally:
            await manager.stop()

    async def test_bounded_queue_and_one_job_per_user(self):
        manager = self.make_manager(queue_capacity=2)
        first = await manager.enqueue_favorites(message(1), 1)
        duplicate = await manager.enqueue_collection(message(1), 1, 7)
        second = await manager.enqueue_favorites(message(2), 2)
        full = await manager.enqueue_favorites(message(3), 3)
        self.assertEqual(duplicate.status, "duplicate")
        self.assertEqual(duplicate.job_id, first.job_id)
        self.assertEqual(second.status, "queued")
        self.assertEqual(full.status, "queue_full")
        await manager.stop()

    async def test_cancel_queued_removes_registry_and_reports_cancel(self):
        manager = self.make_manager()
        queued = await manager.enqueue_favorites(message(7), 7)
        self.assertTrue(await manager.cancel_for_user(7, queued.job_id))
        self.assertEqual(manager.stats(), {"queued": 0, "active": 0, "tracked_users": 0})
        text = manager._bot.edit_message_text.await_args.kwargs["text"]
        self.assertIn("отмен", text.lower())
        await manager.stop()

    async def test_cancel_during_initial_queue_message_finishes_handshake(self):
        manager = self.make_manager()
        entered = asyncio.Event()
        release = asyncio.Event()
        run_job = AsyncMock(return_value=success_result())
        manager._run_job = run_job

        async def blocked_reply(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(message_id=777)

        pending_message = message(9)
        pending_message.reply_text.side_effect = blocked_reply
        await manager.start()
        try:
            enqueue_task = asyncio.create_task(
                manager.enqueue_favorites(pending_message, 9)
            )
            await entered.wait()
            self.assertTrue(await manager.cancel_for_user(9))
            self.assertEqual(manager.stats()["tracked_users"], 0)
            release.set()
            result = await enqueue_task
            self.assertEqual(result.status, "cancelled")
            run_job.assert_not_awaited()
            terminal = manager._bot.edit_message_text.await_args.kwargs["text"]
            self.assertIn("отмен", terminal.lower())
        finally:
            await manager.stop()

    async def test_cancel_active_cleans_temp_and_worker_continues(self):
        manager = self.make_manager()
        entered = asyncio.Event()
        cleanup_seen = asyncio.Event()
        calls = []

        async def run(job, _reporter):
            calls.append(job.user_id)
            if job.user_id == 1:
                job.tempdir = tempfile.mkdtemp(prefix="zip-test-active-")
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    Path(job.tempdir).rmdir()
                    job.tempdir = None
                    cleanup_seen.set()
            return success_result()

        manager._run_job = run
        await manager.start()
        try:
            first = await manager.enqueue_favorites(message(1), 1)
            await entered.wait()
            self.assertTrue(await manager.cancel_for_user(1, first.job_id))
            await cleanup_seen.wait()
            await eventually(lambda: manager.stats()["tracked_users"] == 0)
            await manager.enqueue_favorites(message(2), 2)
            await eventually(lambda: calls == [1, 2])
        finally:
            await manager.stop()

    async def test_repeated_job_cancellation_never_cancels_worker(self):
        manager = self.make_manager()
        entered_by_user = {user: asyncio.Event() for user in (1, 2, 3)}
        temp_paths = []

        async def run(job, _reporter):
            job.tempdir = tempfile.mkdtemp(prefix="zip-test-repeat-cancel-")
            temp_paths.append(job.tempdir)
            entered_by_user[job.user_id].set()
            try:
                await asyncio.Event().wait()
            finally:
                Path(job.tempdir).rmdir()
                job.tempdir = None

        manager._run_job = run
        await manager.start()
        worker = manager._worker_tasks[0]
        try:
            for user in (1, 2, 3):
                queued = await manager.enqueue_favorites(message(user), user)
                await entered_by_user[user].wait()
                self.assertTrue(await manager.cancel_for_user(user, queued.job_id))
                await eventually(lambda: manager.stats()["tracked_users"] == 0)
                self.assertFalse(worker.done())
                self.assertEqual(worker.cancelling(), 0)
            self.assertTrue(all(not os.path.exists(path) for path in temp_paths))
        finally:
            await manager.stop()

    async def test_completed_job_is_not_acknowledged_as_cancelled_during_terminal_status(self):
        manager = self.make_manager()
        terminal_status_started = asyncio.Event()
        release_terminal_status = asyncio.Event()

        async def run(_job, _reporter):
            return success_result()

        async def finish_status(_job, _result):
            terminal_status_started.set()
            await release_terminal_status.wait()

        manager._run_job = run
        manager._safe_finish_status = finish_status
        await manager.start()
        try:
            queued = await manager.enqueue_favorites(message(1), 1)
            await terminal_status_started.wait()
            job = manager._jobs_by_id[queued.job_id]
            self.assertTrue(job.task.done())
            self.assertFalse(await manager.cancel_for_user(1, queued.job_id))
            self.assertFalse(job.cancel_event.is_set())
            release_terminal_status.set()
            await eventually(lambda: manager.stats()["tracked_users"] == 0)
        finally:
            release_terminal_status.set()
            await manager.stop()

    async def test_shutdown_cancels_queued_and_active_and_cleans_registry(self):
        manager = self.make_manager()
        entered = asyncio.Event()
        temp_paths = []

        async def run(job, _reporter):
            job.tempdir = tempfile.mkdtemp(prefix="zip-test-stop-")
            temp_paths.append(job.tempdir)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                Path(job.tempdir).rmdir()
                job.tempdir = None

        manager._run_job = run
        await manager.start()
        await manager.enqueue_favorites(message(1), 1)
        await entered.wait()
        await manager.enqueue_favorites(message(2), 2)
        await manager.stop()
        self.assertEqual(manager.stats(), {"queued": 0, "active": 0, "tracked_users": 0})
        self.assertTrue(all(not os.path.exists(path) for path in temp_paths))
        self.assertEqual(manager._worker_tasks, [])

    async def test_worker_survives_exception_and_whole_job_timeout(self):
        manager = self.make_manager()
        outcomes = []

        async def finish(job, result):
            outcomes.append((job.user_id, result.status, result.limit_reason))
            async with manager._condition:
                manager._cleanup_job_registry(job)
                manager._condition.notify_all()

        async def run(job, _reporter):
            if job.user_id == 1:
                raise RuntimeError("boom")
            if job.user_id == 2:
                raise TimeoutError
            return success_result()

        manager._finish_job = finish
        manager._run_job = run
        await manager.start()
        try:
            for user in (1, 2, 3):
                await manager.enqueue_favorites(message(user), user)
            await eventually(lambda: len(outcomes) == 3)
            self.assertEqual(outcomes, [(1, "failed", None), (2, "limit", "timeout"), (3, "success", None)])
        finally:
            await manager.stop()

    async def test_ambiguous_document_timeout_is_not_retried_and_sets_cooldown(self):
        source = ZipExportSource("saved", [{"id": 1, "file_url": "a"}])
        manager = self.make_manager(
            source_loader=AsyncMock(return_value=source),
            favorites_cooldown_seconds=60,
        )
        manager._bot.send_document.side_effect = TimedOut()

        async def download(_url, path, **_kwargs):
            Path(path).write_bytes(PNG)
            return DownloadedPhotoMeta("u", "image/png", ".png", len(PNG), "x")

        await manager.start()
        try:
            with patch("bot_zip_export.download_photo_to_path", side_effect=download):
                queued = await manager.enqueue_favorites(message(1), 1)
                self.assertEqual(queued.status, "queued")
                await eventually(lambda: manager.stats()["tracked_users"] == 0)

            self.assertEqual(manager._bot.send_document.await_count, 1)
            terminal_texts = [
                call.kwargs["text"] for call in manager._bot.edit_message_text.await_args_list
            ]
            self.assertTrue(any("не подтвердил" in text for text in terminal_texts))
            retry = await manager.enqueue_favorites(message(1), 1)
            self.assertEqual(retry.status, "cooldown")
        finally:
            await manager.stop()


class ZipExportJobTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self, posts, **limits):
        self.sessions = []
        self.sent_archives = []

        def session_factory():
            session = FakeSession()
            self.sessions.append(session)
            return session

        async def send_document(**kwargs):
            document = kwargs["document"]
            archive_path = document.name
            with zipfile.ZipFile(document) as archive:
                self.sent_archives.append((archive_path, archive.namelist(), archive.read(archive.namelist()[0])))

        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            edit_message_text=AsyncMock(),
            send_document=AsyncMock(side_effect=send_document),
        )
        defaults = dict(
            application=SimpleNamespace(bot=bot),
            source_loader=AsyncMock(return_value=ZipExportSource("title", posts)),
            session_factory=session_factory,
            max_files=20,
            max_file_bytes=100,
            max_total_bytes=1000,
            part_limit_bytes=1000,
            max_parts=10,
            max_temp_bytes=1000,
            rate_limiter=FakeRateLimiter(),
        )
        defaults.update(limits)
        manager = ZipExportManager(**defaults)
        job = ZipExportJob("job", 10, 10, "favorites", None, "title", 0)
        reporter = JobStatusReporter(manager, job, progress_interval_seconds=100)
        return manager, job, reporter

    async def run_with_payloads(self, posts, payloads, **limits):
        manager, job, reporter = self.make_manager(posts, **limits)
        calls = 0

        async def download(_url, path, **kwargs):
            nonlocal calls
            payload, extension = payloads[calls]
            calls += 1
            kwargs["byte_budget"].consume(len(payload))
            Path(path).write_bytes(payload)
            return DownloadedPhotoMeta("https://cdn/x", "image/png", extension, len(payload), "x")

        with patch("bot_zip_export.download_photo_to_path", side_effect=download):
            result = await manager._run_job(job, reporter)
        return manager, job, result

    async def test_success_safe_signature_extension_filename_no_traversal_and_cleanup(self):
        posts = [{"id": "../../evil", "file_url": "https://cdn/wrong.exe"}]
        manager, job, result = await self.run_with_payloads(posts, [(PNG, ".png")])
        self.assertEqual(result.status, "success")
        self.assertEqual(len(self.sessions), 1)
        self.assertTrue(self.sessions[0].closed)
        archive_path, names, content = self.sent_archives[0]
        self.assertEqual(names, ["00001_evil.png"])
        self.assertEqual(content, PNG)
        self.assertFalse(os.path.exists(archive_path), "sent ZIP part must be deleted immediately")
        self.assertIsNone(job.tempdir)
        self.assertEqual(_safe_zip_filename(2, "../x", ".exe"), "00002_x.jpg")

    async def test_max_files_and_distinct_success_partial_limit_download_error_texts(self):
        posts = [{"id": 1, "file_url": "u1"}, {"id": 2}, {"id": 3, "file_url": "u3"}]
        _manager, _job, limited_partial = await self.run_with_payloads(posts, [(PNG, ".png")], max_files=1)
        self.assertEqual((limited_partial.status, limited_partial.limit_reason), ("partial", "file_count"))

        _manager, _job, partial = await self.run_with_payloads(
            [{"id": 1, "file_url": "u"}, {"id": 2}], [(PNG, ".png")]
        )
        self.assertEqual(partial.status, "partial")

        _manager, _job, limit = await self.run_with_payloads(
            [{"id": 1, "file_url": "u"}], [(b"12345", ".jpg")], part_limit_bytes=4
        )
        self.assertEqual(limit.status, "limit")

        manager, job, reporter = self.make_manager([])
        empty = await manager._run_job(job, reporter)
        self.assertEqual(empty.status, "download_error")
        self.assertNotEqual(empty.message, partial.message)

        _manager, _job, success = await self.run_with_payloads([{"id": 1, "file_url": "u"}], [(PNG, ".png")])
        self.assertEqual(success.status, "success")
        self.assertEqual(len({success.message, partial.message, limit.message, empty.message}), 4)

    async def test_per_file_and_total_byte_limits_use_actual_downloaded_bytes(self):
        post = [{"id": 1, "file_url": "u"}]
        manager, job, reporter = self.make_manager(post, max_file_bytes=8)

        async def oversize(_url, path, **kwargs):
            self.assertEqual(kwargs["max_bytes"], 8)
            raise ValueError("actual stream exceeded max")

        with patch("bot_zip_export.download_photo_to_path", side_effect=oversize):
            per_file = await manager._run_job(job, reporter)
        self.assertEqual(per_file.status, "download_error")

        posts = [{"id": 1, "file_url": "a"}, {"id": 2, "file_url": "b"}]
        _manager, _job, total = await self.run_with_payloads(
            posts, [(b"12345", ".jpg"), (b"67890", ".jpg")], max_file_bytes=5, max_total_bytes=8
        )
        self.assertEqual((total.status, total.limit_reason, total.downloaded_bytes), ("partial", "total_bytes", 10))

    async def test_invalid_and_partial_downloads_consume_total_budget(self):
        posts = [{"id": index, "file_url": str(index)} for index in range(3)]
        manager, job, reporter = self.make_manager(
            posts, max_file_bytes=6, max_total_bytes=10
        )
        calls = 0

        async def invalid(_url, _path, **kwargs):
            nonlocal calls
            calls += 1
            kwargs["byte_budget"].consume(6)
            raise ValueError("invalid signature")

        with patch("bot_zip_export.download_photo_to_path", side_effect=invalid):
            result = await manager._run_job(job, reporter)
        self.assertEqual(calls, 2)
        self.assertEqual(
            (result.status, result.limit_reason, result.downloaded_bytes),
            ("limit", "total_bytes", 12),
        )

        manager, job, reporter = self.make_manager(
            posts[:1], max_file_bytes=10, max_total_bytes=20
        )

        async def partial(_url, path, **kwargs):
            kwargs["byte_budget"].consume(4)
            Path(path).write_bytes(b"part")
            raise aiohttp.ClientError("connection lost")

        with patch("bot_zip_export.download_photo_to_path", side_effect=partial):
            partial_result = await manager._run_job(job, reporter)
        self.assertEqual(partial_result.status, "download_error")
        self.assertEqual(partial_result.downloaded_bytes, 4)

    async def test_file_limit_exact_budget_and_cancelled_bytes_are_distinct(self):
        post = [{"id": 1, "file_url": "a"}]
        manager, job, reporter = self.make_manager(
            post, max_file_bytes=5, max_total_bytes=20
        )

        async def file_too_large(_url, _path, **kwargs):
            kwargs["byte_budget"].consume(6)
            raise bot_media.FileDownloadLimitExceeded("file")

        with patch("bot_zip_export.download_photo_to_path", side_effect=file_too_large):
            file_result = await manager._run_job(job, reporter)
        self.assertEqual(
            (file_result.status, file_result.limit_reason, file_result.downloaded_bytes),
            ("limit", "file_size", 6),
        )

        manager, job, reporter = self.make_manager(
            post, max_file_bytes=6, max_total_bytes=6
        )

        async def exact(_url, path, **kwargs):
            kwargs["byte_budget"].consume(6)
            Path(path).write_bytes(b"123456")
            return DownloadedPhotoMeta("u", "image/png", ".png", 6, "x")

        with patch("bot_zip_export.download_photo_to_path", side_effect=exact):
            exact_result = await manager._run_job(job, reporter)
        self.assertEqual((exact_result.status, exact_result.downloaded_bytes), ("success", 6))

        manager, job, reporter = self.make_manager(
            post, max_file_bytes=10, max_total_bytes=20
        )

        async def cancelled(_url, path, **kwargs):
            kwargs["byte_budget"].consume(4)
            Path(path).write_bytes(b"part")
            raise asyncio.CancelledError

        with patch("bot_zip_export.download_photo_to_path", side_effect=cancelled):
            with self.assertRaises(asyncio.CancelledError):
                await manager._run_job(job, reporter)
        self.assertEqual(job.progress.downloaded_bytes, 4)
        self.assertIsNone(job.tempdir)

    async def test_part_bytes_part_count_and_temp_disk_limits(self):
        posts = [{"id": 1, "file_url": "a"}, {"id": 2, "file_url": "b"}]
        _m, _j, too_large = await self.run_with_payloads(posts[:1], [(b"12345", ".jpg")], part_limit_bytes=4)
        self.assertEqual((too_large.status, too_large.limit_reason), ("limit", "part_size"))

        _m, _j, parts = await self.run_with_payloads(
            posts, [(b"1234", ".jpg"), (b"5678", ".jpg")],
            part_limit_bytes=700, max_parts=1,
        )
        self.assertEqual((parts.status, parts.limit_reason, parts.sent_parts), ("partial", "part_count", 1))

        _m, _j, temp = await self.run_with_payloads(
            posts, [(b"1234", ".jpg"), (b"5678", ".jpg")],
            part_limit_bytes=2000, max_temp_bytes=700,
        )
        self.assertEqual((temp.status, temp.limit_reason), ("partial", "temp_bytes"))

    async def test_partial_and_download_error_paths_close_one_session_and_delete_temp(self):
        posts = [
            {"id": 1, "file_url": "a"},
            {"id": 2, "file_url": "b"},
            {"id": 3, "file_url": "c"},
        ]
        manager, job, reporter = self.make_manager(posts)
        calls = 0

        async def download(_url, path, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                Path(path).write_bytes(b"partial")
                raise aiohttp.ClientError("broken")
            Path(path).write_bytes(PNG)
            return DownloadedPhotoMeta("u", "image/png", ".png", len(PNG), "x")

        with patch("bot_zip_export.download_photo_to_path", side_effect=download):
            result = await manager._run_job(job, reporter)
        self.assertEqual(result.status, "partial")
        self.assertEqual((result.exported_files, result.skipped_files), (2, 1))
        self.assertEqual(len(self.sessions), 1)
        self.assertTrue(self.sessions[0].closed)
        self.assertIsNone(job.tempdir)

    async def test_cancel_text_is_distinct_and_run_job_cleans_stream_file(self):
        manager, job, reporter = self.make_manager([{"id": 1, "file_url": "u"}])

        async def cancelled(_url, path, **_kwargs):
            Path(path).write_bytes(b"partial")
            raise asyncio.CancelledError

        with patch("bot_zip_export.download_photo_to_path", side_effect=cancelled):
            with self.assertRaises(asyncio.CancelledError):
                await manager._run_job(job, reporter)
        self.assertIsNone(job.tempdir)
        self.assertTrue(self.sessions[0].closed)

    async def test_progress_updates_are_throttled_but_forced_terminal_is_sent(self):
        manager, job, _reporter = self.make_manager([])
        reporter = JobStatusReporter(manager, job, progress_interval_seconds=10)
        job.status_message_id = 5
        with patch("bot_zip_export.time.monotonic", side_effect=[100.0, 101.0, 102.0]):
            await reporter.progress()
            job.progress.exported_files = 1
            await reporter.progress()
            await reporter.finished(success_result())
        self.assertEqual(manager._bot.edit_message_text.await_count, 2)
        self.assertNotEqual(
            manager._bot.edit_message_text.await_args_list[0].kwargs["text"],
            manager._bot.edit_message_text.await_args_list[1].kwargs["text"],
        )

    async def test_telegram_retry_after_retries_document_and_terminal_status(self):
        manager, job, reporter = self.make_manager([{"id": 1, "file_url": "a"}])
        manager._bot.send_document.side_effect = [RetryAfter(0), None]

        async def download(_url, path, **_kwargs):
            Path(path).write_bytes(PNG)
            return DownloadedPhotoMeta("u", "image/png", ".png", len(PNG), "x")

        with patch("bot_zip_export.download_photo_to_path", side_effect=download):
            result = await manager._run_job(job, reporter)

        self.assertEqual(result.status, "success")
        self.assertEqual(manager._bot.send_document.await_count, 2)
        self.assertEqual(len(manager._rate_limiter.retry_calls), 1)

        job.status_message_id = 55
        manager._bot.edit_message_text.side_effect = [RetryAfter(0), None]
        await reporter.finished(result)
        self.assertEqual(manager._bot.edit_message_text.await_count, 2)
        self.assertEqual(len(manager._rate_limiter.retry_calls), 2)


class FakeContent:
    def __init__(self, chunks, entered=None, release=None):
        self.chunks = chunks
        self.entered = entered
        self.release = release

    async def iter_chunked(self, _size):
        if self.entered:
            self.entered.set()
        if self.release:
            await self.release.wait()
        for chunk in self.chunks:
            yield chunk


class PartiallyBlockingContent:
    def __init__(self, entered):
        self.entered = entered

    async def iter_chunked(self, _size):
        yield PNG[:8]
        self.entered.set()
        await asyncio.Event().wait()


class ErrorAfterChunksContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk
        raise aiohttp.ClientError("stream interrupted")


class FakeResponse:
    status = 200

    def __init__(self, chunks, **headers):
        self.headers = headers or {"Content-Type": "image/png"}
        self.content = FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        return None


class HttpSession:
    def __init__(self, response):
        self.response = response
        self.closed = False

    def get(self, *_args, **_kwargs):
        return self.response

    async def close(self):
        self.closed = True


class ZipExportStreamingBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_signature_controls_extension_and_oversize_stream_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "download.bin")
            session = HttpSession(FakeResponse([PNG], **{"Content-Type": "image/jpeg"}))
            with patch("bot_media._validate_public_photo_url", AsyncMock()):
                meta = await bot_media.download_photo_to_path("https://cdn/x.jpg", path, session=session)
            self.assertEqual(meta.extension, ".png")

            session = HttpSession(FakeResponse([PNG]))
            with patch("bot_media._validate_public_photo_url", AsyncMock()):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    await bot_media.download_photo_to_path("https://cdn/x", path, session=session, max_bytes=4)
            self.assertFalse(os.path.exists(path))

    async def test_stream_budget_counts_invalid_partial_exact_and_file_limit_bytes(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "bot_media._validate_public_photo_url", AsyncMock()
        ):
            invalid_budget = bot_media.DownloadByteBudget(100)
            invalid_path = os.path.join(directory, "invalid")
            with self.assertRaisesRegex(ValueError, "not a supported"):
                await bot_media.download_photo_to_path(
                    "https://cdn/invalid",
                    invalid_path,
                    session=HttpSession(FakeResponse([b"not-an-image"])),
                    byte_budget=invalid_budget,
                )
            self.assertEqual(invalid_budget.consumed, len(b"not-an-image"))

            partial_budget = bot_media.DownloadByteBudget(100)
            partial_response = FakeResponse([])
            partial_response.content = ErrorAfterChunksContent([b"partial"])
            partial_path = os.path.join(directory, "partial")
            with self.assertRaises(aiohttp.ClientError):
                await bot_media.download_photo_to_path(
                    "https://cdn/partial",
                    partial_path,
                    session=HttpSession(partial_response),
                    byte_budget=partial_budget,
                )
            self.assertEqual(partial_budget.consumed, len(b"partial"))
            self.assertFalse(os.path.exists(partial_path))

            exact_budget = bot_media.DownloadByteBudget(len(PNG))
            exact_path = os.path.join(directory, "exact")
            await bot_media.download_photo_to_path(
                "https://cdn/exact",
                exact_path,
                session=HttpSession(FakeResponse([PNG])),
                byte_budget=exact_budget,
            )
            self.assertEqual(exact_budget.consumed, len(PNG))

            file_budget = bot_media.DownloadByteBudget(100)
            file_path = os.path.join(directory, "file-limit")
            with self.assertRaises(bot_media.FileDownloadLimitExceeded):
                await bot_media.download_photo_to_path(
                    "https://cdn/file-limit",
                    file_path,
                    session=HttpSession(FakeResponse([PNG])),
                    max_bytes=4,
                    byte_budget=file_budget,
                )
            self.assertEqual(file_budget.consumed, len(PNG))

    async def test_global_download_semaphore_serializes_and_cancel_cleans_partial(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        first_response = FakeResponse([PNG])
        first_response.content = FakeContent([PNG], entered, release)
        second_response = FakeResponse([PNG])
        second_entered = asyncio.Event()
        second_response.content = FakeContent([PNG], second_entered, None)
        semaphore = asyncio.Semaphore(1)

        with tempfile.TemporaryDirectory() as directory, patch(
            "bot_media._validate_public_photo_url", AsyncMock()
        ), patch("bot_media.global_download_semaphore", semaphore):
            first = asyncio.create_task(bot_media.download_photo_to_path(
                "https://cdn/1", os.path.join(directory, "one"), session=HttpSession(first_response)
            ))
            await entered.wait()
            second_path = os.path.join(directory, "two")
            second = asyncio.create_task(bot_media.download_photo_to_path(
                "https://cdn/2", second_path, session=HttpSession(second_response)
            ))
            await asyncio.sleep(0)
            self.assertFalse(second_entered.is_set())
            second.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await second
            self.assertFalse(os.path.exists(second_path))
            release.set()
            await first

            cancel_entered = asyncio.Event()
            cancel_response = FakeResponse([])
            cancel_response.content = PartiallyBlockingContent(cancel_entered)
            cancel_path = os.path.join(directory, "cancelled")
            cancel_budget = bot_media.DownloadByteBudget(100)
            streaming = asyncio.create_task(bot_media.download_photo_to_path(
                "https://cdn/3", cancel_path, session=HttpSession(cancel_response),
                byte_budget=cancel_budget,
            ))
            await cancel_entered.wait()
            streaming.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await streaming
            self.assertFalse(os.path.exists(cancel_path))
            self.assertEqual(cancel_budget.consumed, len(PNG[:8]))


if __name__ == "__main__":
    unittest.main()
