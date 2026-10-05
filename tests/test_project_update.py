import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import app.telegram.application as bot
import app.infrastructure.project_update as project_update


OLD_COMMIT = "a" * 40
NEW_COMMIT = "b" * 40


def admin_update(user_id=1, chat_type="private"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id, type=chat_type),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )


class FakeGit:
    def __init__(self, *, dirty="", fail_stage=None, dependencies=False):
        self.dirty = dirty
        self.fail_stage = fail_stage
        self.dependencies = dependencies
        self.calls = []
        self.pulled = False

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if args[:3] == ("git", "rev-parse", "--is-inside-work-tree"):
            return project_update.CommandResult(0, "true", "")
        if args[:3] == ("git", "status", "--porcelain"):
            return project_update.CommandResult(0, self.dirty, "")
        if args[:2] == ("git", "fetch"):
            if self.fail_stage == "fetch":
                return project_update.CommandResult(1, "", "private remote error")
            return project_update.CommandResult(0, "", "")
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return project_update.CommandResult(
                0, NEW_COMMIT if self.pulled else OLD_COMMIT, ""
            )
        if args[:3] == ("git", "rev-parse", "origin/main"):
            return project_update.CommandResult(0, NEW_COMMIT, "")
        if args[:3] == ("git", "pull", "--ff-only"):
            self.pulled = True
            if self.fail_stage == "pull":
                return project_update.CommandResult(1, "", "pull failed")
            return project_update.CommandResult(0, "", "")
        if args[:3] == ("git", "diff", "--name-only"):
            output = "requirements.txt" if self.dependencies else ""
            return project_update.CommandResult(0, output, "")
        if "compileall" in args:
            if self.fail_stage == "compile":
                return project_update.CommandResult(1, "", "compile failed")
            return project_update.CommandResult(0, "", "")
        if args[-2:] == (
            "-c",
            'import app.telegram.application as bot, app.telegram.media as bot_media, app.storage.database as database, app.integrations.rule34.client as api_handler, app.config as config, app.infrastructure.project_update as project_update',
        ):
            if self.fail_stage == "imports":
                return project_update.CommandResult(1, "", "import failed")
            return project_update.CommandResult(0, "", "")
        if "pip" in args:
            return project_update.CommandResult(0, "", "")
        raise AssertionError(f"Unexpected command: {args}")


class ProjectUpdateFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_cannot_update(self):
        update = admin_update(user_id=2)
        with (
            patch.object(bot, "ADMIN_USER_IDS", {1}),
            patch.object(bot, "perform_update", AsyncMock()) as perform,
        ):
            await bot.update_command(update, SimpleNamespace())

        perform.assert_not_awaited()
        self.assertIn("только администратору", update.message.reply_text.await_args.args[0])

    async def test_admin_cannot_update_from_group(self):
        update = admin_update(chat_type="supergroup")
        with (
            patch.object(bot, "ADMIN_USER_IDS", {1}),
            patch.object(bot, "perform_update", AsyncMock()) as perform,
        ):
            await bot.update_command(update, SimpleNamespace())

        perform.assert_not_awaited()
        self.assertIn("личном чате", update.message.reply_text.await_args.args[0])

    async def test_second_concurrent_update_is_rejected(self):
        update = admin_update()
        await bot.update_operation_lock.acquire()
        try:
            with (
                patch.object(bot, "ADMIN_USER_IDS", {1}),
                patch.object(bot, "perform_update", AsyncMock()) as perform,
            ):
                await bot.update_command(update, SimpleNamespace())
        finally:
            bot.update_operation_lock.release()

        perform.assert_not_awaited()
        update.message.reply_text.assert_awaited_once_with(
            "⏳ Обновление уже выполняется."
        )

    async def test_cancelled_update_releases_global_lock(self):
        update = admin_update()
        with (
            patch.object(bot, "ADMIN_USER_IDS", {1}),
            patch.object(
                bot, "perform_update", AsyncMock(side_effect=asyncio.CancelledError)
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot.update_command(update, SimpleNamespace())

        self.assertFalse(bot.update_operation_lock.locked())

    async def test_dirty_worktree_blocks_update(self):
        git = FakeGit(dirty=" M bot.py\n?? local.txt")
        with patch.object(project_update, "run_command", git):
            result = await project_update.perform_update()

        self.assertEqual(result.status, "dirty")
        self.assertEqual(result.changed_files, ("bot.py", "local.txt"))
        self.assertFalse(any(call[:2] == ("git", "fetch") for call in git.calls))

    async def test_no_updates_does_not_restart(self):
        update = admin_update()
        result = project_update.UpdateResult(
            status="current", old_commit=OLD_COMMIT, new_commit=OLD_COMMIT
        )
        context = SimpleNamespace(application=SimpleNamespace(stop_running=Mock()))
        with (
            patch.object(bot, "ADMIN_USER_IDS", {1}),
            patch.object(bot, "perform_update", AsyncMock(return_value=result)),
            patch.object(bot, "request_restart", AsyncMock()) as restart,
        ):
            await bot.update_command(update, context)

        restart.assert_not_awaited()
        update.message.reply_text.assert_awaited_once_with(
            "✅ Уже установлена последняя версия."
        )

    async def test_fetch_error_is_reported_without_credentials(self):
        git = FakeGit(fail_stage="fetch")
        with patch.object(project_update, "run_command", git):
            result = await project_update.perform_update()

        self.assertEqual((result.status, result.stage, result.returncode), ("error", "fetch", 1))
        self.assertNotIn("private remote", result.details)

    async def test_fetch_error_handler_shows_safe_message(self):
        update = admin_update()
        result = project_update.UpdateResult(status="error", stage="fetch", returncode=1)
        with (
            patch.object(bot, "ADMIN_USER_IDS", {1}),
            patch.object(bot, "perform_update", AsyncMock(return_value=result)),
        ):
            await bot.update_command(update, SimpleNamespace())

        text = update.message.reply_text.await_args.args[0]
        self.assertIn("получения данных из GitHub", text)
        self.assertNotIn("private", text)

    async def test_backup_error_blocks_pull(self):
        git = FakeGit()
        with (
            patch.object(project_update, "run_command", git),
            patch.object(
                project_update,
                "create_database_backup",
                AsyncMock(side_effect=OSError("backup failed")),
            ),
        ):
            result = await project_update.perform_update()

        self.assertEqual((result.status, result.stage), ("error", "backup"))
        self.assertFalse(any(call[:2] == ("git", "pull") for call in git.calls))

    async def test_pull_is_fast_forward_only(self):
        git = FakeGit()
        with (
            patch.object(project_update, "run_command", git),
            patch.object(
                project_update,
                "create_database_backup",
                AsyncMock(return_value=Path("backup.db")),
            ),
        ):
            result = await project_update.perform_update()

        self.assertEqual(result.status, "updated")
        self.assertIn(("git", "pull", "--ff-only", "origin", "main"), git.calls)

    async def test_pull_error_does_not_restart(self):
        update = admin_update()
        context = SimpleNamespace(application=SimpleNamespace(stop_running=Mock()))
        result = project_update.UpdateResult(status="error", stage="pull", returncode=1)
        with (
            patch.object(bot, "ADMIN_USER_IDS", {1}),
            patch.object(bot, "perform_update", AsyncMock(return_value=result)),
            patch.object(bot, "request_restart", AsyncMock()) as restart,
        ):
            await bot.update_command(update, context)

        restart.assert_not_awaited()

    async def test_failed_validation_does_not_restart(self):
        update = admin_update()
        result = project_update.UpdateResult(
            status="error",
            stage="imports",
            returncode=1,
            old_commit=OLD_COMMIT,
            new_commit=NEW_COMMIT,
        )
        with (
            patch.object(bot, "ADMIN_USER_IDS", {1}),
            patch.object(bot, "perform_update", AsyncMock(return_value=result)),
            patch.object(bot, "request_restart", AsyncMock()) as restart,
        ):
            await bot.update_command(update, SimpleNamespace())

        restart.assert_not_awaited()
        text = update.message.reply_text.await_args.args[0]
        self.assertIn(OLD_COMMIT[:12], text)
        self.assertIn(NEW_COMMIT[:12], text)

    async def test_successful_update_uses_existing_restart(self):
        update = admin_update()
        context = SimpleNamespace(application=SimpleNamespace(stop_running=Mock()))
        result = project_update.UpdateResult(
            status="updated", old_commit=OLD_COMMIT, new_commit=NEW_COMMIT
        )
        with (
            patch.object(bot, "ADMIN_USER_IDS", {1}),
            patch.object(bot, "perform_update", AsyncMock(return_value=result)),
            patch.object(bot, "write_update_marker") as marker,
            patch.object(bot, "request_restart", AsyncMock()) as restart,
        ):
            await bot.update_command(update, context)

        marker.assert_called_once_with(1, NEW_COMMIT)
        restart.assert_awaited_once_with(update, context, response_text=None)

    async def test_update_check_never_pulls(self):
        calls = []

        async def fake_run(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("git", "fetch"):
                return project_update.CommandResult(0)
            if args[:3] == ("git", "rev-parse", "HEAD"):
                return project_update.CommandResult(0, OLD_COMMIT)
            if args[:3] == ("git", "rev-parse", "origin/main"):
                return project_update.CommandResult(0, NEW_COMMIT)
            if args[:3] == ("git", "rev-list", "--count"):
                return project_update.CommandResult(0, "3")
            raise AssertionError(args)

        with patch.object(project_update, "run_command", fake_run):
            result = await project_update.check_for_updates()

        self.assertEqual(result.commits_behind, 3)
        self.assertFalse(any(call[:2] == ("git", "pull") for call in calls))

    async def test_version_message_does_not_expose_remote_credentials(self):
        update = admin_update()
        info = project_update.VersionInfo("abc1234", "main", "2026-01-01", False)
        with (
            patch.object(bot, "ADMIN_USER_IDS", {1}),
            patch.object(bot, "get_version_info", AsyncMock(return_value=info)),
        ):
            await bot.version_command(update, SimpleNamespace())

        text = update.message.reply_text.await_args.args[0]
        self.assertNotIn("github.com", text)
        self.assertNotIn("@", text)

    async def test_marker_is_removed_after_notification(self):
        telegram_bot = SimpleNamespace(send_message=AsyncMock())
        with tempfile.TemporaryDirectory() as tempdir:
            marker_path = Path(tempdir) / "marker.json"
            with patch.object(project_update, "UPDATE_MARKER_PATH", marker_path):
                project_update.write_update_marker(1, NEW_COMMIT)
                self.assertTrue(await project_update.notify_update_marker(telegram_bot))
                self.assertFalse(marker_path.exists())

        telegram_bot.send_message.assert_awaited_once_with(
            chat_id=1,
            text=f"✅ Бот запущен после обновления. Версия: {NEW_COMMIT[:12]}",
        )


class ProjectUpdateSubprocessTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_output_is_limited(self):
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * 100)
        stream.feed_eof()

        output = await project_update._read_limited(stream, 32)

        self.assertLessEqual(len(output), 32)
        self.assertTrue(output.endswith(b"[output truncated]"))

    async def test_command_timeout_kills_process(self):
        class FakeProcess:
            def __init__(self):
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()
                self.returncode = None
                self.killed = False

            async def wait(self):
                if not self.killed:
                    await asyncio.Event().wait()
                self.returncode = -9
                return self.returncode

            def kill(self):
                self.killed = True
                self.stdout.feed_eof()
                self.stderr.feed_eof()

        process = FakeProcess()
        with patch.object(
            project_update.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=process),
        ):
            with self.assertRaises(project_update.UpdateCommandError) as raised:
                await project_update.run_command("git", "status", timeout=0.01)

        self.assertTrue(process.killed)
        self.assertTrue(raised.exception.timed_out)

    async def test_dependency_change_uses_current_python(self):
        git = FakeGit(dependencies=True)
        with (
            patch.object(project_update, "run_command", git),
            patch.object(
                project_update,
                "create_database_backup",
                AsyncMock(return_value=Path("backup.db")),
            ),
        ):
            result = await project_update.perform_update()

        self.assertEqual(result.status, "updated")
        self.assertTrue(any(
            call[:6] == (
                project_update.sys.executable,
                "-m",
                "pip",
                "install",
                "-r",
                "requirements.txt",
            )
            for call in git.calls
        ))


if __name__ == "__main__":
    unittest.main()
