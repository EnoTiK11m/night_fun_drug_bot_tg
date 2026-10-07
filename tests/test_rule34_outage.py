"""Outage admission, recovery, secret safety, and persisted scheduling boundaries."""
import asyncio
from datetime import UTC, datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
import app.storage.database as database
import app.telegram.application as bot
import app.services.subscriptions as subscriptions
from app.integrations.rule34.client import rule34API, APITemporaryError
from app.integrations.rule34.outage import (
    Rule34Outage, Rule34Unavailable, forbidden_diagnostics, unavailable_text,
)
from app.integrations.rule34.rate_limiter import Rule34Limiter, rule34_limiter
from app.services.media_preferences import RuntimeMetrics
from app.services.search import ProgressiveSearch
from test_database_integration import TempDatabaseTestCase


class Clock:
    now = 0.0
    def __call__(self):
        return self.now


class Response:
    def __init__(self, status=200, data=None, *, body=None, headers=None, gate=None):
        self.status = status
        self.body = body if body is not None else json.dumps([] if data is None else data).encode()
        self.headers = headers or {}
        self.content = SimpleNamespace(read=AsyncMock(return_value=self.body))
        self.entered = asyncio.Event()
        self.gate = gate
        self.exited = False

    async def __aenter__(self):
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        return self

    async def __aexit__(self, *args):
        self.exited = True

    async def text(self):
        return self.body.decode()


class Session:
    closed = False
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


async def settle():
    for _ in range(12):
        await asyncio.sleep(0)


class OutageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.metrics = RuntimeMetrics()
        self.breaker = Rule34Outage(clock=self.clock, jitter=lambda: 0,
            wall_clock=lambda: datetime(2026, 10, 7, tzinfo=UTC), metrics=self.metrics)
        async def wait(condition, delay):
            await condition.wait()
        self.limiter = Rule34Limiter(clock=self.clock, wait_strategy=wait, metrics=self.metrics)
        self.api = rule34API(limiter=self.limiter, breaker=self.breaker)

    def responses(self, *responses):
        self.api.session = Session(responses)

    async def unavailable(self, kind='background'):
        with self.assertRaises(Rule34Unavailable) as caught:
            await self.api.search('tag', set(), request_kind=kind)
        return caught.exception

    async def open(self):
        self.responses(Response(403))
        await self.unavailable()

    async def test_single_403_opens_circuit_without_immediate_retries(self):
        self.responses(Response(403), Response(200))
        exc = await self.unavailable()
        self.assertEqual(exc.retry_after_seconds, 60)
        self.assertEqual(len(self.api.session.calls), 1)
        self.assertEqual(self.breaker.consecutive_failures, 1)
        self.assertEqual(self.breaker.physical_failures, 1)
        self.assertEqual(self.breaker.logical_failures, 1)
        self.assertEqual(self.breaker.category, 'http_403_unknown')

    async def test_background_suppression_preserves_physical_quota(self):
        await self.open()
        for _ in range(10):
            await self.unavailable()
        self.assertEqual(len(self.api.session.calls), 1)
        self.assertEqual(list(self.limiter.timestamps), [0])
        self.assertEqual(self.metrics.counters['rule34_requests_total'], 1)
        self.assertEqual(self.metrics.counters['rule34_http_attempts'], 1)
        self.assertEqual(self.metrics.counters['rule34_suppressed_requests'], 10)
        self.assertEqual(self.breaker.logical_failures, 11)
        self.assertEqual(self.breaker.consecutive_failures, 1)

    async def test_backoff_progression_caps_at_900_and_success_resets_it(self):
        self.responses(*(Response(403) for _ in range(7)), Response(200, [{'id': 42}]), Response(403))
        for delay in (60, 120, 300, 600, 900, 900, 900):
            await self.unavailable()
            self.assertEqual(self.breaker.backoff_seconds, delay)
            self.assertEqual(self.breaker.cooldown_until, self.clock.now + delay)
            self.clock.now = self.breaker.cooldown_until
        self.assertEqual(await self.api.search('tag', set()), [{'id': 42}])
        self.assertFalse(self.breaker.is_open)
        self.assertEqual(self.breaker.backoff_seconds, 0)
        self.assertEqual(self.breaker.consecutive_failures, 0)
        self.assertEqual(self.breaker.logical_failures, 0)
        self.assertIsNotNone(self.breaker.last_success_at)
        self.assertEqual(self.metrics.counters['rule34_recoveries'], 1)
        await self.unavailable()
        self.assertEqual(self.breaker.backoff_seconds, 60)

    async def test_cooldown_boundary_and_one_probe_among_ten_contenders(self):
        await self.open()
        self.clock.now = 59.999
        await self.unavailable()
        self.clock.now = 60
        gate = asyncio.Event()
        response = Response(200, [{'id': 7}], gate=gate)
        self.responses(response)
        tasks = [asyncio.create_task(self.api.search('tag', set(), request_kind='background')) for _ in range(10)]
        await response.entered.wait()
        await settle()
        self.assertEqual(len(self.api.session.calls), 1)
        self.assertEqual(sum(t.done() for t in tasks), 9)
        gate.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(results.count([{'id': 7}]), 1)
        self.assertEqual(sum(isinstance(r, Rule34Unavailable) for r in results), 9)
        self.assertEqual(self.metrics.counters['rule34_probes'], 1)
        self.assertFalse(self.breaker.is_open)

    async def test_queued_closed_requests_are_rejected_before_quota_after_first_403(self):
        gate = asyncio.Event()
        response = Response(403, gate=gate)
        self.responses(response)
        tasks = [asyncio.create_task(self.api.search('tag', set(), request_kind='background')) for _ in range(10)]
        await response.entered.wait()
        await settle()
        self.assertEqual(len(self.limiter.queues['background']), 9)
        gate.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(isinstance(r, Rule34Unavailable) for r in results))
        self.assertEqual(len(self.api.session.calls), 1)
        self.assertEqual(len(self.limiter.timestamps), 1)
        self.assertEqual(self.limiter.fields('background')['queue_depth'], 0)
        self.assertFalse(self.api.background_semaphore.locked())

    async def test_failed_probe_reopens_with_increased_backoff(self):
        await self.open()
        self.clock.now = 60
        self.responses(Response(403))
        await self.unavailable()
        self.assertEqual(self.breaker.cooldown_until, 180)
        self.assertEqual(self.breaker.probes, 1)
        self.assertEqual(self.breaker.consecutive_failures, 2)
        self.assertIsNone(self.breaker.probe)

    async def test_cancelled_probe_releases_reservation_and_defers_next_probe(self):
        await self.open()
        self.clock.now = 60
        response = Response(200, gate=asyncio.Event())
        self.responses(response)
        task = asyncio.create_task(self.api.search('tag', set(), request_kind='background'))
        await response.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.breaker.probe)
        self.assertEqual(self.breaker.cooldown_until, 120)
        self.assertFalse(self.api.background_semaphore.locked())
        self.responses(Response(200))
        self.clock.now = 120
        self.assertEqual(await self.api.search('tag', set()), [])
        self.assertFalse(self.breaker.is_open)

    async def test_cancelled_probe_in_quota_queue_does_not_burn_quota(self):
        await self.open()
        self.clock.now = 60
        for _ in range(55):
            await self.limiter.acquire()
        self.responses()
        task = asyncio.create_task(self.api.search('tag', set()))
        await settle()
        self.assertIsNotNone(self.breaker.probe)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.limiter.timestamps), 55)
        self.assertEqual(self.api.session.calls, [])
        self.assertIsNone(self.breaker.probe)
        self.assertEqual(self.limiter.fields('interactive')['queue_depth'], 0)

    async def test_limiter_shutdown_cleans_reserved_probe(self):
        await self.open()
        self.clock.now = 60
        for _ in range(55):
            await self.limiter.acquire()
        task = asyncio.create_task(self.api.search('tag', set()))
        await settle()
        await self.limiter.stop()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.breaker.probe)
        self.assertEqual(self.limiter.fields('interactive')['queue_depth'], 0)

    async def test_probe_non403_failure_keeps_circuit_without_fast_retries(self):
        await self.open()
        self.clock.now = 60
        self.responses(Response(503), Response(200))
        await self.unavailable()
        self.assertEqual(len(self.api.session.calls), 1)
        self.assertTrue(self.breaker.is_open)
        self.assertEqual(self.breaker.backoff_seconds, 60)
        self.assertEqual(self.breaker.cooldown_until, 120)

    async def test_invalid_json_does_not_close_probe(self):
        await self.open()
        self.clock.now = 60
        self.responses(Response(200, body=b'not json'))
        await self.unavailable()
        self.assertTrue(self.breaker.is_open)
        self.assertEqual(self.breaker.recoveries, 0)

    async def test_provider_error_does_not_close_probe(self):
        await self.open()
        self.clock.now = 60
        self.responses(Response(200, {'success': False, 'message': 'denied'}))
        await self.unavailable()
        self.assertTrue(self.breaker.is_open)

    async def test_stale_inflight_success_cannot_close_new_outage(self):
        old = self.breaker.admit('interactive')
        new = self.breaker.admit('background')
        self.breaker.dispatched(new, 'background', 'post_search')
        self.breaker.forbidden(new, 'background', 'http_403_unknown')
        self.breaker.success(old, 'interactive')
        self.assertTrue(self.breaker.is_open)
        self.assertEqual(self.breaker.consecutive_failures, 1)

    async def test_parallel_successes_while_closed_do_not_invalidate_each_other(self):
        gate = asyncio.Event()
        first = Response(200, [{'id': 1}], gate=gate)
        self.responses(first, Response(200, [{'id': 2}]))
        pending = asyncio.create_task(self.api.search('a', set()))
        await first.entered.wait()
        self.assertEqual(await self.api.search('b', set()), [{'id': 2}])
        gate.set()
        self.assertEqual(await pending, [{'id': 1}])
        self.assertFalse(self.breaker.is_open)

    async def test_recovery_probe_still_waits_for_physical_quota(self):
        await self.open()
        self.clock.now = 60
        for _ in range(55):
            await self.limiter.acquire()
        self.responses(Response(200, [{'id': 9}]))
        pending = asyncio.create_task(self.api.search('tag', set()))
        await settle()
        self.assertEqual(self.api.session.calls, [])
        self.assertEqual(len(self.limiter.timestamps), 55)
        self.clock.now = 120
        await self.limiter.wake()
        self.assertEqual(await pending, [{'id': 9}])
        self.assertEqual(list(self.limiter.timestamps), [120])
        self.assertFalse(self.breaker.is_open)

    async def test_interactive_cannot_bypass_backoff_and_has_retry_message(self):
        await self.open()
        exc = await self.unavailable('interactive')
        self.assertIn('~1 мин', unavailable_text(exc))
        self.assertEqual(len(self.api.session.calls), 1)

    async def test_lookup_and_autocomplete_share_circuit(self):
        await self.open()
        self.assertIsNone(await self.api.get_post_by_id(42))
        self.assertEqual(await self.api.autocomplete('tag'), [])
        self.clock.now = 60
        self.assertEqual(await self.api.autocomplete('tag'), [])
        self.assertEqual(len(self.api.session.calls), 1)
        self.responses(Response(200, [{'id': 42}]))
        self.assertEqual(await self.api.get_post_by_id(42), {'id': 42})
        self.assertFalse(self.breaker.is_open)

    async def test_non403_http_retries_remain_three_physical_attempts(self):
        self.responses(Response(500), Response(502), Response(200, [{'id': 42}]))
        with patch('app.integrations.rule34.client.asyncio.sleep', AsyncMock()) as sleep:
            result = await self.api.search('tag', set())
        self.assertEqual(result, [{'id': 42}])
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [1, 2])
        self.assertEqual(self.metrics.counters['rule34_requests_total'], 3)
        self.assertFalse(self.breaker.is_open)

    async def test_network_failure_does_not_open_403_circuit(self):
        class BrokenSession:
            closed = False
            def get(self, *args, **kwargs):
                raise aiohttp.ClientConnectionError('offline')
        self.api.session = BrokenSession()
        with patch('app.integrations.rule34.client.asyncio.sleep', AsyncMock()):
            with self.assertRaises(APITemporaryError):
                await self.api.search('tag', set())
        self.assertFalse(self.breaker.is_open)
        self.assertEqual(self.breaker.consecutive_failures, 3)
        self.assertEqual(self.breaker.logical_failures, 1)

    async def test_sustained_5xx_still_alerts_without_opening_circuit(self):
        sent = []
        self.breaker.notifier = AsyncMock(side_effect=lambda message: sent.append(message))
        self.responses(*(Response(503) for _ in range(6)), Response(200))
        with patch('app.integrations.rule34.client.asyncio.sleep', AsyncMock()):
            for _ in range(2):
                with self.assertRaises(APITemporaryError):
                    await self.api.search('tag', set())
        self.assertFalse(self.breaker.is_open)
        self.assertEqual(len(sent), 1)
        self.assertIn('HTTP 503', sent[0])
        self.assertIn('http_5xx', sent[0])
        self.assertEqual(await self.api.search('tag', set()), [])
        self.assertEqual(sum(message.startswith('✅') for message in sent), 1)
        self.assertEqual(self.breaker.consecutive_failures, 0)

    async def test_notifications_throttle_and_recovery_is_sent_once(self):
        sent = []
        self.breaker.notifier = AsyncMock(side_effect=lambda message: sent.append(message))
        await self.open()
        self.assertEqual(len(sent), 1)
        self.assertIn('HTTP 403', sent[0])
        self.assertIn('physical failures=1', sent[0])
        self.assertIn('backoff=60s', sent[0])
        await self.unavailable()
        self.clock.now = 899
        await self.breaker.notify()
        self.assertEqual(len(sent), 1)
        self.clock.now = 900
        await self.breaker.notify()
        self.assertEqual(len(sent), 2)
        self.responses(Response(200))
        await self.api.search('tag', set())
        await self.breaker.notify()
        await self.api.breaker.notify()
        self.assertEqual(sum(message.startswith('✅') for message in sent), 1)

    async def test_notification_failure_does_not_break_recovery_or_spam(self):
        self.breaker.notifier = AsyncMock(side_effect=RuntimeError('notification failed'))
        await self.open()
        await self.unavailable()
        self.assertEqual(self.breaker.notifier.await_count, 1)
        self.clock.now = 60
        self.responses(Response(200))
        self.assertEqual(await self.api.search('tag', set()), [])
        self.assertFalse(self.breaker.is_open)

    async def test_safe_diagnostic_and_log_do_not_echo_credentials(self):
        secrets = ['SECRET_API_KEY', 'SECRET_USER_CREDENTIAL', 'SECRET_COOKIE']
        response = Response(403, body=(' '.join(secrets)).encode(), headers={
            'Server': secrets[0], 'Content-Type': secrets[1], 'Retry-After': secrets[2],
            'Set-Cookie': secrets[2], 'Authorization': secrets[0], 'CF-Ray': secrets[1],
        })
        self.responses(response)
        with patch('app.integrations.rule34.client.API_KEY', secrets[0]), \
             patch('app.integrations.rule34.client.API_USER_ID', secrets[1]), \
             self.assertLogs('app.integrations.rule34.client', level='WARNING') as logs:
            await self.unavailable()
        diagnostic = forbidden_diagnostics(response.headers, response.body)
        output = '\n'.join(logs.output) + json.dumps(diagnostic)
        for secret in secrets:
            self.assertNotIn(secret, output)
        self.assertNotIn('snippet', diagnostic)
        self.assertNotIn('api_key', output)
        self.assertEqual(diagnostic['body_sample_bytes'], len(response.body))

    async def test_diagnostics_read_failure_still_opens_circuit(self):
        response = Response(403)
        response.content.read.side_effect = aiohttp.ClientPayloadError('broken body')
        self.responses(response)
        await self.unavailable()
        self.assertTrue(self.breaker.is_open)
        self.assertEqual(self.breaker.physical_failures, 1)

    async def test_cancel_during_diagnostic_does_not_lose_observed_403(self):
        response = Response(403)
        reading = asyncio.Event()
        async def read(limit):
            reading.set()
            await asyncio.Event().wait()
        response.content.read.side_effect = read
        self.responses(response)
        task = asyncio.create_task(self.api.search('tag', set()))
        await reading.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.breaker.is_open)
        self.assertFalse(self.api.interactive_semaphore.locked())

    async def test_four_hour_ten_subscription_simulation(self):
        # Independent production cadence: ten logical failures per ~71 seconds.
        old_attempts = len(range(0, 14400, 71)) * 10 * 3
        physical_times = []
        class OutageSession:
            closed = False
            def get(session, *args, **kwargs):
                physical_times.append(self.clock.now)
                return Response(403)
        self.api.session = OutageSession()
        due = [0] * 10
        while min(due) < 14400:
            self.clock.now = min(due)
            for i in range(10):
                if due[i] <= self.clock.now:
                    await self.unavailable()
                    due[i] = self.clock.now + self.breaker.subscription_delay()
        self.assertEqual(old_attempts, 6090)
        self.assertEqual(len(physical_times), 19)
        self.assertTrue(all(b - a >= 60 for a, b in zip(physical_times, physical_times[1:])))
        self.assertEqual(self.metrics.counters['rule34_http_attempts'], 19)
        self.assertTrue(self.breaker.is_open)

    def test_default_clients_share_breaker_and_existing_limiter(self):
        self.assertIs(rule34API().breaker, rule34API().breaker)
        self.assertIs(rule34API().limiter, rule34_limiter)


class DiagnosticTests(unittest.TestCase):
    def test_echoed_credentials_matching_header_formats_are_redacted(self):
        api_key, user_credential = '0123456789abcdef-MXP', '123456789'
        result = forbidden_diagnostics({'CF-Ray': api_key, 'Content-Length': user_credential},
                                       b'Forbidden', secrets=(api_key, user_credential))
        self.assertNotIn('cf_ray', result)
        self.assertNotIn('content_length', result)
        self.assertNotIn(api_key, json.dumps(result))
        self.assertNotIn(user_credential, json.dumps(result))

    def test_classification_fixtures(self):
        fixtures = [
            ({}, b'', 'empty', 'http_403_unknown'),
            ({}, b'{"success":false,"message":"denied"}', 'json_error', 'http_403_unknown'),
            ({}, b'{"success":false,"message":"invalid api key"}', 'auth_like', 'http_403_auth_like'),
            ({'Server': 'cloudflare'}, b'<html>cf-challenge-platform cloudflare</html>', 'cloudflare_challenge', 'http_403_cloudflare_challenge'),
            ({}, b'<html>Forbidden</html>', 'html_forbidden', 'http_403_unknown'),
            ({}, b'access denied', 'unknown_text', 'http_403_unknown'),
            ({}, b'cloudflare', 'unknown_text', 'http_403_unknown'),
        ]
        for headers, body, classification, category in fixtures:
            with self.subTest(classification=classification, body=body):
                result = forbidden_diagnostics(headers, body)
                self.assertEqual(result['body_classification'], classification)
                self.assertEqual(result['category'], category)

    def test_allowlisted_headers_and_bounded_body(self):
        result = forbidden_diagnostics({
            'Content-Type': 'text/html; charset=UTF-8', 'Content-Length': '1234',
            'Server': 'cloudflare', 'Retry-After': '60', 'CF-Ray': '0123456789abcdef-MXP',
            'CF-Mitigated': 'challenge', 'CF-Cache-Status': 'DYNAMIC', 'Cookie': 'private',
        }, b'x' * 10000)
        self.assertEqual(result['body_sample_bytes'], 8192)
        self.assertEqual(result['cf_ray'], '0123456789abcdef-MXP')
        self.assertEqual(result['retry_after'], '60')
        self.assertEqual(result['body_classification'], 'cloudflare_challenge')
        self.assertNotIn('Cookie', result)


class SubscriptionOutageTests(TempDatabaseTestCase):
    async def make_api(self):
        self.clock = Clock()
        self.breaker = Rule34Outage(clock=self.clock, jitter=lambda: 15, metrics=RuntimeMetrics())
        self.api = rule34API(breaker=self.breaker, limiter=Rule34Limiter(metrics=RuntimeMetrics()))
        self.api.session = Session([Response(403)])
        await database.add_subscription(1, 'tag', interval_seconds=30)

    async def test_worker_persists_defer_beyond_cooldown_and_releases_claim(self):
        await self.make_api()
        before = datetime.now(UTC).timestamp()
        with patch.object(bot, 'api', self.api), \
             patch.object(bot, 'search_service', ProgressiveSearch(self.api)), \
             patch.object(bot, 'is_recipient_allowed', return_value=True):
            self.assertFalse(await bot.process_one_subscription(SimpleNamespace(bot=object()), (1, 'tag', 30, 0)))
        async with database.connect_db() as db:
            row = await (await db.execute('SELECT next_check_at,processing_until,processing_token,no_new_posts_count FROM subscriptions')).fetchone()
        next_check = datetime.fromisoformat(row[0]).replace(tzinfo=UTC).timestamp()
        self.assertGreaterEqual(next_check, before + 75)
        self.assertEqual(row[1:], (None, None, 0))
        self.assertEqual(await database.get_due_subscriptions(), [])
        self.assertEqual(len(self.api.session.calls), 1)
        self.assertEqual(subscriptions._delivery_locks, {})

    async def test_cache_success_preserves_api_outage(self):
        await self.make_api()
        cached = {'id': 42, 'file_url': 'https://example.test/a.jpg'}
        with patch.object(bot, 'api', self.api), \
             patch.object(bot, 'search_service', ProgressiveSearch(self.api)), \
             patch.object(bot, 'get_subscription_cache', AsyncMock(return_value=([cached], None))):
            result = await bot.get_subscription_cached_image(1, 'tag', set(), set(), {})
        self.assertEqual(result, cached)
        self.assertTrue(self.breaker.is_open)
        self.assertEqual(self.breaker.consecutive_failures, 1)
        self.assertIsNone(self.breaker.last_success_at)

    async def test_full_worker_cache_delivery_does_not_reset_health(self):
        await self.make_api()
        before = datetime.now(UTC).timestamp()
        cached = {'id': 42, 'file_url': 'https://example.test/a.jpg'}
        with patch.object(bot, 'api', self.api), \
             patch.object(bot, 'search_service', ProgressiveSearch(self.api)), \
             patch.object(bot, 'get_subscription_cache', AsyncMock(return_value=([cached], None))), \
             patch.object(bot, 'send_post_media_to_chat', AsyncMock(return_value=True)), \
             patch.object(bot, 'is_recipient_allowed', return_value=True):
            self.assertTrue(await bot.process_one_subscription(SimpleNamespace(bot=object()), (1, 'tag', 30, 0)))
        async with database.connect_db() as db:
            row = await (await db.execute('SELECT processing_token,next_check_at,last_sent,no_new_posts_count FROM subscriptions')).fetchone()
            history = await (await db.execute('SELECT post_id FROM subscription_user_delivery_history WHERE user_id=1')).fetchall()
        self.assertIsNone(row[0])
        self.assertGreaterEqual(datetime.fromisoformat(row[1]).replace(tzinfo=UTC).timestamp(), before + 75)
        self.assertIsNotNone(row[2])
        self.assertEqual(row[3], 0)
        self.assertEqual(history, [(42,)])
        self.assertTrue(self.breaker.is_open)
        self.assertEqual(self.breaker.consecutive_failures, 1)
        self.assertIsNone(self.breaker.last_success_at)

    async def test_ten_persisted_defers_stagger_recovery_without_http_herd(self):
        await self.make_api()
        for i in range(1, 10):
            await database.add_subscription(1, f'tag{i}', interval_seconds=30)
        jitter = iter(range(10))
        self.breaker.jitter = lambda: next(jitter)
        with patch.object(bot, 'api', self.api), \
             patch.object(bot, 'search_service', ProgressiveSearch(self.api)), \
             patch.object(bot, 'is_recipient_allowed', return_value=True):
            for query in ['tag'] + [f'tag{i}' for i in range(1, 10)]:
                self.assertFalse(await bot.process_one_subscription(SimpleNamespace(bot=object()), (1, query, 30, 0)))
        async with database.connect_db() as db:
            rows = await (await db.execute('SELECT next_check_at,processing_until,processing_token FROM subscriptions')).fetchall()
        self.assertEqual(len({r[0] for r in rows}), 10)
        self.assertTrue(all(r[1:] == (None, None) for r in rows))
        self.assertEqual(await database.get_due_subscriptions(), [])
        self.assertEqual(len(self.api.session.calls), 1)
        self.assertEqual(subscriptions._delivery_locks, {})


if __name__ == '__main__':
    unittest.main()
