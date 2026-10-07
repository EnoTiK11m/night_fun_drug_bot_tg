"""Domain evidence and health policy, without changing delivery decisions."""
from contextlib import ExitStack
from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from httpx import ReadTimeout
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from app.observability.errors import error_details
from app.observability.health import RuntimeHealth
from app.observability.diagnostics import format_snapshot
import app.observability.logic_trace as trace
import app.storage.database as database
import app.telegram.application as bot
import app.telegram.media as media
from app.telegram.delivery import TelegramRateLimiter


def ambiguous_timeout():
    error = TimedOut()
    error.__cause__ = ReadTimeout('read response lost')
    return error


class ClassificationAndHealthTests(unittest.TestCase):
    def test_domain_timeout_survives_generic_components_and_wrapper(self):
        error = ambiguous_timeout()
        wrapper = RuntimeError('outer flow')
        wrapper.__cause__ = error
        for component in ('app', 'media', 'subscription', 'search', 'telegram'):
            for exception in (error, wrapper):
                with self.subTest(component=component, wrapper=exception is wrapper):
                    details = error_details(exception, component)
                    self.assertEqual(details['error_category'], 'telegram_timeout_ambiguous')
                    self.assertEqual(details['root_error_type'], 'ReadTimeout')
                    self.assertEqual(details['root_error_message'], 'read response lost')

    def test_domain_timeout_precedes_transport_http_status(self):
        error = ambiguous_timeout()
        error.__cause__.status_code = 504
        details = error_details(error)
        self.assertEqual(details['error_category'], 'telegram_timeout_ambiguous')
        self.assertEqual(details['root_http_status'], 504)

    def test_suppressed_domain_context_does_not_classify_new_error(self):
        error = ValueError('new validation failure')
        error.__context__, error.__suppress_context__ = ambiguous_timeout(), True
        self.assertEqual(error_details(error)['error_category'], 'invalid_state')

    def test_non_telegram_http_error_keeps_status_taxonomy(self):
        self.assertEqual(error_details(RuntimeError('HTTP 403'))['error_category'], 'http_403')

    def test_bad_request_aggregates_as_local_without_false_recovery(self):
        health = RuntimeHealth(clock=lambda:100)
        details = error_details(BadRequest('Photo_invalid_dimensions'))
        first = health.error('telegram', 'send_photo', details, 'request', 1)
        repeated = health.error('telegram', 'send_photo', details, 'request', 1)
        self.assertEqual(repeated['incident_id'], first['incident_id'])
        self.assertEqual((repeated['failures'], repeated['observations']), (1, 2))
        self.assertEqual(health.success('telegram'), [])
        snapshot = health.snapshot(errors=True)
        self.assertFalse(snapshot['components']['telegram']['degraded'])
        self.assertEqual(snapshot['active_incidents'], 0)
        self.assertEqual(snapshot['incidents'][0]['outcome'], 'operation_local')
        self.assertNotIn('recovered_at', snapshot['incidents'][0])

    def test_distinct_bad_request_roots_have_safe_diagnostic_reasons(self):
        health = RuntimeHealth()
        for message in ('Photo_invalid_dimensions', 'Wrong type of the web page content',
                        'Failed to get http url content'):
            health.error('telegram', 'send_photo', error_details(BadRequest(message)))
        incidents = health.snapshot(errors=True)['incidents']
        self.assertEqual(len({i['incident_id'] for i in incidents}), 3)
        self.assertEqual({i['safe_reason'] for i in incidents},
                         {'unsupported_dimensions', 'wrong_content_type', 'telegram_fetch_failed'})

    def test_unknown_reason_is_neutral_and_formatter_does_not_expose_message(self):
        health = RuntimeHealth()
        health.error('telegram', 'send_photo', error_details(BadRequest('private https://host/secret')))
        text = format_snapshot(health.snapshot(errors=True), timezone=UTC)
        self.assertIn('reason=other_bad_request', text)
        self.assertIn('state=operation_local', text)
        self.assertNotIn('https://host', text)
        self.assertNotIn('private', text)

    def test_network_failure_then_success_recovers_only_systemic_incident(self):
        now = [0]
        health = RuntimeHealth(clock=lambda:now[0])
        health.error('telegram', 'send_photo', error_details(BadRequest('invalid dimensions')))
        for n in range(3):
            health.error('telegram', 'send_photo', error_details(NetworkError('offline')), f'req{n}', 1)
        self.assertTrue(health.snapshot()['components']['telegram']['degraded'])
        self.assertEqual(health.snapshot()['active_incidents'], 1)
        now[0] = 10
        recovered = health.success('telegram', categories={'telegram_network'})
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]['failures'], 3)
        snapshot = health.snapshot(errors=True)
        self.assertFalse(snapshot['components']['telegram']['degraded'])
        self.assertEqual(snapshot['active_incidents'], 0)
        self.assertEqual([i['outcome'] for i in snapshot['incidents']], ['operation_local', 'recovered'])

    def test_local_error_does_not_mask_systemic_outage(self):
        health = RuntimeHealth()
        health.error('telegram', 'send_photo', error_details(NetworkError('offline')))
        health.error('telegram', 'send_photo', error_details(BadRequest('invalid dimensions')))
        self.assertTrue(health.snapshot()['components']['telegram']['degraded'])
        health.success('telegram', categories={'telegram_network'})
        self.assertFalse(health.snapshot()['components']['telegram']['degraded'])
        self.assertEqual(health.snapshot()['components']['telegram']['error_category'], 'telegram_bad_request')

    def test_cooldown_stays_degraded_despite_unrelated_inflight_success(self):
        health = RuntimeHealth()
        health.error('telegram', 'send_photo', error_details(RetryAfter(5253)))
        health.update('telegram', cooldown_remaining_seconds=5253)
        self.assertEqual(health.success('telegram'), [])
        self.assertTrue(health.snapshot()['components']['telegram']['degraded'])
        health.update('telegram', cooldown_remaining_seconds=0)
        self.assertTrue(health.snapshot()['components']['telegram']['degraded'])
        self.assertEqual(len(health.success('telegram', categories={'telegram_retry_after'})), 1)
        self.assertFalse(health.snapshot()['components']['telegram']['degraded'])

    def test_cooldown_observation_degrades_even_without_error_incident(self):
        health = RuntimeHealth()
        health.update('telegram', cooldown_remaining_seconds=10)
        health.error('telegram', 'send_photo', error_details(BadRequest('invalid dimensions')))
        self.assertTrue(health.snapshot()['components']['telegram']['degraded'])
        health.update('telegram', cooldown_remaining_seconds=0)
        self.assertFalse(health.snapshot()['components']['telegram']['degraded'])

    def test_diag_separates_last_error_from_healthy_subsystem(self):
        now = [64]
        health = RuntimeHealth(clock=lambda:now[0])
        health.error('telegram', 'send_photo', error_details(BadRequest('invalid dimensions')))
        now[0] = 196
        health.success('telegram')
        text = format_snapshot(health.snapshot(errors=True), now=200, timezone=UTC)
        self.assertIn('Telegram success_age=4s error_age=136s category=telegram_bad_request degraded=False cooldown=0s', text)
        self.assertIn('App active_incidents=0', text)
        self.assertIn('state=operation_local', text)


class FlowCorrectnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='observability_correctness_')
        self.path = Path(self.directory.name)/'trace.jsonl'
        self.health = RuntimeHealth()
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(trace, 'runtime_health', self.health))
        trace.configure_trace(enabled=True, path=self.path, level='normal', secrets=['BOTSECRET'])

    async def asyncTearDown(self):
        await trace.shutdown_trace()
        self.stack.close()
        self.directory.cleanup()

    async def events(self):
        await trace.shutdown_trace()
        return [json.loads(line) for line in self.path.read_text(encoding='utf-8').splitlines()]

    async def test_actual_telegram_timeout_propagates_through_media_and_search(self):
        limiter = TelegramRateLimiter()
        operation = AsyncMock(side_effect=ambiguous_timeout())
        async def reply(*args, **kwargs):
            return await limiter.execute(operation, operation_name='send_photo', chat_id=1)
        post = dict(id=42, file_url='https://example.test/photo.jpg')
        with patch.object(media, 'get_media_url_candidates', return_value=[('file_url', post['file_url'])]), \
             patch.object(media, 'reply_media_url', reply):
            with self.assertRaises(TimedOut):
                with trace.flow_context('search'):
                    await media.send_post_media(SimpleNamespace(), post, retries=2)
        operation.assert_awaited_once()  # No new timeout retry or media fallback.
        events = await self.events()
        for name in ('telegram.request.finished', 'media.delivery.finished', 'search.finished'):
            final = next(e for e in events if e['event']==name)
            self.assertEqual(final['error_category'], 'telegram_timeout_ambiguous', name)
            self.assertEqual(final['root_error_type'], 'ReadTimeout', name)
            self.assertEqual(final['user_impact'], 'unknown_delivery', name)
            self.assertEqual(final['outcome'], 'failed', name)  # Existing outcome retained.

    async def test_wrapped_generic_error_preserves_observed_domain_and_root(self):
        timeout = ambiguous_timeout()
        with self.assertRaises(RuntimeError):
            with trace.flow_context('search'):
                with trace.request_context('telegram', 'send_photo'):
                    trace.next_attempt()
                    trace.trace_error(timeout, 'send_photo', 'telegram.send.failed')
                wrapped = RuntimeError('higher layer')
                wrapped.__cause__ = timeout
                raise wrapped
        final = next(e for e in await self.events() if e['event']=='search.finished')
        self.assertEqual(final['error_category'], 'telegram_timeout_ambiguous')
        self.assertEqual(final['root_error_type'], 'ReadTimeout')
        self.assertEqual(final['user_impact'], 'unknown_delivery')

    async def test_generic_root_only_reobservation_keeps_domain_origin(self):
        timeout = ambiguous_timeout()
        with trace.flow_context('search'):
            with trace.request_context('telegram', 'send_photo'):
                trace.next_attempt()
                trace.trace_error(timeout, 'send_photo', 'telegram.send.failed')
            trace.trace_error(timeout.__cause__, 'search', 'search.error')
            trace.outcome('telegram_error')
        final = next(e for e in await self.events() if e['event']=='search.finished')
        self.assertEqual(final['error_category'], 'telegram_timeout_ambiguous')
        self.assertEqual(final['root_error_type'], 'ReadTimeout')
        self.assertEqual(final['user_impact'], 'unknown_delivery')
        self.assertEqual(self.health.snapshot(errors=True)['incidents'][0]['failures'], 1)

    async def test_bad_request_then_real_limited_fallback_keeps_telegram_healthy(self):
        limiter = TelegramRateLimiter()
        operation = AsyncMock(side_effect=[BadRequest('Photo_invalid_dimensions'), True])
        async def reply(*args, **kwargs):
            return await limiter.execute(operation, operation_name='send_photo', chat_id=1)
        post = dict(id=42, file_url='https://example.test/original.jpg', sample_url='https://example.test/sample.jpg')
        with patch.object(media, 'get_media_url_candidates', return_value=[('file_url', post['file_url']), ('sample_url', post['sample_url'])]), \
             patch.object(media, 'reply_media_url', reply):
            with trace.flow_context('search'):
                self.assertTrue(await media.send_post_media(SimpleNamespace(), post, retries=1))
        final = next(e for e in await self.events() if e['event']=='media.delivery.finished')
        self.assertEqual((final['outcome'], final['user_impact']), ('fallback_success', 'delivered'))
        snapshot = self.health.snapshot(errors=True)
        self.assertFalse(snapshot['components']['telegram']['degraded'])
        self.assertEqual(snapshot['active_incidents'], 0)
        self.assertEqual(snapshot['incidents'][0]['outcome'], 'operation_local')

    async def test_unrelated_root_does_not_inherit_previous_domain_classification(self):
        with trace.flow_context('search'):
            with trace.request_context('telegram', 'send_photo'):
                trace.next_attempt()
                trace.trace_error(ambiguous_timeout(), 'send_photo', 'telegram.send.failed')
            trace.trace_error(ReadTimeout('different transport failure'), 'search', 'search.error')
        error = next(e for e in await self.events() if e['event']=='search.error')
        self.assertEqual(error['error_category'], 'invalid_state')
        self.assertEqual(error['root_error_message'], 'different transport failure')
        self.assertEqual(len(self.health.snapshot(errors=True)['incidents']), 2)

    async def test_full_retry_after_health_recovers_after_success_only(self):
        now = [0]
        limiter = TelegramRateLimiter(clock=lambda:now[0])
        limiter.apply_retry_after(1, RetryAfter(5253))
        self.assertTrue(self.health.snapshot()['components']['telegram']['degraded'])
        now[0] = 5254
        await limiter.execute(AsyncMock(return_value=True), operation_name='send_photo', chat_id=1)
        self.assertFalse(self.health.snapshot()['components']['telegram']['degraded'])
        self.assertTrue(any(e['event']=='telegram.cooldown.recovered' for e in await self.events()))

    async def test_trace_disabled_local_error_still_does_not_degrade(self):
        await trace.shutdown_trace()
        trace.configure_trace(enabled=False, path=self.path)
        with trace.flow_context('media.delivery'):
            trace.trace_error(BadRequest('invalid dimensions'), 'send_photo', 'telegram.send.failed')
            trace.trace_event('telegram.send.success')
        self.assertFalse(self.health.snapshot()['components']['telegram']['degraded'])
        self.assertEqual(self.path.read_text(encoding='utf-8'), '')

    async def test_actual_subscription_timeout_preserves_sqlite_defer_without_ack(self):
        path = Path(self.directory.name)/'worker.db'
        self.stack.enter_context(patch.object(database, 'DB_PATH', str(path)))
        await database.init_db()
        await database.add_subscription(1, 'tag', interval_seconds=30)
        limiter = TelegramRateLimiter()
        operation = AsyncMock(side_effect=ambiguous_timeout())
        async def send(*args, before_send, **kwargs):
            self.assertTrue(await before_send())
            return await limiter.execute(operation, operation_name='send_photo', chat_id=1)
        self.stack.enter_context(patch.object(bot, 'is_recipient_allowed', return_value=True))
        self.stack.enter_context(patch.object(bot, 'get_user_settings', AsyncMock(return_value={'show_caption':False})))
        self.stack.enter_context(patch.object(bot, 'get_subscription_cached_image', AsyncMock(return_value={'id':42})))
        self.stack.enter_context(patch.object(bot, 'remember_and_cache_post', AsyncMock()))
        self.stack.enter_context(patch.object(bot, 'send_post_media_to_chat', send))
        bot.user_operation_gate.reset_for_tests()
        try:
            self.assertFalse(await bot.process_one_subscription(SimpleNamespace(bot=object()), (1, 'tag', 30, 0)))
        finally:
            bot.user_operation_gate.reset_for_tests()
        operation.assert_awaited_once()
        async with database.connect_db() as db:
            row = await (await db.execute("SELECT processing_token, processing_until, CAST(strftime('%s', next_check_at) AS INTEGER)-CAST(strftime('%s', 'now') AS INTEGER) FROM subscriptions WHERE user_id=1 AND query='tag'")).fetchone()
            self.assertEqual(row[:2], (None, None))
            self.assertGreaterEqual(row[2], 1790)
            self.assertLessEqual(row[2], 1800)
            for table in ('sent_posts', 'subscription_delivery_history',
                          'subscription_user_delivery_history', 'delivery_failures'):
                self.assertEqual((await (await db.execute(f'SELECT COUNT(*) FROM {table}')).fetchone())[0], 0, table)
        final = next(e for e in await self.events() if e['event']=='subscription.finished')
        self.assertEqual((final['outcome'], final['user_impact']), ('failed', 'unknown_delivery'))
        self.assertEqual(final['error_category'], 'telegram_timeout_ambiguous')
        self.assertEqual(final['root_error_type'], 'ReadTimeout')
        self.assertFalse(any(e['event']=='subscription.delivery.success' for e in await self.events()))
