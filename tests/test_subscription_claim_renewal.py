"""Lease ownership and worker cleanup against disposable SQLite and fake time."""
import asyncio
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telegram.error import NetworkError, RetryAfter, TimedOut
import app.services.subscriptions as worker
import app.storage.database as database
import app.telegram.application as bot
from app.telegram.delivery import TelegramRateLimiter
from test_telegram_rate_limiter import FakeClock, ManualWaitStrategy, wait_until


class SubscriptionClaimRenewalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="claim_renewal_")
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(database, "DB_PATH", str(Path(self.directory.name) / "test.db")))
        await database.init_db()
        await database.add_subscription(1, "tag", interval_seconds=30)
        self.clock = FakeClock()
        self.epoch = datetime.now().replace(microsecond=0)
        self.sleepers = asyncio.Queue()
        self.renewals = asyncio.Queue()
        self.events = []
        original_connect = database.connect_db

        def sql_datetime(value, *modifiers):
            if value is None:
                return None
            value = self.epoch + timedelta(seconds=self.clock.value) if value == "now" else datetime.fromisoformat(value)
            for modifier in modifiers:
                amount, unit = modifier.split()
                value += timedelta(seconds=float(amount) * (60 if unit.startswith("minute") else 1))
            return value.strftime("%Y-%m-%d %H:%M:%S")

        @asynccontextmanager
        async def connect():
            async with original_connect() as db:
                await db.create_function("datetime", -1, sql_datetime)
                yield db

        self.stack.enter_context(patch.object(database, "connect_db", connect))
        await self.sql("UPDATE subscriptions SET next_check_at='2000-01-01'")

        async def sleep(seconds):
            self.assertEqual(seconds, 60)
            future = asyncio.get_running_loop().create_future()
            self.sleepers.put_nowait(future)
            await future

        lease_class = worker._SubscriptionClaimLease
        self.stack.enter_context(patch.object(worker, "_SubscriptionClaimLease",
            side_effect=lambda *args: lease_class(*args, sleep=sleep)))

        def trace(event, **fields):
            self.events.append((event, fields))
            if event in ("claim.renewed", "claim.renewal_failed"):
                self.renewals.put_nowait(event)

        self.stack.enter_context(patch.object(bot, "trace_event", trace))
        self.stack.enter_context(patch.object(bot, "is_recipient_allowed", return_value=True))
        for name in ("claim_due_subscription", "renew_subscription_claim", "is_subscription_claim_active",
                     "update_subscription_time", "release_subscription_claim", "defer_subscription_after_transient_failure"):
            self.stack.enter_context(patch.object(bot, name, getattr(database, name)))
        for name, value in {
            "get_user_blacklist": set(), "get_user_settings": {"show_caption": False},
            "get_subscription_options": {}, "get_subscription_cached_image": {"id": 123},
            "remember_and_cache_post": None, "mark_post_sent": None,
            "clear_delivery_failure_for_post": None, "save_delivery_failure": None,
        }.items():
            self.stack.enter_context(patch.object(bot, name, AsyncMock(return_value=value)))
        self.delivered = self.stack.enter_context(patch.object(bot.search_service, "delivered", AsyncMock()))
        self.started = asyncio.Event()
        self.finish_send = asyncio.Event()
        self.accepted = []

        async def send(*args, before_send, **kwargs):
            self.started.set()
            await self.finish_send.wait()
            if await before_send():
                self.accepted.append(123)
                return True
            return False

        self.stack.enter_context(patch.object(bot, "send_post_media_to_chat", send))
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.stack.close()
        self.directory.cleanup()

    async def sql(self, query, parameters=()):
        async with database.connect_db() as db:
            cursor = await db.execute(query, parameters)
            rows = await cursor.fetchall()
            await db.commit()
            return rows

    async def start(self):
        task = asyncio.create_task(worker.process_one_subscription(bot, SimpleNamespace(bot=object()), (1, "tag", 30, 0)))
        self.tasks.append(task)
        await asyncio.wait_for(self.started.wait(), 2)
        return task

    async def step(self):
        future = await asyncio.wait_for(self.sleepers.get(), 2)
        self.clock.advance(60)
        future.set_result(None)
        return await asyncio.wait_for(self.renewals.get(), 2)

    def assert_no_lease_tasks(self):
        self.assertFalse([t for t in asyncio.all_tasks() if not t.done() and
                          t.get_name() in ("subscription-claim-worker", "subscription-claim-renewal")])

    async def test_short_success_clears_claim_and_stops_renewal(self):
        task = await self.start()
        self.finish_send.set()
        self.assertTrue(await task)
        self.assertEqual(await self.sql("SELECT processing_token,processing_until FROM subscriptions"), [(None, None)])
        self.assertEqual(self.accepted, [123])
        self.assertFalse(any(e == "claim.renewed" for e, _ in self.events))
        self.assert_no_lease_tasks()

    async def check_long_retry_after(self, seconds):
        scheduler = ManualWaitStrategy()
        limiter = TelegramRateLimiter(clock=self.clock, wait_strategy=scheduler)
        attempts = []

        async def send(*args, before_send, **kwargs):
            async def operation():
                self.assertTrue(await before_send())
                attempts.append(self.clock.value)
                if len(attempts) == 1:
                    self.started.set()
                    raise RetryAfter(seconds)
                self.accepted.append(123)
                return True
            return await limiter.execute(operation, operation_name="send_photo", chat_id=1)

        self.stack.enter_context(patch.object(bot, "send_post_media_to_chat", send))
        task = await self.start()
        await wait_until(lambda: limiter.waiter_count == 1)
        self.assertEqual(limiter._global_cooldown_until, seconds)
        token = (await self.sql("SELECT processing_token FROM subscriptions"))[0][0]
        for _ in range(seconds // 60):
            self.assertEqual(await self.step(), "claim.renewed")
            self.assertTrue(await database.is_subscription_claim_active(1, "tag", token))
            self.assertIsNone(await database.claim_due_subscription(1, "tag"))
        self.assertEqual(attempts, [0])
        await scheduler.advance(limiter, self.clock, seconds - self.clock.value)
        self.assertTrue(await asyncio.wait_for(task, 2))
        self.assertEqual(attempts, [0, seconds])
        self.assertEqual(await self.sql("SELECT processing_token FROM subscriptions"), [(None,)])
        self.delivered.assert_awaited_once()
        self.assert_no_lease_tasks()

    async def test_retry_after_1800_preserves_live_claim(self):
        await self.check_long_retry_after(1800)

    async def test_retry_after_10315_preserves_live_claim(self):
        await self.check_long_retry_after(10315)
        renewed = [fields for event, fields in self.events if event == "claim.renewed"]
        self.assertEqual(len(renewed), 171)
        self.assertEqual(renewed[-1]["lease_remaining_seconds"], 300)
        self.assertIn("claim_token_hash", renewed[-1])
        self.assertNotIn("processing_token", renewed[-1])
        self.assertEqual(renewed[-1]["user_id_hash"], bot.safe_hash(1, "u_"))
        self.assertEqual(renewed[-1]["query_hash"], bot.safe_hash("tag", "q_"))

    async def check_loss(self, mutation, parameters=()):
        task = await self.start()
        await self.sql(mutation, parameters)
        self.assertEqual(await self.step(), "claim.renewal_failed")
        self.assertFalse(await asyncio.wait_for(task, 2))
        self.assertEqual(self.accepted, [])
        self.delivered.assert_not_awaited()
        self.assert_no_lease_tasks()

    async def test_deleted_subscription_stops_waiting_worker(self):
        await self.check_loss("DELETE FROM subscriptions")

    async def test_disabled_subscription_stops_waiting_worker(self):
        await self.check_loss("UPDATE subscriptions SET is_active=0")

    async def test_claim_loss_cancels_actual_limiter_wait_without_request(self):
        limiter = TelegramRateLimiter(clock=self.clock, wait_strategy=ManualWaitStrategy())
        limiter.apply_retry_after(1, RetryAfter(10315))
        request = AsyncMock()

        async def send(*args, **kwargs):
            self.started.set()
            return await limiter.execute(request, operation_name="send_photo", chat_id=1)

        self.stack.enter_context(patch.object(bot, "send_post_media_to_chat", send))
        await self.check_loss("DELETE FROM subscriptions")
        request.assert_not_awaited()
        self.assertEqual(limiter.waiter_count, 0)

    async def test_replaced_token_is_not_renewed_or_released_by_old_worker(self):
        await self.check_loss("UPDATE subscriptions SET processing_token='new-owner'")
        self.assertEqual((await self.sql("SELECT processing_token FROM subscriptions"))[0][0], "new-owner")

    async def test_global_pause_stops_waiting_worker(self):
        pause = (self.epoch + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        await self.check_loss("INSERT INTO user_settings(user_id,settings_json) VALUES (1,?)",
                              ('{"subscription_pause_until":"' + pause + '"}',))

    async def test_false_renewal_stops_waiting_worker(self):
        self.stack.enter_context(patch.object(bot, "renew_subscription_claim", AsyncMock(return_value=False)))
        await self.check_loss("SELECT 1")

    async def test_renewal_exception_fails_closed(self):
        self.stack.enter_context(patch.object(bot, "renew_subscription_claim", AsyncMock(side_effect=RuntimeError("DB unavailable"))))
        await self.check_loss("SELECT 1")

    async def test_expired_claim_cannot_be_resurrected(self):
        token = await database.claim_due_subscription(1, "tag")
        self.clock.advance(301)
        self.assertFalse(await database.renew_subscription_claim(1, "tag", token))
        self.assertFalse(await database.is_subscription_claim_active(1, "tag", token))

    async def test_renewal_is_scoped_to_user_query_and_token(self):
        token = await database.claim_due_subscription(1, "tag")
        original = await self.sql("SELECT processing_until FROM subscriptions")
        self.clock.advance(60)
        for user, query, candidate in ((2, "tag", token), (1, "other", token), (1, "tag", "wrong")):
            self.assertFalse(await database.renew_subscription_claim(user, query, candidate))
        self.assertEqual(await self.sql("SELECT processing_until FROM subscriptions"), original)
        self.assertTrue(await database.renew_subscription_claim(1, "tag", token))

    async def test_cancel_stops_renewal_and_preserves_existing_ambiguous_defer(self):
        task = await self.start()
        self.assertEqual(await self.step(), "claim.renewed")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        row = (await self.sql("SELECT processing_token,next_check_at FROM subscriptions"))[0]
        self.assertIsNone(row[0])
        self.assertEqual(row[1], (self.epoch + timedelta(seconds=1860)).strftime("%Y-%m-%d %H:%M:%S"))
        self.assert_no_lease_tasks()

    async def check_finish_error(self, error=None, result=False):
        self.stack.enter_context(patch.object(bot, "send_post_media_to_chat", AsyncMock(side_effect=error, return_value=result)))
        task = asyncio.create_task(worker.process_one_subscription(bot, SimpleNamespace(bot=object()), (1, "tag", 30, 0)))
        self.tasks.append(task)
        self.assertFalse(await task)
        self.assertEqual(await self.sql("SELECT processing_token,processing_until FROM subscriptions"), [(None, None)])
        self.assert_no_lease_tasks()

    async def test_failed_delivery_stops_renewal(self):
        await self.check_finish_error()

    async def test_terminal_token_clear_cannot_trigger_false_claim_loss(self):
        cleared = asyncio.Event()
        finish_update = asyncio.Event()

        async def update(*args):
            result = await database.update_subscription_time(*args)
            cleared.set()
            await finish_update.wait()
            return result

        self.stack.enter_context(patch.object(bot, "update_subscription_time", update))
        task = await self.start()
        self.assertEqual(await self.step(), "claim.renewed")
        pending = await asyncio.wait_for(self.sleepers.get(), 2)
        self.finish_send.set()
        await asyncio.wait_for(cleared.wait(), 2)
        self.assertTrue(pending.cancelled())
        self.clock.advance(60)
        finish_update.set()
        self.assertTrue(await task)
        self.assertFalse(any(e == "claim.renewal_failed" for e, _ in self.events))
        self.assert_no_lease_tasks()

    async def test_cancellation_before_worker_starts_releases_claim(self):
        create_task = asyncio.create_task

        def cancel_new_worker(coroutine, **kwargs):
            task = create_task(coroutine, **kwargs)
            if kwargs.get("name") == "subscription-claim-worker":
                task.cancel()
            return task

        with patch.object(worker.asyncio, "create_task", cancel_new_worker):
            with self.assertRaises(asyncio.CancelledError):
                await worker.process_one_subscription(bot, SimpleNamespace(bot=object()), (1, "tag", 30, 0))
        self.assertEqual(await self.sql("SELECT processing_token,processing_until FROM subscriptions"), [(None, None)])
        self.assert_no_lease_tasks()

    async def test_network_error_stops_renewal(self):
        await self.check_finish_error(NetworkError("offline"))

    async def test_timeout_stops_renewal(self):
        await self.check_finish_error(TimedOut())

    async def test_exception_stops_renewal(self):
        await self.check_finish_error(RuntimeError("failed"))

    async def test_retry_after_exhaustion_stops_renewal(self):
        await self.check_finish_error(RetryAfter(10315))
