import asyncio
import json
from pathlib import Path
import tempfile
import threading
import os
import time
import unittest
from unittest.mock import patch

import app.observability.logic_trace as trace
import app.storage.database as database
from app.services.search import ProgressiveSearch, SearchBudgetExceeded
from test_database_integration import TempDatabaseTestCase
from unittest.mock import AsyncMock
from types import SimpleNamespace
import app.telegram.application as bot
import app.telegram.delivery as bot_delivery
import app.telegram.state as bot_state
from telegram.error import Forbidden
from scripts.read_trace import read_events, summary


class TraceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'logic_trace.jsonl'

    async def asyncTearDown(self):
        await trace.shutdown_trace()
        self.directory.cleanup()

    def enable(self, **kwargs):
        trace.configure_trace(enabled=True, path=self.path, **kwargs)

    def events(self):
        return [json.loads(line) for line in self.path.read_text(encoding='utf-8').splitlines()]

    async def test_disabled_does_not_create_file_or_evaluate_fields(self):
        trace.configure_trace(enabled=False, path=self.path)
        @trace.traced_flow('search')
        async def work():
            trace.trace_event('search.selected', dangerous=object())
            return 42
        self.assertEqual(await work(), 42)
        self.assertFalse(self.path.exists())

    async def test_json_redaction_and_context_propagation(self):
        self.enable(secrets=['123:SECRET_TOKEN', 'SECRET_API_KEY'])
        with trace.flow_context('subscription', user_id=123456, query='tag') as ctx:
            trace.trace_event('rule34.request.failed', message='https://api.telegram.org/bot123:SECRET_TOKEN/sendPhoto?api_key=SECRET_API_KEY', authorization='Bearer private', api_user_id='secret', ids=list(range(1000)))
            trace.trace_error(ValueError('SECRET_API_KEY\n123:SECRET_TOKEN'), stage='test')
        await trace.shutdown_trace()
        raw = self.path.read_text(encoding='utf-8')
        self.assertNotIn('SECRET_API_KEY', raw)
        self.assertNotIn('SECRET_TOKEN', raw)
        self.assertNotIn('Bearer private', raw)
        events = self.events()
        self.assertTrue(all(e['trace_id'] == ctx.trace_id for e in events))
        self.assertTrue(all(e['user_id_hash'].startswith('u_') for e in events))
        self.assertLessEqual(len(events[1]['ids']), 20)

    async def test_minimal_level_and_rotation(self):
        self.enable(level='minimal', max_bytes=500, backup_count=2)
        with trace.flow_context('search'):
            trace.trace_event('search.page.request', level='normal', pid=0)
            for i in range(20):
                trace.trace_event('search.selected', post_id=i)
        await trace.shutdown_trace()
        self.assertTrue(Path(str(self.path) + '.1').exists())
        for path in Path(self.directory.name).glob('logic_trace.jsonl*'):
            events = [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()]
            self.assertTrue(all(e['event'] != 'search.page.request' for e in events))

    async def test_queue_overload_is_nonblocking_and_counts_drops(self):
        self.enable(queue_size=2)
        blocked, release = threading.Event(), threading.Event()
        original = trace.RotatingFileHandler.emit
        def slow_emit(handler, record):
            blocked.set()
            release.wait(timeout=2)
            original(handler, record)
        with patch.object(trace.RotatingFileHandler, 'emit', slow_emit):
            try:
                with trace.flow_context('search'):
                    self.assertTrue(await asyncio.to_thread(blocked.wait, 1))
                    async with asyncio.timeout(.5):
                        for i in range(1000):
                            trace.trace_event('filter.summary', level='normal', received=i)
                        await asyncio.sleep(0)
            finally:
                release.set()
            await trace.shutdown_trace()
        self.assertGreater(trace.dropped_events(), 0)

    async def test_retention_removes_only_expired_numbered_backups(self):
        old = Path(str(self.path) + '.7')
        unrelated = Path(str(self.path) + '.notes')
        for path in (old, unrelated):
            path.write_text('old', encoding='utf-8')
            os.utime(path, (time.time()-10*86400, time.time()-10*86400))
        self.enable(retention_days=7)
        await trace.shutdown_trace()
        self.assertFalse(old.exists())
        self.assertTrue(unrelated.exists())

    async def test_normal_hides_verbose_samples_and_ids_are_bounded(self):
        self.enable(level='normal')
        with trace.flow_context('search', user_id=555):
            trace.trace_event('filter.rejections', level='verbose', rejected_samples=list(range(1000)))
            trace.trace_event('search.page.filtered', level='normal', remaining=1)
        await trace.shutdown_trace()
        self.assertNotIn('filter.rejections', [e['event'] for e in self.events()])

    async def test_cli_filters_rotated_logs_skips_invalid_and_does_not_write(self):
        item = {'ts': '2026-10-05T10:00:00Z', 'trace_id': 'abc', 'flow': 'search', 'user_id_hash': 'u_test', 'event': 'search.finish', 'outcome': 'success', 'duration_ms': 1}
        self.path.write_text(json.dumps(item)+'\nnot json\n', encoding='utf-8')
        before = self.path.read_bytes()
        events, invalid = read_events(self.path, trace='abc', user='u_test', event='search.finish')
        self.assertEqual(events, [item])
        self.assertEqual(invalid, 1)
        self.assertIn('outcome: success', summary(events))
        self.assertEqual(before, self.path.read_bytes())

    async def test_digest_partial_failure_finish_reports_delivery_error(self):
        self.enable()
        @trace.traced_flow('digest')
        async def digest():
            return bot.DigestDeliveryResult(failed_ids=[('tag', 1)])
        await digest()
        await trace.shutdown_trace()
        self.assertEqual(self.events()[-1]['outcome'], 'telegram_error')

    async def test_nested_context_has_parent_and_transport_inherits(self):
        self.enable()
        with trace.flow_context('callback') as parent:
            with trace.flow_context('zip', user_id=1) as child:
                trace.trace_event('telegram.send.success')
        await trace.shutdown_trace()
        event = next(e for e in self.events() if e['event'] == 'telegram.send.success')
        self.assertEqual(event['trace_id'], child.trace_id)
        self.assertEqual(event['parent_trace_id'], parent.trace_id)

    async def test_cancelled_flow_has_finish(self):
        self.enable()
        @trace.traced_flow('search')
        async def work():
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await work()
        await trace.shutdown_trace()
        self.assertEqual(self.events()[-1]['outcome'], 'cancelled')

    async def test_telegram_failure_redacts_token_and_keeps_trace_id(self):
        self.enable(secrets=['123:TEST_SECRET'])
        limiter = bot_delivery.TelegramRateLimiter()
        async def failing():
            raise Forbidden('https://api.telegram.org/bot123:TEST_SECRET/sendPhoto')
        with trace.flow_context('search') as ctx:
            with self.assertRaises(Forbidden):
                await limiter.execute(failing, operation_name='send_photo', chat_id=1)
        await trace.shutdown_trace()
        events = self.events()
        self.assertIn('telegram.send.forbidden', [e['event'] for e in events])
        self.assertTrue(all(e['trace_id'] == ctx.trace_id for e in events))
        self.assertNotIn('TEST_SECRET', self.path.read_text(encoding='utf-8'))


class ProgressTraceTests(TempDatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.path = Path(self.tempdir) / 'trace.jsonl'
        trace.configure_trace(enabled=True, path=self.path, level='verbose', secrets=[])

    async def asyncTearDown(self):
        await trace.shutdown_trace()
        await super().asyncTearDown()

    async def events(self):
        await trace.shutdown_trace()
        return [json.loads(line) for line in self.path.read_text(encoding='utf-8').splitlines()]

    async def test_search_two_pages_explains_counts_advance_selection_and_finish(self):
        pages = {0: [{'id': 1, 'file_url': 'https://example.test/a.jpg', 'rating': 'e'}, {'id': 'bad'}], 1: [{'id': 2, 'file_url': 'https://example.test/b.jpg', 'rating': 's'}]}
        async def search(*args, pid=0, **kwargs):
            return pages.get(pid, [])
        result = await ProgressiveSearch(SimpleNamespace(search=search)).select(1, 'tag', set(), {'rating_filter': 's'})
        self.assertEqual(result['id'], 2)
        events = await self.events()
        filtered = [e for e in events if e['event'] == 'filter.summary']
        self.assertEqual((filtered[0]['received'], filtered[0]['invalid_id'], filtered[0]['rating'], filtered[0]['accepted']), (2, 1, 1, 0))
        self.assertEqual(filtered[1]['accepted'], 1)
        names = [e['event'] for e in events]
        self.assertIn('search.page.advance', names)
        self.assertIn('search.selected', names)
        self.assertEqual(events[-1]['outcome'], 'success')
        self.assertEqual(len({e['trace_id'] for e in events}), 1)

    async def test_budget_is_distinct_from_archive_exhaustion(self):
        async def search(*args, **kwargs):
            return [{'id': 1, 'file_url': 'https://example.test/a.jpg', 'rating': 'e'}]
        with self.assertRaises(SearchBudgetExceeded):
            await ProgressiveSearch(SimpleNamespace(search=search), request_budget=1).select(1, 'tag', set(), {'rating_filter': 's'})
        events = await self.events()
        self.assertIn('search.budget.exhausted', [e['event'] for e in events])
        self.assertNotIn('search.exhausted', [e['event'] for e in events])
        self.assertEqual(events[-1]['outcome'], 'budget_exhausted')

    async def test_subscription_archive_and_fresh_decisions(self):
        await database.add_subscription(1, 'tag', 60)
        async def search(*args, **kwargs):
            return [{'id': 10, 'file_url': 'https://example.test/a.jpg'}]
        service = ProgressiveSearch(SimpleNamespace(search=search))
        first = await service.select(1, 'tag', set(), {}, subscription=True)
        await service.delivered(1, 'tag', first, subscription=True)
        async with database.connect_db() as db:
            await db.execute("UPDATE query_progress SET pid=7,page_json=?,used_json='[]'", (json.dumps([{'id': 2, 'file_url': 'https://example.test/archive.jpg'}]),))
            await db.commit()
        archive = await service.select(1, 'tag', set(), {}, subscription=True)
        self.assertEqual(archive['id'], 2)
        service.api.search = AsyncMock(return_value=[{'id': 11, 'file_url': 'https://example.test/new.jpg'}])
        fresh = await service.select(1, 'tag', set(), {}, subscription=True)
        self.assertEqual(fresh['id'], 11)
        events = await self.events()
        decisions = [e['decision'] for e in events if e['event'] == 'subscription.decision']
        self.assertEqual(decisions, ['archive', 'archive', 'fresh'])

    async def test_claim_disabled_during_wait_is_visible_and_not_sent(self):
        await database.add_subscription(1, 'tag', 60)
        post = {'id': 42, 'file_url': 'https://example.test/a.jpg'}
        telegram_bot = SimpleNamespace(send_photo=AsyncMock())
        async def disable(chat_id):
            async with database.connect_db() as db:
                await db.execute('UPDATE subscriptions SET is_active=0 WHERE user_id=1')
                await db.commit()
            return True
        with (
            patch.object(bot, 'is_recipient_allowed', return_value=True),
            patch.object(bot.search_service, 'select', AsyncMock(return_value=post)),
            patch.object(bot_state, 'DB_PATH', database.DB_PATH),
            patch.object(bot.telegram_rate_limiter, 'wait_for_slot', side_effect=disable),
        ):
            self.assertFalse(await bot.process_one_subscription(SimpleNamespace(bot=telegram_bot), (1, 'tag', 60, 0)))
            await bot_state.flush_callback_payloads()
        telegram_bot.send_photo.assert_not_awaited()
        events = await self.events()
        checks = [e for e in events if e['event'] == 'subscription.before_send.revalidate']
        self.assertTrue(any(e['claim_valid'] is False for e in checks))
        self.assertIn('telegram.send.skipped', [e['event'] for e in events])
