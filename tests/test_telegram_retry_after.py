"""Regression coverage for complete Telegram flood-control waits."""
import asyncio
import json
from datetime import timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from telegram.error import RetryAfter
import app.observability.logic_trace as trace
from app.telegram.delivery import TelegramRateLimiter, retry_after_seconds
from test_telegram_rate_limiter import FakeClock, ManualWaitStrategy, wait_until


class RetryAfterDurationTests(unittest.TestCase):
    def test_valid_durations_are_not_capped(self):
        for value, expected in ((30, 30.0), (300, 300.0), (301, 301.0),
                                (1800, 1800.0), (1800.5, 1800.5), (0, 0.0),
                                (timedelta(minutes=45), 2700.0),
                                (SimpleNamespace(total_seconds=lambda: 3600), 3600.0),
                                (RetryAfter(1800), 1800.0)):
            with self.subTest(value=value):
                self.assertEqual(retry_after_seconds(value), expected)

    def test_malformed_and_nonfinite_values_use_safe_fallback(self):
        values = (None, object(), '1800', True, float('nan'), float('inf'),
                  float('-inf'), -1, -1800.5, timedelta(seconds=-30),
                  SimpleNamespace(total_seconds=lambda: float('nan')),
                  SimpleNamespace(total_seconds='not callable'))
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(retry_after_seconds(value), 1.0)


class RetryAfterCooldownTests(unittest.IsolatedAsyncioTestCase):
    def make_limiter(self, **kwargs):
        clock = FakeClock()
        scheduler = ManualWaitStrategy()
        limiter = TelegramRateLimiter(clock=clock, wait_strategy=scheduler,
                                      global_requests_per_second=1000,
                                      per_chat_requests_per_second=1000, burst=10, **kwargs)
        return limiter, clock, scheduler

    async def test_global_and_chat_cooldown_receive_full_duration(self):
        limiter, clock, _ = self.make_limiter()
        clock.advance(100)
        self.assertEqual(limiter.apply_retry_after(1, RetryAfter(1800)), 1800.0)
        self.assertEqual(limiter._global_cooldown_until, 1900.0)
        self.assertEqual(limiter._buckets[1].cooldown_until, 1900.0)
        limiter.apply_retry_after(1, RetryAfter(30))
        self.assertEqual(limiter._global_cooldown_until, 1900.0)
        self.assertEqual(limiter._buckets[1].cooldown_until, 1900.0)

    async def test_negative_value_does_not_create_negative_cooldown(self):
        limiter, clock, _ = self.make_limiter()
        clock.advance(100)
        self.assertEqual(limiter.apply_retry_after(1, -30), 1.0)
        self.assertEqual(limiter._global_cooldown_until, 101.0)
        self.assertEqual(limiter._buckets[1].cooldown_until, 101.0)

    async def test_all_chat_waiters_stay_blocked_until_full_cooldown(self):
        limiter, clock, scheduler = self.make_limiter()
        limiter.apply_retry_after(1, RetryAfter(1800))
        tasks = [asyncio.create_task(limiter.wait_for_slot(chat)) for chat in (1, 2)]
        try:
            await wait_until(lambda: limiter.waiter_count == 2)
            await scheduler.advance(limiter, clock, 300)
            self.assertTrue(all(not t.done() for t in tasks))
            await scheduler.advance(limiter, clock, 1499)
            self.assertTrue(all(not t.done() for t in tasks))
            await scheduler.advance(limiter, clock, 1)
            self.assertEqual(await asyncio.gather(*tasks), [True, True])
            self.assertEqual(clock.value, 1800.0)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_default_condition_wait_releases_lock_during_long_cooldown(self):
        clock = FakeClock()
        limiter = TelegramRateLimiter(clock=clock)
        limiter.apply_retry_after(1, RetryAfter(1800))
        task = asyncio.create_task(limiter.wait_for_slot(1))
        try:
            await wait_until(lambda: limiter.waiter_count == 1)
            async with asyncio.timeout(1):
                async with limiter._condition:
                    self.assertFalse(task.done())
                    clock.advance(1800)
                    limiter._condition.notify_all()
                self.assertTrue(await task)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_long_retry_still_respects_attempt_limit(self):
        limiter, clock, scheduler = self.make_limiter(max_retry_after_attempts=1)
        operation = AsyncMock(side_effect=RetryAfter(1800))
        task = asyncio.create_task(limiter.execute(operation, operation_name='send_photo', chat_id=1))
        try:
            await wait_until(lambda: limiter.waiter_count == 1)
            self.assertEqual(operation.await_count, 1)
            await scheduler.advance(limiter, clock, 1799)
            self.assertEqual(operation.await_count, 1)
            await scheduler.advance(limiter, clock, 1)
            with self.assertRaises(RetryAfter):
                await task
            self.assertEqual(operation.await_count, 2)
            self.assertEqual(limiter.metrics.telegram_retry_attempts, 1)
            self.assertEqual(limiter._global_cooldown_until, 3600.0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_warning_and_real_trace_record_full_duration_even_without_retries(self):
        limiter, _, _ = self.make_limiter(max_retry_after_attempts=0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace.jsonl'
            try:
                trace.configure_trace(enabled=True, path=path)
                with self.assertLogs('app.telegram.delivery', level='WARNING') as logs:
                    with trace.flow_context('search'):
                        with self.assertRaises(RetryAfter):
                            await limiter.execute(AsyncMock(side_effect=RetryAfter(1800)),
                                                  operation_name='send_photo', chat_id=123)
            finally:
                await trace.shutdown_trace()
            events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        event = next(e for e in events if e['event'] == 'telegram.send.retry_after')
        self.assertEqual(event['raw_retry_after_seconds'], 1800.0)
        self.assertEqual(event['applied_wait_seconds'], 1800.0)
        self.assertEqual(event['cooldown_remaining_seconds'], 1800.0)
        self.assertEqual(event['attempt'], 1)
        self.assertEqual(event['operation'], 'send_photo')
        message = '\n'.join(logs.output)
        for field in ('operation=send_photo', 'chat_id=123', 'attempt=1',
                      'raw_retry_after_seconds=1800.0', 'applied_wait_seconds=1800.0'):
            self.assertIn(field, message)

    async def test_malformed_raw_value_is_not_logged_as_arbitrary_object(self):
        limiter, _, _ = self.make_limiter(max_retry_after_attempts=0)
        class SecretObject:
            def __repr__(self):
                raise AssertionError('arbitrary object must not be formatted')
        class MalformedRetryAfter(RetryAfter):
            malformed = False
            @property
            def retry_after(self):
                return SecretObject() if self.malformed else super().retry_after
        error = MalformedRetryAfter(1)
        error.malformed = True
        with self.assertLogs('app.telegram.delivery', level='WARNING') as logs:
            with self.assertRaises(RetryAfter):
                await limiter.execute(AsyncMock(side_effect=error), operation_name='send_photo', chat_id=1)
        self.assertIn('raw_retry_after_seconds=None', logs.output[0])
        self.assertIn('applied_wait_seconds=1.0', logs.output[0])
