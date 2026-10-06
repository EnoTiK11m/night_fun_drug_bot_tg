"""Admin shutdown remains bounded when Telegram cannot acknowledge it."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import NetworkError, RetryAfter, TimedOut
import app.telegram.application as bot
from app.infrastructure.instance_lock import InstanceLock
from app.infrastructure.project_update import UpdateResult
from app.telegram.delivery import TelegramRateLimiter
from test_instance_lock import make_lifecycle
from test_project_update import admin_update


class AdminRestartNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = [patch.object(bot, "ADMIN_USER_IDS", {1}),
                        patch.object(bot, "restart_requested", False),
                        patch.object(bot, "ADMIN_SHUTDOWN_NOTIFICATION_TIMEOUT_SECONDS", 0.02)]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.update = admin_update()
        self.stop = Mock()
        self.context = SimpleNamespace(application=SimpleNamespace(stop_running=self.stop))

    async def check_restart(self, error=None):
        self.update.message.reply_text = AsyncMock(side_effect=error)
        await asyncio.wait_for(bot.request_restart(self.update, self.context), 1)
        self.stop.assert_called_once()
        self.assertTrue(bot.restart_requested)
        self.assertEqual(bot.RESTART_EXIT_CODE, 42)

    async def test_successful_notification_restarts(self):
        await self.check_restart()

    async def test_retry_after_notification_restarts(self):
        await self.check_restart(RetryAfter(10315))

    async def test_timed_out_notification_restarts(self):
        await self.check_restart(TimedOut())

    async def test_network_error_notification_restarts(self):
        await self.check_restart(NetworkError("offline"))

    async def test_local_timeout_cancels_notification_and_restarts(self):
        cancelled = asyncio.Event()

        async def blocked(*args):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        self.update.message.reply_text = blocked
        await asyncio.wait_for(bot.request_restart(self.update, self.context), 1)
        self.assertTrue(cancelled.is_set())
        self.stop.assert_called_once()

    async def test_active_cooldown_is_not_bypassed_or_reset(self):
        limiter = TelegramRateLimiter()
        limiter.apply_retry_after(1, RetryAfter(10315))
        until = limiter._global_cooldown_until
        request = AsyncMock()

        async def reply(*args):
            await limiter.execute(request, operation_name="sendMessage", chat_id=1)

        self.update.message.reply_text = reply
        await asyncio.wait_for(bot.request_restart(self.update, self.context), 1)
        request.assert_not_awaited()
        self.assertEqual(limiter._global_cooldown_until, until)
        self.assertEqual(limiter.waiter_count, 0)
        self.stop.assert_called_once()

    async def test_concurrent_restart_requests_stop_once(self):
        await asyncio.gather(*(bot.request_restart(self.update, self.context) for _ in range(5)))
        self.update.message.reply_text.assert_awaited_once()
        self.stop.assert_called_once()

    async def test_cancelled_notification_still_requests_shutdown(self):
        self.update.message.reply_text = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await bot.request_restart(self.update, self.context)
        self.stop.assert_called_once()

    async def test_non_admin_cannot_request_restart(self):
        self.update.effective_user.id = 2
        await bot.request_restart(self.update, self.context)
        self.stop.assert_not_called()
        self.assertFalse(bot.restart_requested)

    async def check_update(self, status, error):
        self.update.message.reply_text = AsyncMock(side_effect=error)
        result = UpdateResult(status=status, old_commit="a" * 40, new_commit="b" * 40,
                              stage="pull" if status == "error" else "", returncode=1 if status == "error" else 0)
        with (patch.object(bot, "perform_update", AsyncMock(return_value=result)),
              patch.object(bot, "write_update_marker") as marker):
            await asyncio.wait_for(bot.update_command(self.update, self.context), 1)
        self.assertFalse(bot.update_operation_lock.locked())
        if status == "updated":
            marker.assert_called_once()
            self.stop.assert_called_once()
            self.assertTrue(bot.restart_requested)
        else:
            marker.assert_not_called()
            self.stop.assert_not_called()
            self.assertFalse(bot.restart_requested)

    async def test_successful_update_network_notification_failure_restarts(self):
        await self.check_update("updated", NetworkError("offline"))

    async def test_successful_update_retry_after_notification_restarts(self):
        await self.check_update("updated", RetryAfter(10315))

    async def test_successful_update_blocked_notification_restarts(self):
        async def blocked(*args):
            await asyncio.Future()
        self.update.message.reply_text = blocked
        with (patch.object(bot, "perform_update", AsyncMock(return_value=UpdateResult(
                status="updated", old_commit="a" * 40, new_commit="b" * 40))),
              patch.object(bot, "write_update_marker")):
            await asyncio.wait_for(bot.update_command(self.update, self.context), 1)
        self.stop.assert_called_once()

    async def test_failed_update_notification_failure_never_restarts(self):
        await self.check_update("error", RetryAfter(10315))

    async def test_failed_update_successful_notification_never_restarts(self):
        await self.check_update("error", None)


class RestartExitLifecycleTests(unittest.TestCase):
    def test_restart_exits_42_and_releases_real_instance_lock(self):
        with tempfile.TemporaryDirectory(prefix="restart_lifecycle_") as directory:
            lock = InstanceLock(Path(directory) / "instance.lock")
            lifecycle = make_lifecycle(lock)
            application = Mock()
            builder = Mock()
            for name in ("application_class", "token", "rate_limiter", "post_init", "post_shutdown", "concurrent_updates"):
                getattr(builder, name).return_value = builder
            builder.build.return_value = application
            update = admin_update()
            update.message.reply_text = AsyncMock(side_effect=TimedOut())

            def polling(**kwargs):
                self.assertTrue(lock.held)
                asyncio.run(bot.request_restart(update, SimpleNamespace(application=application)))

            application.run_polling.side_effect = polling
            with (patch.object(bot.Application, "builder", return_value=builder),
                  patch.object(bot, "ADMIN_USER_IDS", {1}),
                  patch.object(bot, "restart_requested", False),
                  self.assertRaises(SystemExit) as raised):
                bot.run_with_instance_lifecycle(lifecycle, bot.build_and_run_application)
            self.assertEqual(raised.exception.code, 42)
            self.assertFalse(lock.held)
            application.stop_running.assert_called_once()
