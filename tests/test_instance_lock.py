import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import app.telegram.application as bot
import app.infrastructure.instance_lock as bot_instance_lock
from app.infrastructure.instance_lock import (
    BotInstanceLifecycle,
    InstanceLock,
    InstanceLockBusy,
    InstanceLockLifecycleError,
    OrphanCleanupResult,
    OrphanCleanupTarget,
    StartupCheck,
    StartupChecksResult,
    cleanup_orphan_temp_files,
    lock_path_for_database,
    run_startup_checks,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_lifecycle(lock, *, checks=None, cleanup=None, wait=0.0, retry=0.02):
    return BotInstanceLifecycle(
        lock,
        wait_seconds=wait,
        retry_interval_seconds=retry,
        startup_checks=checks or (lambda: StartupChecksResult((StartupCheck("ok", True),))),
        orphan_cleanup=cleanup or (lambda: OrphanCleanupResult(0, 0, 0)),
    )


def old(path: Path, *, seconds=3600) -> None:
    timestamp = time.time() - seconds
    os.utime(path, (timestamp, timestamp))


class InstanceLockAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="instance_lock_test_")
        self.root = Path(self.temporary.name)
        self.lock_path = self.root / "instance.lock"

    def tearDown(self):
        self.temporary.cleanup()

    async def test_first_instance_acquires_and_second_is_busy(self):
        first = InstanceLock(self.lock_path)
        second = InstanceLock(self.lock_path)
        await first.acquire(wait_seconds=0)
        self.assertTrue(first.held)
        with self.assertRaises(InstanceLockBusy):
            await second.acquire(wait_seconds=0)
        self.assertFalse(second.held)
        self.assertIsNone(second._file)
        first.release()

    async def test_different_directories_can_run_together(self):
        first = InstanceLock(self.root / "a" / "instance.lock")
        second = InstanceLock(self.root / "b" / "instance.lock")
        await first.acquire(wait_seconds=0)
        await second.acquire(wait_seconds=0)
        self.assertTrue(first.held and second.held)
        first.release()
        second.release()

    async def test_release_allows_second_instance_and_double_release_is_safe(self):
        first = InstanceLock(self.lock_path)
        second = InstanceLock(self.lock_path)
        await first.acquire(wait_seconds=0)
        first.release()
        first.release()
        await second.acquire(wait_seconds=0)
        self.assertTrue(second.held)
        second.release()

    async def test_non_owner_release_cannot_unlock_owner(self):
        first = InstanceLock(self.lock_path)
        non_owner = InstanceLock(self.lock_path)
        probe = InstanceLock(self.lock_path)
        await first.acquire(wait_seconds=0)
        non_owner.release()
        with self.assertRaises(InstanceLockBusy):
            await probe.acquire(wait_seconds=0)
        first.release()

    async def test_bounded_wait_acquires_after_release(self):
        first = InstanceLock(self.lock_path)
        second = InstanceLock(self.lock_path)
        await first.acquire(wait_seconds=0)

        async def release_soon():
            await asyncio.sleep(0.08)
            first.release()

        release_task = asyncio.create_task(release_soon())
        wait_ms = await second.acquire(wait_seconds=1, retry_interval_seconds=0.02)
        await release_task
        self.assertGreaterEqual(wait_ms, 40)
        self.assertTrue(second.held)
        second.release()

    async def test_bounded_wait_times_out_and_closes_descriptor(self):
        first = InstanceLock(self.lock_path)
        second = InstanceLock(self.lock_path)
        await first.acquire(wait_seconds=0)
        started = time.monotonic()
        with self.assertRaises(InstanceLockBusy):
            await second.acquire(wait_seconds=0.08, retry_interval_seconds=0.02)
        self.assertGreaterEqual(time.monotonic() - started, 0.05)
        self.assertIsNone(second._file)
        first.release()

    async def test_cancellation_while_waiting_closes_resources(self):
        first = InstanceLock(self.lock_path)
        waiting = InstanceLock(self.lock_path)
        await first.acquire(wait_seconds=0)
        task = asyncio.create_task(
            waiting.acquire(wait_seconds=10, retry_interval_seconds=0.02)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(waiting._file)
        self.assertFalse(waiting.held)
        first.release()
        probe = InstanceLock(self.lock_path)
        await probe.acquire(wait_seconds=0)
        probe.release()

    async def test_startup_exception_releases_lock(self):
        lock = InstanceLock(self.lock_path)

        def fail():
            raise OSError("startup failed")

        lifecycle = make_lifecycle(lock, checks=fail)
        with self.assertRaises(InstanceLockLifecycleError):
            await lifecycle.start()
        self.assertFalse(lock.held)
        probe = InstanceLock(self.lock_path)
        await probe.acquire(wait_seconds=0)
        probe.release()

    async def test_cancelled_start_releases_lock(self):
        lock = InstanceLock(self.lock_path)

        def cancel():
            raise asyncio.CancelledError

        lifecycle = make_lifecycle(lock, checks=cancel)
        with self.assertRaises(asyncio.CancelledError):
            await lifecycle.start()
        self.assertFalse(lock.held)

    async def test_metadata_is_safe_and_only_owner_updates_it(self):
        first = InstanceLock(self.lock_path, working_directory=self.root)
        second = InstanceLock(self.lock_path, working_directory=self.root / "other")
        with patch.dict(
            os.environ,
            {"BOT_TOKEN": "secret-token", "PRIVATE_ENV_VALUE": "secret-env"},
        ):
            await first.acquire(wait_seconds=0)
        metadata = first.read_metadata()
        encoded = json.dumps(metadata)
        self.assertEqual(metadata["pid"], os.getpid())
        self.assertNotIn("secret-token", encoded)
        self.assertNotIn("secret-env", encoded)
        self.assertEqual(metadata["working_directory"], str(self.root.resolve()))
        with self.assertRaises(InstanceLockBusy):
            await second.acquire(wait_seconds=0)
        self.assertEqual(first.read_metadata(), metadata)
        first.release()

    async def test_reentrant_acquire_is_typed_error(self):
        lock = InstanceLock(self.lock_path)
        await lock.acquire(wait_seconds=0)
        with self.assertRaises(InstanceLockLifecycleError):
            await lock.acquire(wait_seconds=0)
        lock.release()


class InstanceLockSyncLifecycleTests(unittest.TestCase):
    def test_same_and_new_object_reacquire_in_new_event_loops(self):
        with tempfile.TemporaryDirectory(prefix="instance_loop_test_") as directory:
            path = Path(directory) / "instance.lock"
            first = InstanceLock(path)
            asyncio.run(first.acquire(wait_seconds=0))
            first.release()
            asyncio.run(first.acquire(wait_seconds=0))
            first.release()
            second = InstanceLock(path)
            asyncio.run(second.acquire(wait_seconds=0))
            second.release()

    def test_lock_path_is_bound_to_absolute_database(self):
        with tempfile.TemporaryDirectory(prefix="instance_path_test_") as directory:
            root = Path(directory)
            self.assertEqual(
                lock_path_for_database("db.sqlite", working_directory=root),
                lock_path_for_database(root / "db.sqlite", working_directory=root / "other"),
            )
            self.assertNotEqual(
                lock_path_for_database("db.sqlite", working_directory=root / "one"),
                lock_path_for_database("db.sqlite", working_directory=root / "two"),
            )

    def test_runner_exception_and_post_init_cancellation_release_lock(self):
        with tempfile.TemporaryDirectory(prefix="instance_runner_test_") as directory:
            path = Path(directory) / "instance.lock"
            for error in (RuntimeError("build failed"), asyncio.CancelledError()):
                lock = InstanceLock(path)
                lifecycle = make_lifecycle(lock)

                def fail(error=error):
                    raise error

                with self.assertRaises(type(error)):
                    bot.run_with_instance_lifecycle(lifecycle, fail)
                self.assertFalse(lock.held)

    def test_actual_post_init_cancellation_releases_instance_lock(self):
        with tempfile.TemporaryDirectory(prefix="instance_post_init_cancel_") as directory:
            lock = InstanceLock(Path(directory) / "instance.lock")
            lifecycle = make_lifecycle(lock)
            telegram_bot = SimpleNamespace(
                delete_my_commands=AsyncMock(side_effect=asyncio.CancelledError)
            )
            application = SimpleNamespace(bot=telegram_bot)

            def cancelled_post_init():
                asyncio.run(bot.post_init(application))

            with (
                patch.object(bot.user_operation_gate, "start", AsyncMock()),
                self.assertRaises(asyncio.CancelledError),
            ):
                bot.run_with_instance_lifecycle(lifecycle, cancelled_post_init)
            self.assertFalse(lock.held)

    def test_sqlite_initialization_failure_releases_instance_lock(self):
        with tempfile.TemporaryDirectory(prefix="instance_sqlite_failure_") as directory:
            lock = InstanceLock(Path(directory) / "instance.lock")
            lifecycle = make_lifecycle(lock)
            telegram_bot = SimpleNamespace(
                delete_my_commands=AsyncMock(),
                set_my_commands=AsyncMock(),
            )
            application = SimpleNamespace(bot=telegram_bot)

            def failed_post_init():
                asyncio.run(bot.post_init(application))

            with (
                patch.object(bot.user_operation_gate, "start", AsyncMock()),
                patch.object(bot, "init_db", AsyncMock(side_effect=OSError("db failed"))),
                self.assertRaises(OSError),
            ):
                bot.run_with_instance_lifecycle(lifecycle, failed_post_init)
            self.assertFalse(lock.held)

    def test_failed_startup_checks_do_not_run_workers(self):
        with tempfile.TemporaryDirectory(prefix="instance_failed_checks_") as directory:
            path = Path(directory) / "instance.lock"
            checks = lambda: StartupChecksResult((StartupCheck("sqlite", False),))
            lifecycle = make_lifecycle(InstanceLock(path), checks=checks)
            runner = Mock()
            with self.assertRaises(InstanceLockLifecycleError):
                bot.run_with_instance_lifecycle(lifecycle, runner)
            runner.assert_not_called()
            self.assertFalse(lifecycle.lock.held)

    def test_successful_empty_startup_and_shutdown(self):
        with tempfile.TemporaryDirectory(prefix="instance_empty_startup_") as directory:
            lifecycle = make_lifecycle(InstanceLock(Path(directory) / "instance.lock"))
            runner = Mock()
            bot.run_with_instance_lifecycle(lifecycle, runner)
            runner.assert_called_once_with()
            self.assertFalse(lifecycle.lock.held)


class StartupChecksAndCleanupTests(unittest.TestCase):
    def test_startup_checks_accept_empty_database_and_required_directories(self):
        with tempfile.TemporaryDirectory(prefix="startup_checks_") as directory:
            root = Path(directory)
            result = run_startup_checks(
                working_directory=root,
                database_path="data/bot.db",
                callback_database_path="data/bot.db",
                zip_temp_root=root / "zip",
                backup_directory=root / "backups",
            )
            self.assertTrue(result.ok, result)
            self.assertGreater(result.free_bytes or 0, 0)

    def test_startup_checks_detect_unavailable_database_directory(self):
        with tempfile.TemporaryDirectory(prefix="startup_checks_bad_") as directory:
            root = Path(directory)
            blocker = root / "blocker"
            blocker.write_text("not a directory", encoding="utf-8")
            result = run_startup_checks(
                working_directory=root,
                database_path=blocker / 'app.telegram.application.db',
                callback_database_path=blocker / "callbacks.db",
                zip_temp_root=root,
                backup_directory=root / "backups",
            )
            self.assertFalse(result.ok)
            self.assertIn("sqlite_directory", result.failed_names)
            self.assertIn("callback_payload_database", result.failed_names)

    def test_cleanup_removes_only_old_prefixed_objects_and_keeps_fresh(self):
        with tempfile.TemporaryDirectory(prefix="orphan_cleanup_") as directory:
            root = Path(directory)
            old_dir = root / "zip_export_old"
            old_dir.mkdir()
            (old_dir / "part.zip").write_bytes(b"zip")
            old(old_dir)
            fresh_dir = root / "zip_export_fresh"
            fresh_dir.mkdir()
            unrelated = root / "unrelated_old"
            unrelated.mkdir()
            old(unrelated)
            old_file = root / "update_restart.tmp.old"
            old_file.write_bytes(b"tmp")
            old(old_file)
            targets = (
                OrphanCleanupTarget(root, "zip_export_", True),
                OrphanCleanupTarget(root, "update_restart.tmp", False),
            )
            result = cleanup_orphan_temp_files(targets, ttl_seconds=60, batch_size=10)
            self.assertEqual(result.deleted, 2)
            self.assertFalse(old_dir.exists())
            self.assertFalse(old_file.exists())
            self.assertTrue(fresh_dir.exists())
            self.assertTrue(unrelated.exists())

    def test_reparse_candidate_is_not_deleted_or_traversed(self):
        with tempfile.TemporaryDirectory(prefix="orphan_reparse_") as directory:
            root = Path(directory)
            candidate = root / "zip_export_link"
            candidate.mkdir()
            marker = candidate / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            old(candidate)
            original = bot_instance_lock._is_reparse_point

            def is_reparse(path):
                return path == candidate or original(path)

            with patch.object(bot_instance_lock, "_is_reparse_point", side_effect=is_reparse):
                result = cleanup_orphan_temp_files(
                    (OrphanCleanupTarget(root, "zip_export_", True),),
                    ttl_seconds=60,
                    batch_size=10,
                )
            self.assertEqual(result.skipped_reparse_points, 1)
            self.assertTrue(marker.exists())

    def test_cleanup_batch_is_bounded(self):
        with tempfile.TemporaryDirectory(prefix="orphan_batch_") as directory:
            root = Path(directory)
            for index in range(5):
                candidate = root / f"zip_export_{index}"
                candidate.mkdir()
                old(candidate, seconds=3600 + index)
            result = cleanup_orphan_temp_files(
                (OrphanCleanupTarget(root, "zip_export_", True),),
                ttl_seconds=60,
                batch_size=2,
            )
            self.assertEqual(result.deleted, 2)
            self.assertEqual(len(tuple(root.iterdir())), 3)


class InstanceLockSubprocessTests(unittest.TestCase):
    def _environment(self):
        environment = os.environ.copy()
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(PROJECT_ROOT) + (os.pathsep + existing if existing else "")
        return environment

    def _command(self, lock_path: Path, hold_seconds: float, crash: bool = False):
        source = (
            "import asyncio, os, sys, time\nfrom app.infrastructure.instance_lock import InstanceLock, InstanceLockBusy\np = sys.argv[1]\nhold = float(sys.argv[2])\nasync def go():\n l=InstanceLock(p)\n try:\n  await l.acquire(wait_seconds=0)\n except InstanceLockBusy:\n  print('BUSY',flush=True);return\n print('OWNED',flush=True)\n"
            + (" os._exit(17)\n" if crash else " time.sleep(hold);l.release()\n")
            + "asyncio.run(go())"
        )
        return [sys.executable, "-c", source, str(lock_path), str(hold_seconds)]

    def test_crashed_subprocess_releases_os_lock(self):
        with tempfile.TemporaryDirectory(prefix="instance_crash_") as directory:
            path = Path(directory) / "instance.lock"
            process = subprocess.run(
                self._command(path, 0, crash=True),
                cwd=PROJECT_ROOT,
                env=self._environment(),
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(process.returncode, 17, process.stderr)
            self.assertIn("OWNED", process.stdout)
            lock = InstanceLock(path)
            asyncio.run(lock.acquire(wait_seconds=0))
            lock.release()

    def test_two_simultaneous_subprocesses_have_exactly_one_owner(self):
        with tempfile.TemporaryDirectory(prefix="instance_race_") as directory:
            path = Path(directory) / "instance.lock"
            command = self._command(path, 1.0)
            first = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=self._environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            second = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=self._environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            first_output, first_error = first.communicate(timeout=15)
            second_output, second_error = second.communicate(timeout=15)
            outputs = (first_output.strip(), second_output.strip())
            self.assertEqual(outputs.count("OWNED"), 1, (outputs, first_error, second_error))
            self.assertEqual(outputs.count("BUSY"), 1, (outputs, first_error, second_error))


if __name__ == "__main__":
    unittest.main()
