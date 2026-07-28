import asyncio
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, RetryAfter, TimedOut

import bot
import bot_media
from bot_delivery import (
    TelegramRateLimiter,
    TelegramRateLimiterClosed,
    TelegramRateLimiterLifecycleError,
    retry_after_seconds,
)


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class ManualWaitStrategy:
    async def __call__(self, condition, _delay):
        await condition.wait()

    async def advance(self, limiter, clock, seconds):
        clock.advance(seconds)
        async with limiter._condition:
            limiter._condition.notify_all()
        await asyncio.sleep(0)


async def wait_until(predicate, attempts=100):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


class TelegramRateLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_limit_spaces_requests(self):
        clock = FakeClock()
        scheduler = ManualWaitStrategy()
        limiter = TelegramRateLimiter(
            global_requests_per_second=1,
            per_chat_requests_per_second=1000,
            burst=1,
            clock=clock,
            wait_strategy=scheduler,
        )
        completed = []

        async def request(chat_id):
            await limiter.wait_for_slot(chat_id)
            completed.append((chat_id, clock.value))

        await limiter.wait_for_slot(0)
        tasks = [asyncio.create_task(request(chat_id)) for chat_id in (1, 2, 3)]
        await wait_until(lambda: limiter.waiter_count == 3)
        for completed_count, _task in enumerate(tasks, start=1):
            await scheduler.advance(limiter, clock, 1)
            await wait_until(lambda: len(completed) == completed_count)
        await asyncio.gather(*tasks)
        self.assertEqual(completed, [(1, 1.0), (2, 2.0), (3, 3.0)])

    async def test_per_chat_limit_does_not_limit_different_chats(self):
        clock = FakeClock()
        scheduler = ManualWaitStrategy()
        limiter = TelegramRateLimiter(
            global_requests_per_second=1000,
            per_chat_requests_per_second=1,
            burst=1,
            clock=clock,
            wait_strategy=scheduler,
        )
        await limiter.wait_for_slot(1)
        clock.advance(0.001)
        blocked = asyncio.create_task(limiter.wait_for_slot(1))
        ready = asyncio.create_task(limiter.wait_for_slot(2))
        await ready
        self.assertFalse(blocked.done())
        await scheduler.advance(limiter, clock, 1)
        self.assertTrue(await blocked)

    async def test_fifo_fairness_between_users(self):
        clock = FakeClock()
        scheduler = ManualWaitStrategy()
        limiter = TelegramRateLimiter(
            global_requests_per_second=1,
            per_chat_requests_per_second=1000,
            burst=1,
            clock=clock,
            wait_strategy=scheduler,
        )
        order = []

        async def request(chat_id):
            await limiter.wait_for_slot(chat_id)
            order.append(chat_id)

        await limiter.wait_for_slot(0)
        tasks = [asyncio.create_task(request(chat_id)) for chat_id in (1, 2, 3, 4)]
        await wait_until(lambda: limiter.waiter_count == 4)
        for completed_count, _task in enumerate(tasks, start=1):
            await scheduler.advance(limiter, clock, 1)
            await wait_until(lambda: len(order) == completed_count)
        await asyncio.gather(*tasks)
        self.assertEqual(order, [1, 2, 3, 4])

    async def test_burst_is_bounded(self):
        clock = FakeClock()
        scheduler = ManualWaitStrategy()
        limiter = TelegramRateLimiter(
            global_requests_per_second=1,
            per_chat_requests_per_second=1000,
            burst=3,
            clock=clock,
            wait_strategy=scheduler,
        )
        for chat_id in (1, 2, 3):
            await limiter.wait_for_slot(chat_id)
        fourth = asyncio.create_task(limiter.wait_for_slot(4))
        await wait_until(lambda: limiter.waiter_count == 1)
        self.assertFalse(fourth.done())
        await scheduler.advance(limiter, clock, 1)
        self.assertTrue(await fourth)

    async def test_retry_after_creates_global_pause(self):
        clock = FakeClock()
        scheduler = ManualWaitStrategy()
        limiter = TelegramRateLimiter(
            global_requests_per_second=1000,
            per_chat_requests_per_second=1000,
            burst=10,
            max_retry_after_attempts=1,
            clock=clock,
            wait_strategy=scheduler,
        )
        first_attempt = asyncio.Event()
        calls = 0

        async def flooded_operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                first_attempt.set()
                raise RetryAfter(5)
            return True

        flooded = asyncio.create_task(
            limiter.execute(flooded_operation, operation_name="send_message", chat_id=1)
        )
        await first_attempt.wait()
        other = asyncio.create_task(
            limiter.execute(AsyncMock(return_value=True), operation_name="send_message", chat_id=2)
        )
        await wait_until(lambda: limiter.waiter_count == 2)
        await scheduler.advance(limiter, clock, 4)
        self.assertFalse(other.done())
        await scheduler.advance(limiter, clock, 1)
        await other
        self.assertTrue(await flooded)

    async def test_retry_after_added_while_waiting_is_rechecked_after_wakeup(self):
        clock = FakeClock()
        scheduler = ManualWaitStrategy()
        limiter = TelegramRateLimiter(
            global_requests_per_second=1,
            per_chat_requests_per_second=1000,
            burst=1,
            clock=clock,
            wait_strategy=scheduler,
        )
        await limiter.wait_for_slot(1)
        waiting = asyncio.create_task(limiter.wait_for_slot(2))
        await wait_until(lambda: limiter.waiter_count == 1)
        limiter.apply_retry_after(1, SimpleNamespace(retry_after=5))
        await scheduler.advance(limiter, clock, 1)
        self.assertFalse(waiting.done())
        await scheduler.advance(limiter, clock, 4)
        self.assertTrue(await waiting)

    def test_retry_after_number_and_timedelta_are_normalized(self):
        self.assertEqual(retry_after_seconds(7), 7.0)
        self.assertEqual(retry_after_seconds(timedelta(seconds=9)), 9.0)
        self.assertEqual(retry_after_seconds(1000), 300.0)

    async def test_retry_attempts_are_limited(self):
        limiter = TelegramRateLimiter(
            global_requests_per_second=1000,
            per_chat_requests_per_second=1000,
            burst=10,
            max_retry_after_attempts=1,
        )
        operation = AsyncMock(side_effect=RetryAfter(0))
        with self.assertRaises(RetryAfter):
            await limiter.execute(operation, operation_name="send_message", chat_id=1)
        self.assertEqual(operation.await_count, 2)
        self.assertEqual(limiter.metrics.telegram_retry_attempts, 1)

    async def test_cancellation_removes_multiple_waiters(self):
        limiter = TelegramRateLimiter(
            global_requests_per_second=0.1,
            per_chat_requests_per_second=1000,
            burst=1,
        )
        await limiter.wait_for_slot(1)
        waiting = [asyncio.create_task(limiter.wait_for_slot(chat_id)) for chat_id in (2, 3, 4)]
        await wait_until(lambda: limiter.waiter_count == 3)
        for task in waiting:
            task.cancel()
        for task in waiting:
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(limiter.waiter_count, 0)

    async def test_inactive_bucket_is_removed_by_ttl(self):
        clock = FakeClock()
        limiter = TelegramRateLimiter(
            global_requests_per_second=1000,
            per_chat_requests_per_second=1000,
            burst=10,
            state_ttl_seconds=10,
            clock=clock,
        )
        await limiter.wait_for_slot(1)
        clock.advance(11)
        await limiter.wait_for_slot(2)
        self.assertNotIn(1, limiter._buckets)
        self.assertIn(2, limiter._buckets)

    async def test_registry_is_bounded_for_many_unique_users(self):
        limiter = TelegramRateLimiter(
            global_requests_per_second=1_000_000,
            per_chat_requests_per_second=1_000_000,
            burst=2000,
            max_registry_size=50,
        )
        for chat_id in range(1000):
            await limiter.wait_for_slot(chat_id)
        self.assertLessEqual(limiter.registry_size, 50)

    async def test_shutdown_releases_waiters_and_rejects_new_requests(self):
        limiter = TelegramRateLimiter(
            global_requests_per_second=0.1,
            per_chat_requests_per_second=1000,
            burst=1,
        )
        await limiter.wait_for_slot(1)
        waiting = asyncio.create_task(limiter.wait_for_slot(2))
        for _ in range(20):
            if limiter.waiter_count:
                break
            await asyncio.sleep(0)
        await limiter.shutdown()
        with self.assertRaises(TelegramRateLimiterClosed):
            await waiting
        with self.assertRaises(TelegramRateLimiterClosed):
            await limiter.wait_for_slot(3)
        self.assertEqual(limiter.registry_size, 0)

    async def test_process_request_never_retries_timeout(self):
        limiter = TelegramRateLimiter(
            global_requests_per_second=1000,
            per_chat_requests_per_second=1000,
            burst=10,
        )
        with self.assertRaises(TimedOut):
            await limiter.process_request(
                operation := AsyncMock(side_effect=TimedOut()),
                (), {}, "editMessageText", {"chat_id": 1}, None
            )
        self.assertEqual(operation.await_count, 1)

    async def test_process_request_retries_only_retry_after_bounded(self):
        limiter = TelegramRateLimiter(
            global_requests_per_second=1000,
            per_chat_requests_per_second=1000,
            burst=10,
            max_retry_after_attempts=1,
        )
        operation = AsyncMock(side_effect=[RetryAfter(0), True])
        result = await limiter.process_request(
            operation, (), {}, "sendMessage", {"chat_id": 1}, None
        )
        self.assertTrue(result)
        self.assertEqual(operation.await_count, 2)

    async def test_explicit_safe_timeout_retries_once(self):
        limiter = TelegramRateLimiter(
            global_requests_per_second=1000,
            per_chat_requests_per_second=1000,
            burst=10,
        )
        operation = AsyncMock(side_effect=[TimedOut(), True])
        self.assertTrue(await limiter.execute(
            operation,
            operation_name="edit_message_text",
            chat_id=1,
            safe_to_retry_timeout=True,
        ))
        self.assertEqual(operation.await_count, 2)

    async def test_ambiguous_bad_request_is_normalized_only_by_exact_policy(self):
        cases = (
            ("edit_message_text", "Message is not modified"),
            ("edit_message_reply_markup", "Message is not modified"),
            ("delete_message", "Message to delete not found"),
            (
                "answer_callback_query",
                "Query is too old and response timeout expired or query ID is invalid",
            ),
        )
        for policy, message in cases:
            with self.subTest(policy=policy):
                limiter = TelegramRateLimiter(
                    global_requests_per_second=1000,
                    per_chat_requests_per_second=1000,
                    burst=10,
                )
                operation = AsyncMock(side_effect=[TimedOut(), BadRequest(message)])
                result = await limiter.execute(
                    operation,
                    operation_name=policy,
                    chat_id=1,
                    safe_to_retry_timeout=True,
                    ambiguous_bad_request_policy=policy,
                )
                self.assertTrue(result)
                self.assertEqual(operation.await_count, 2)

    async def test_bad_request_without_ambiguous_timeout_is_not_hidden(self):
        limiter = TelegramRateLimiter(
            global_requests_per_second=1000,
            per_chat_requests_per_second=1000,
            burst=10,
        )
        with self.assertRaises(BadRequest):
            await limiter.execute(
                AsyncMock(side_effect=BadRequest("Message is not modified")),
                operation_name="edit_message_text",
                chat_id=1,
                safe_to_retry_timeout=True,
                ambiguous_bad_request_policy="edit_message_text",
            )

    async def test_unrelated_bad_request_after_timeout_is_not_hidden(self):
        limiter = TelegramRateLimiter(
            global_requests_per_second=1000,
            per_chat_requests_per_second=1000,
            burst=10,
        )
        operation = AsyncMock(side_effect=[TimedOut(), BadRequest("Chat not found")])
        with self.assertRaises(BadRequest):
            await limiter.execute(
                operation,
                operation_name="delete_message",
                chat_id=1,
                safe_to_retry_timeout=True,
                ambiguous_bad_request_policy="delete_message",
            )

    async def test_ready_waiters_are_fifo_and_fifo_wait_is_measured(self):
        clock = FakeClock()
        scheduler = ManualWaitStrategy()
        limiter = TelegramRateLimiter(
            global_requests_per_second=1,
            per_chat_requests_per_second=1000,
            burst=1,
            clock=clock,
            wait_strategy=scheduler,
        )
        await limiter.wait_for_slot(0)
        order = []

        async def request(chat_id):
            await limiter.wait_for_slot(chat_id)
            order.append(chat_id)

        tasks = [asyncio.create_task(request(chat_id)) for chat_id in (11, 12)]
        await wait_until(lambda: limiter.waiter_count == 2)
        await scheduler.advance(limiter, clock, 1)
        await wait_until(lambda: order == [11])
        await scheduler.advance(limiter, clock, 1)
        await asyncio.gather(*tasks)
        self.assertEqual(order, [11, 12])
        self.assertGreaterEqual(limiter.metrics.telegram_rate_limit_waits, 2)

    async def test_blocked_chat_does_not_block_ready_chat(self):
        clock = FakeClock()
        scheduler = ManualWaitStrategy()
        limiter = TelegramRateLimiter(
            global_requests_per_second=1000,
            per_chat_requests_per_second=1,
            burst=1,
            clock=clock,
            wait_strategy=scheduler,
        )
        await limiter.wait_for_slot(1)
        clock.advance(0.001)
        blocked = asyncio.create_task(limiter.wait_for_slot(1))
        await wait_until(lambda: limiter.waiter_count == 1)
        ready = asyncio.create_task(limiter.wait_for_slot(2))
        self.assertTrue(await ready)
        self.assertFalse(blocked.done())
        blocked.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await blocked

    async def test_initialize_with_active_waiters_is_rejected_then_shutdown_releases(self):
        clock = FakeClock()
        scheduler = ManualWaitStrategy()
        limiter = TelegramRateLimiter(
            global_requests_per_second=1,
            per_chat_requests_per_second=1000,
            burst=1,
            clock=clock,
            wait_strategy=scheduler,
        )
        await limiter.wait_for_slot(1)
        waiting = asyncio.create_task(limiter.wait_for_slot(2))
        await wait_until(lambda: limiter.waiter_count == 1)
        with self.assertRaises(TelegramRateLimiterLifecycleError):
            await limiter.initialize()
        self.assertFalse(waiting.done())
        await limiter.shutdown()
        with self.assertRaises(TelegramRateLimiterClosed):
            await waiting

    async def test_nested_ptb_request_is_limited_once(self):
        limiter = TelegramRateLimiter(
            global_requests_per_second=1000,
            per_chat_requests_per_second=1000,
            burst=10,
        )
        callback = AsyncMock(return_value=True)

        async def operation():
            return await limiter.process_request(
                callback, (), {}, "sendMessage", {"chat_id": 1}, None
            )

        self.assertTrue(
            await limiter.execute(operation, operation_name="send_message", chat_id=1)
        )
        self.assertEqual(callback.await_count, 1)
        self.assertEqual(limiter.metrics.telegram_requests_total, 1)


class TelegramLimiterCrossLoopTests(unittest.TestCase):
    def test_other_loop_is_rejected_without_state_reset_then_restart_works(self):
        limiter = TelegramRateLimiter(
            global_requests_per_second=1000,
            per_chat_requests_per_second=1000,
            burst=10,
        )
        loop_one = asyncio.new_event_loop()
        loop_two = asyncio.new_event_loop()

        async def bind_and_set_state():
            await limiter.wait_for_slot(1)
            limiter.apply_retry_after(1, SimpleNamespace(retry_after=15))

        async def use_without_shutdown():
            return await limiter.wait_for_slot(2)

        async def restart():
            await limiter.start()
            return await limiter.wait_for_slot(2)

        try:
            loop_one.run_until_complete(bind_and_set_state())
            cooldown = limiter._global_cooldown_until
            registry = set(limiter._buckets)
            with self.assertRaises(TelegramRateLimiterLifecycleError):
                loop_two.run_until_complete(use_without_shutdown())
            self.assertEqual(limiter._global_cooldown_until, cooldown)
            self.assertEqual(set(limiter._buckets), registry)
            loop_one.run_until_complete(limiter.shutdown())
            self.assertTrue(loop_two.run_until_complete(restart()))
        finally:
            loop_one.close()
            loop_two.run_until_complete(limiter.shutdown())
            loop_two.close()


class TelegramLimiterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def make_limiter(self, retries=1):
        return TelegramRateLimiter(
            global_requests_per_second=1000,
            per_chat_requests_per_second=1000,
            burst=20,
            max_retry_after_attempts=retries,
        )

    async def run_with_limiter(self, limiter, operation, **kwargs):
        return await limiter.execute(operation, **kwargs)

    async def test_media_uses_limiter_once_and_retries_retry_after_bounded(self):
        limiter = self.make_limiter(retries=1)
        telegram_bot = AsyncMock()
        telegram_bot.send_photo = AsyncMock(side_effect=[RetryAfter(0), None])
        post = {"id": 1, "file_url": "https://example.test/image.jpg"}

        async def execute(operation, **kwargs):
            return await limiter.execute(operation, **kwargs)

        with patch.object(bot_media, "execute_telegram_request", side_effect=execute) as helper:
            delivered = await bot_media.send_post_media_to_chat(
                telegram_bot, 123, post, keyboard=object()
            )
        self.assertTrue(delivered)
        self.assertEqual(helper.await_count, 1)
        self.assertEqual(telegram_bot.send_photo.await_count, 2)
        self.assertEqual(limiter.metrics.telegram_requests_total, 2)

    async def test_non_idempotent_media_timeout_is_not_retried(self):
        limiter = self.make_limiter()
        telegram_bot = AsyncMock()
        telegram_bot.send_photo = AsyncMock(side_effect=TimedOut())
        post = {"id": 1, "file_url": "https://example.test/image.jpg"}

        async def execute(operation, **kwargs):
            return await limiter.execute(operation, **kwargs)

        with patch.object(bot_media, "execute_telegram_request", side_effect=execute):
            with self.assertRaises(TimedOut):
                await bot_media.send_post_media_to_chat(
                    telegram_bot, 123, post, keyboard=object()
                )
        self.assertEqual(telegram_bot.send_photo.await_count, 1)

    async def test_callback_answer_uses_per_chat_limiter(self):
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=777),
            answer=AsyncMock(),
        )
        with patch("bot.execute_telegram_request", new=AsyncMock()) as execute:
            await bot.safe_query_answer(query, "ok")
        self.assertEqual(execute.await_args.kwargs["chat_id"], 777)
        self.assertTrue(execute.await_args.kwargs["safe_to_retry_timeout"])
        self.assertEqual(
            execute.await_args.kwargs["ambiguous_bad_request_policy"],
            "answer_callback_query",
        )
