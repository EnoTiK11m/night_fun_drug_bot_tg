import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app.storage.database as database
import app.observability.logic_trace as logic_trace
import app.telegram.application as bot
import app.config as config
from app.integrations.rule34.client import rule34API
from app.services.media_preferences import RuntimeMetrics
from app.telegram.formatting import parse_subscription_interval, format_subscription_interval
from app.integrations.rule34.rate_limiter import Rule34Limiter, QuotaQueueFull, rule34_limiter
from app.services.search import ProgressiveSearch, SearchBudgetExceeded
from test_database_integration import TempDatabaseTestCase


class Clock:
    now = 0
    def __call__(self):
        return self.now


async def settle():
    for _ in range(8):
        await asyncio.sleep(0)


class Response:
    def __init__(self, status=200, data=None, headers=None):
        self.status, self.data, self.headers = status, data or [], headers or {}
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def text(self):
        return json.dumps(self.data)


class Session:
    closed = False
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.received = asyncio.Queue()
    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.received.put_nowait(len(self.calls))
        return next(self.responses)


class LimiterTests(unittest.IsolatedAsyncioTestCase):
    def make(self, limit=55, **kwargs):
        self.clock = Clock()
        async def wait(condition, delay):
            await condition.wait()
        self.limiter = Rule34Limiter(limit, 60, clock=self.clock, wait_strategy=wait,
                                    metrics=RuntimeMetrics(), **kwargs)
        return self.limiter

    async def advance(self, value):
        self.clock.now = value
        await self.limiter.wake()
        await settle()

    async def test_rolling_55_then_56_waits_until_oldest_expires(self):
        limiter = self.make()
        for _ in range(55):
            await limiter.acquire()
        pending = asyncio.create_task(limiter.acquire())
        await settle()
        self.assertFalse(pending.done())
        await self.advance(59.999)
        self.assertFalse(pending.done())
        await self.advance(60)
        await pending
        self.assertEqual(list(limiter.timestamps), [60])

    async def test_later_interactive_precedes_background_then_background_gets_turn(self):
        limiter = self.make(limit=1)
        await limiter.acquire()
        order = []
        async def acquire(kind):
            await limiter.acquire(kind)
            order.append(kind)
        tasks = [asyncio.create_task(acquire('background'))]
        await settle()
        tasks += [asyncio.create_task(acquire('interactive')) for _ in range(6)]
        await settle()
        for tick in range(1, 8):
            await self.advance(tick * 60)
        await asyncio.gather(*tasks)
        self.assertEqual(order, ['interactive'] * 5 + ['background', 'interactive'])

    async def test_concurrent_mixed_traffic_never_exceeds_rolling_quota(self):
        limiter = self.make(limit=3)
        sent = []
        async def request(kind):
            async with limiter.slot(kind):
                sent.append(self.clock())
        tasks = [asyncio.create_task(request('background' if i % 2 else 'interactive')) for i in range(12)]
        await settle()
        for tick in range(1, 4):
            await self.advance(tick * 60)
        await asyncio.gather(*tasks)
        self.assertTrue(all(sum(t - 60 < other <= t for other in sent) <= 3 for t in sent))

    async def test_quota_wait_does_not_hold_http_semaphore(self):
        limiter = self.make(limit=1)
        await limiter.acquire()
        semaphore = asyncio.Semaphore(1)
        pending = asyncio.create_task(limiter.acquire('background', semaphore))
        await settle()
        self.assertFalse(semaphore.locked())
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(limiter.fields('background')['queue_depth'], 0)

    async def test_semaphore_release_wakes_next_request_without_burning_quota(self):
        limiter = self.make()
        semaphore = asyncio.Semaphore(1)
        async with limiter.slot('interactive', semaphore):
            async def next_request():
                async with limiter.slot('interactive', semaphore):
                    return True
            pending = asyncio.create_task(next_request())
            await settle()
            self.assertFalse(pending.done())
            self.assertEqual(len(limiter.timestamps), 1)
        self.assertTrue(await pending)
        self.assertEqual(len(limiter.timestamps), 2)

    async def test_retry_retry_request_uses_three_quota_entries(self):
        limiter = self.make()
        api = rule34API(limiter=limiter)
        api.session = Session([Response(500), Response(502), Response(200)])
        with patch('app.integrations.rule34.client.asyncio.sleep', AsyncMock()):
            self.assertEqual(await api.search('tag', set()), [])
        self.assertEqual(len(api.session.calls), 3)
        self.assertEqual(len(limiter.timestamps), 3)
        self.assertEqual(limiter.metrics.counters['rule34_requests_total'], 3)

    async def test_http_429_retry_after_blocks_other_background_and_interactive(self):
        limiter = self.make()
        api = rule34API(limiter=limiter)
        api.session = Session([Response(429, headers={'Retry-After': '10'}), Response(200)])
        with patch('app.integrations.rule34.client.asyncio.sleep', AsyncMock()):
            pending = asyncio.create_task(api.search('tag', set()))
            # patched sleep cannot settle; use loop futures rather than real time.
            for _ in range(8):
                future = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(future.set_result, None)
                await future
            self.assertEqual(len(api.session.calls), 1)
            background = asyncio.create_task(limiter.acquire('background'))
            await limiter.wake()
            self.clock.now = 10
            await limiter.wake()
            await asyncio.gather(pending, background)
        self.assertEqual(limiter.metrics.counters['rule34_http_429_total'], 1)
        self.assertEqual(len(api.session.calls), 2)

    async def test_429_without_header_uses_safe_fallback(self):
        limiter = self.make()
        await limiter.cooldown(None)
        pending = asyncio.create_task(limiter.acquire())
        await settle()
        await self.advance(4.99)
        self.assertFalse(pending.done())
        await self.advance(5)
        await pending

    async def test_shutdown_cancels_all_waiters_and_cleans_gauges(self):
        limiter = self.make(limit=1)
        await limiter.acquire()
        tasks = [asyncio.create_task(limiter.acquire(kind)) for kind in ('interactive', 'background')]
        await settle()
        await limiter.stop()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
        self.assertEqual(limiter.fields('interactive')['queue_depth'], 0)
        self.assertEqual(limiter.metrics.counters['rule34_background_waiters'], 0)

    async def test_queue_is_bounded(self):
        limiter = self.make(limit=1, queue_limit=1)
        await limiter.acquire()
        pending = asyncio.create_task(limiter.acquire())
        await settle()
        with self.assertRaises(QuotaQueueFull):
            await limiter.acquire('background')
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

    async def test_instances_share_the_default_process_quota(self):
        self.assertIs(rule34API().limiter, rule34_limiter)
        self.assertIs(rule34API().limiter, rule34API().limiter)


class SecondsTests(TempDatabaseTestCase):
    async def test_two_due_subscriptions_share_quota_and_yield_to_interactive(self):
        await database.add_subscription(1, 'subscription_a', interval_seconds=30)
        await database.add_subscription(2, 'subscription_b', interval_seconds=60)
        due = await database.get_due_subscriptions()
        self.assertEqual([row[2] for row in due], [30, 60])
        clock = Clock()
        async def wait(condition, delay):
            await condition.wait()
        limiter = Rule34Limiter(1, 60, clock=clock, wait_strategy=wait, metrics=RuntimeMetrics())
        await limiter.acquire()
        api = rule34API(limiter=limiter)
        api.session = Session([Response(data=[{'id': 1, 'file_url': 'https://example.test/a.jpg'}]) for _ in range(3)])
        service = ProgressiveSearch(api)
        tasks = [asyncio.create_task(service.select(row[0], row[1], set(), {}, subscription=True)) for row in due]
        async with asyncio.timeout(3):
            async with limiter.condition:
                await limiter.condition.wait_for(lambda: len(limiter.queues['background']) == 2)
        interactive = asyncio.create_task(api.search('interactive_tag', set()))
        async with limiter.condition:
            await limiter.condition.wait_for(lambda: len(limiter.queues['interactive']) == 1)
        async with asyncio.timeout(3):
            for tick in range(1, 4):
                clock.now = tick * 60
                await limiter.wake()
                await api.session.received.get()
        await asyncio.gather(interactive, *tasks)
        self.assertTrue(api.session.calls[0][1]['params']['tags'].startswith('interactive_tag'))
        self.assertEqual(len(api.session.calls), 3)
        self.assertEqual(limiter.metrics.counters['rule34_requests_total'], 4)

    async def test_create_30_seconds_and_reject_29(self):
        self.assertTrue(await database.add_subscription(1, 'tag', interval_seconds=30))
        rejected = await database.add_subscription(1, 'bad', interval_seconds=29)
        self.assertEqual(rejected.status, 'invalid_interval')
        with self.assertRaises(ValueError):
            await database.update_subscription_interval(1, 'tag', interval_seconds=29)
        self.assertEqual((await database.get_due_subscriptions())[0][2], 30)

    async def test_success_schedules_next_due_in_30_seconds(self):
        await database.add_subscription(1, 'tag', interval_seconds=30)
        await database.update_subscription_time(1, 'tag')
        self.assertEqual(await database.get_due_subscriptions(), [])
        async with database.connect_db() as db:
            row = await (await db.execute("SELECT CAST(strftime('%s',next_check_at) AS INTEGER)-CAST(strftime('%s',last_sent) AS INTEGER) FROM subscriptions")).fetchone()
            self.assertEqual(row[0], 30)
            await db.execute("UPDATE subscriptions SET last_sent=datetime('now','-30 seconds'),next_check_at=datetime('now')")
            await db.commit()
        self.assertEqual((await database.get_due_subscriptions())[0][2], 30)

    async def test_upgrade_minutes_to_seconds_preserves_schedule_and_is_idempotent(self):
        for i, minutes in enumerate((1, 5, 60), 1):
            await database.add_subscription(i, 'tag', minutes)
        async with database.connect_db() as db:
            await db.execute("UPDATE subscriptions SET next_check_at='2030-01-02 03:04:05'")
            await db.execute('DROP TRIGGER subscription_legacy_interval_insert')
            await db.execute('DROP TRIGGER subscription_legacy_interval_update')
            await db.execute('ALTER TABLE subscriptions DROP COLUMN interval_seconds')
            await db.execute('DELETE FROM schema_migrations WHERE version=7')
            await db.commit()
        await database.init_db()
        await database.init_db()
        async with database.connect_db() as db:
            rows = await (await db.execute('SELECT interval_seconds,next_check_at FROM subscriptions ORDER BY user_id')).fetchall()
            versions = await (await db.execute('SELECT count(*) FROM schema_migrations WHERE version=7')).fetchone()
        self.assertEqual(rows, [(seconds, '2030-01-02 03:04:05') for seconds in (60, 300, 3600)])
        self.assertEqual(versions[0], 1)

    async def test_repeat_init_keeps_subminute_interval(self):
        await database.add_subscription(1, 'tag', interval_seconds=30)
        await database.update_subscription_time(1, 'tag')
        await database.init_db()
        self.assertEqual((await database.get_all_user_subscriptions(1))[0][1], 30)

    async def test_quota_deadline_saves_progress_without_http_request(self):
        limiter = Rule34Limiter(1, 60)
        await limiter.acquire()
        api = rule34API(limiter=limiter)
        api.session = Session([])
        with patch('app.services.search.SEARCH_DEADLINE_SECONDS', 0.01):
            with self.assertRaises(SearchBudgetExceeded):
                await ProgressiveSearch(api).select(1, 'tag', set(), {})
        self.assertEqual(api.session.calls, [])
        self.assertEqual(limiter.fields('interactive')['queue_depth'], 0)
        async with database.connect_db() as db:
            row = await (await db.execute("SELECT pid FROM query_progress WHERE kind='search'")).fetchone()
        self.assertEqual(row[0], 0)

    async def test_subscription_quota_deadline_defers_without_empty_backoff(self):
        await database.add_subscription(1, 'tag', interval_seconds=30)
        limiter = Rule34Limiter(1, 60)
        await limiter.acquire()
        api = rule34API(limiter=limiter)
        api.session = Session([])
        service = ProgressiveSearch(api)
        with (patch('app.services.search.SUBSCRIPTION_DEADLINE_SECONDS', 0.01),
              patch.object(bot, 'search_service', service),
              patch.object(bot, 'is_recipient_allowed', return_value=True)):
            self.assertFalse(await bot.process_one_subscription(SimpleNamespace(bot=SimpleNamespace()), (1, 'tag', 30, 0)))
        async with database.connect_db() as db:
            row = await (await db.execute('SELECT no_new_posts_count,processing_token FROM subscriptions')).fetchone()
        self.assertEqual(row, (0, None))
        self.assertEqual(api.session.calls, [])

    async def test_normal_trace_contains_limiter_wait_grant_and_429_fields(self):
        path = Path(self.tempdir) / 'trace.jsonl'
        logic_trace.configure_trace(enabled=True, path=path, level='normal', secrets=[])
        clock = Clock()
        limiter = Rule34Limiter(1, 60, clock=clock, metrics=RuntimeMetrics())
        try:
            with logic_trace.flow_context('subscription', user_id=1):
                await limiter.acquire('background')
                await limiter.cooldown('2', 'background')
                pending = asyncio.create_task(limiter.acquire('background'))
                await settle()
                clock.now = 60
                await limiter.wake()
                await pending
        finally:
            await logic_trace.shutdown_trace()
        events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        required = {'request_kind','used','limit','window_seconds','wait_ms','queue_depth','interactive_waiters','background_waiters'}
        traced = [event for event in events if event['event'].startswith('rule34.limit.')]
        self.assertTrue(all(required <= event.keys() for event in traced))
        self.assertTrue({'rule34.limit.acquire','rule34.limit.wait','rule34.limit.granted','rule34.limit.429','rule34.limit.cooldown'} <= {e['event'] for e in traced})


class IntervalParsingTests(unittest.TestCase):
    def test_units_and_formatting(self):
        for text in ('30s','30 sec','30с','30 сек'):
            self.assertEqual(parse_subscription_interval(text), 30)
        for text in ('1m','1 мин','60s','1'):
            self.assertEqual(parse_subscription_interval(text), 60)
        for value, label in ((30,'30 сек'),(60,'1 мин'),(300,'5 мин'),(3600,'1 ч')):
            self.assertEqual(format_subscription_interval(value), label)
        with self.assertRaises(ValueError):
            parse_subscription_interval('29s')

    def test_scheduler_polls_often_enough(self):
        self.assertLessEqual(config.SUBSCRIPTION_CHECK_INTERVAL_SECONDS, 10)
        self.assertEqual(config.SUBSCRIPTION_MIN_INTERVAL_SECONDS, 30)
