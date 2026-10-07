"""Causal evidence, real read-only SQLite, and bounded incident lifecycle."""
import asyncio
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import app.observability.logic_trace as trace
from app.observability.errors import error_details
from app.observability.health import RuntimeHealth
from app.observability.diagnostics import read_subscriptions, format_snapshot, local_snapshot
from app.observability.logging_filters import RepeatedDiagnosticFilter
from app.observability.db_diagnostics import Diagnostics
import app.telegram.application as bot
from app.telegram.delivery import TelegramRateLimiter
from telegram.error import RetryAfter, TimedOut, BadRequest
from app.integrations.rule34.client import rule34API, APITemporaryError
from app.integrations.rule34.outage import Rule34Outage, Rule34Unavailable
from test_rule34_outage import Response, Session, Clock
from app.integrations.rule34.rate_limiter import Rule34Limiter


class EvidenceTests(unittest.TestCase):
    def test_wrapped_http_root(self):
        root = APITemporaryError('Rule34 API HTTP 403')
        high = APITemporaryError('Rule34 API request failed')
        high.__cause__ = root
        details = error_details(high)
        self.assertEqual(details['root_http_status'], 403)
        self.assertEqual(details['root_error_message'], str(root))
        self.assertEqual(details['error_message'], str(high))

    def test_chain_cycle_bounded(self):
        one, two = ValueError('one'), RuntimeError('two')
        one.__cause__, two.__cause__ = two, one
        self.assertEqual(error_details(one)['root_error_type'], 'RuntimeError')

    def test_suppressed_context_not_reported_as_cause(self):
        high = ValueError('new')
        high.__context__, high.__suppress_context__ = TimeoutError('old'), True
        self.assertEqual(error_details(high)['root_error_type'], 'ValueError')

    def test_taxonomy(self):
        wrapped_timeout = TimedOut()
        wrapped_timeout.__cause__ = TimeoutError('transport read')
        self.assertEqual(error_details(wrapped_timeout, 'telegram')['root_error_type'], 'TimeoutError')
        self.assertEqual(error_details(wrapped_timeout, 'telegram')['error_category'], 'telegram_timeout_ambiguous')
        for error, category in [(sqlite3.OperationalError('database is locked'),'db_locked'),
            (sqlite3.IntegrityError('constraint'),'db_integrity'), (TimedOut(),'telegram_timeout_ambiguous'),
            (RetryAfter(10),'telegram_retry_after'), (asyncio.CancelledError(),'cancellation')]:
            with self.subTest(category=category):
                self.assertEqual(error_details(error)['error_category'], category)

    def test_aggregation_recovery_new_incident(self):
        clock = Clock()
        health = RuntimeHealth(clock=clock)
        details = error_details(APITemporaryError('HTTP 403'))
        records = [health.error('rule34','search',details) for _ in range(100)]
        self.assertEqual(sum(not r['repeated'] for r in records), 1)
        self.assertEqual(records[-1]['failures'], 100)
        health.error('rule34','search',error_details(APITemporaryError('HTTP 503')))
        self.assertEqual(health.snapshot()['active_incidents'], 2)
        self.assertEqual(len(health.success('rule34')), 2)
        new = health.error('rule34','search',details)
        self.assertNotEqual(new['incident_id'], records[0]['incident_id'])

    def test_same_request_attempt_deduplicated_but_retry_counted(self):
        health = RuntimeHealth()
        details = error_details(TimedOut())
        health.error('telegram','send',details,'req',1)
        self.assertEqual(health.error('telegram','send',details,'req',1)['failures'], 1)
        self.assertEqual(health.error('telegram','send',details,'req',2)['failures'], 2)

    def test_periodic_summary_and_ring_bounded(self):
        clock = Clock()
        health = RuntimeHealth(clock=clock, capacity=3)
        details = error_details(ValueError('one'))
        health.error('app','work',details)
        clock.now = 301
        self.assertTrue(health.error('app','work',details)['summary_due'])
        for i in range(10):
            health.error('app','work',error_details(ValueError(str(i))))
        self.assertEqual(len(health.incidents), 3)
        self.assertEqual(len(health.active), 3)

    def test_log_traceback_throttling_distinct_root(self):
        clock = Clock()
        limiter = RepeatedDiagnosticFilter(clock=clock)
        def record(message):
            return logging.LogRecord('test',logging.ERROR,'',1,'outer',(),(ValueError,ValueError(message),None))
        self.assertTrue(limiter.filter(record('one')))
        self.assertFalse(limiter.filter(record('one')))
        self.assertTrue(limiter.filter(record('two')))
        clock.now = 301
        summary = record('one')
        self.assertTrue(limiter.filter(summary))
        self.assertIsNone(summary.exc_info)
        self.assertIn('count=3', summary.getMessage())

    def test_plain_warning_summary_serializes(self):
        clock = Clock()
        limiter = RepeatedDiagnosticFilter(clock=clock)
        def record():
            return logging.LogRecord('test',logging.WARNING,'',1,'bad %s',('source',),None)
        limiter.filter(record())
        clock.now = 301
        item = record()
        self.assertTrue(limiter.filter(item))
        self.assertIn('bad source',item.getMessage())

    def test_read_only_sqlite_preserves_claim_and_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'snapshot.db'
            with sqlite3.connect(path) as connection:
                connection.execute('CREATE TABLE subscriptions (is_active, next_check_at, last_sent, processing_until, processing_token)')
                connection.executemany('INSERT INTO subscriptions VALUES (?,?,?,?,?)',[
                    (1,'2000-01-01',None,None,None),(1,'2000-01-01',None,'2999-01-01','secret-claim'),
                    (0,None,None,'2000-01-01','expired')])
            connection.close()
            before = path.read_bytes()
            result = read_subscriptions(path)
            self.assertEqual((result['active'],result['due'],result['claims'],result['expired_claims']),(2,1,1,1))
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn('secret-claim',json.dumps(result))

    def test_missing_database_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'missing.db'
            self.assertIn('snapshot_error',read_subscriptions(path))
            self.assertFalse(path.exists())

    def test_read_only_snapshot_under_exclusive_lock_is_bounded(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'locked.db'
            connection = sqlite3.connect(path)
            try:
                connection.execute('CREATE TABLE subscriptions (is_active,next_check_at,last_sent,processing_until)')
                connection.commit()
                connection.execute('BEGIN EXCLUSIVE')
                started = time.monotonic()
                self.assertIn('snapshot_error', read_subscriptions(path, timeout=.02))
                self.assertLess(time.monotonic()-started, 1)
                self.assertTrue(connection.in_transaction)
            finally:
                connection.rollback()
                connection.close()

    def test_timezone_abstraction_and_unknown_health(self):
        snapshot = RuntimeHealth(clock=lambda:0).snapshot()
        rendered = format_snapshot(snapshot, now=60, timezone=UTC)
        self.assertIn('1970-01-01 00:01:00 UTC',rendered)
        self.assertIn('success_age=unknown',rendered)


class CausalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)/'trace.jsonl'
        self.health = RuntimeHealth()
        self.patcher = patch.object(trace,'runtime_health',self.health)
        self.patcher.start()
        trace.configure_trace(enabled=True,path=self.path,level='normal',secrets=['BOTSECRET','APISECRET'])

    async def asyncTearDown(self):
        await trace.shutdown_trace()
        self.patcher.stop()
        self.directory.cleanup()

    async def events(self):
        await trace.shutdown_trace()
        return [json.loads(line) for line in self.path.read_text(encoding='utf-8').splitlines()]

    async def test_nested_flow_request_connection_correlation(self):
        with trace.flow_context('subscription',user_id=123,query='tag') as root:
            with trace.flow_context('media.delivery') as child:
                with trace.request_context('telegram','photo') as request:
                    trace.next_attempt()
                    trace.trace_event('telegram.send.success')
                diagnostic = Diagnostics('ack')
                diagnostic.finished()
        events = await self.events()
        sent = next(e for e in events if e['event']=='telegram.send.success')
        self.assertEqual(sent['trace_id'],root.trace_id)
        self.assertEqual(sent['flow_id'],child.flow_id)
        self.assertNotEqual(root.flow_id,child.flow_id)
        self.assertEqual(sent['request_id'],request['request_id'])
        self.assertEqual(sent['attempt'],1)
        finish = next(e for e in events if e['event']=='subscription.finished')
        self.assertEqual(finish['user_impact'],'delivered')

    async def test_one_traceback_per_repeated_incident(self):
        with trace.flow_context('search'):
            for _ in range(100):
                trace.trace_error(APITemporaryError('HTTP 403'),'rule34')
        events = await self.events()
        errors = [e for e in events if e['event']=='error']
        self.assertEqual(len(errors),100)
        self.assertEqual(sum('traceback' in e for e in errors),1)
        self.assertEqual(errors[-1]['failures'],100)

    async def test_cache_fallback_delivered_does_not_heal_rule34(self):
        with trace.flow_context('subscription'):
            trace.trace_error(APITemporaryError('HTTP 403'),'rule34')
            trace.outcome('api_error')
            trace.trace_event('cache.decision',decision='fallback')
            trace.trace_event('telegram.send.success')
            trace.trace_event('subscription.delivery.success')
        events = await self.events()
        final = next(e for e in events if e['event']=='subscription.finished')
        self.assertEqual(final['outcome'],'fallback_success')
        self.assertEqual(final['user_impact'],'delivered')
        self.assertTrue(self.health.snapshot()['components']['rule34']['degraded'])
        self.assertFalse(any(e['event']=='rule34.recovered' for e in events))

    async def test_deferred_final_outcome(self):
        with trace.flow_context('subscription'):
            trace.outcome('api_error')
            trace.trace_event('subscription.defer.api_error')
            trace.trace_event('subscription.defer.outage',backoff_seconds=600)
        final = next(e for e in await self.events() if e['event']=='subscription.finished')
        self.assertEqual((final['outcome'],final['user_impact']),('deferred','delayed'))
        self.assertEqual(self.health.snapshot()['components']['subscriptions']['deferred'],1)

    async def test_ambiguous_timeout_final_impact(self):
        with trace.flow_context('search'):
            trace.trace_error(TimedOut(),'photo','telegram.send.timeout')
            trace.outcome('telegram_error')
        final = next(e for e in await self.events() if e['event']=='search.finished')
        self.assertEqual(final['user_impact'],'unknown_delivery')

    async def test_secret_event_payload_and_exception_chain(self):
        with trace.flow_context('callback',user_id=91345678):
            trace.trace_event('callback.selected',processing_token='RAWCLAIM',callback_payload='RAWCALLBACK',
                authorization='Bearer RAWAUTH',url='https://host/path?api_key=QUERYSECRET&user_id=APISECRET',
                message='BOTSECRET APISECRET socks5://user:PROXYSECRET@host processing_token=RAWCLAIM callback_data="RAWCALLBACK"')
            trace.trace_error(ValueError('BOTSECRET APISECRET'),'test')
        await self.events()
        raw = self.path.read_text(encoding='utf-8')
        for secret in ['BOTSECRET','APISECRET','RAWCLAIM','RAWCALLBACK','RAWAUTH','QUERYSECRET','PROXYSECRET','91345678']:
            self.assertNotIn(secret,raw)

    async def test_real_http_retries_share_request_and_root_status(self):
        api = rule34API(limiter=Rule34Limiter(),breaker=Rule34Outage(jitter=lambda:0))
        api.session = Session([Response(503),Response(503),Response(503)])
        with patch('app.integrations.rule34.client.asyncio.sleep',new=AsyncMock()):
            with self.assertRaises(APITemporaryError):
                await api.search('tag',set())
        events = await self.events()
        attempts = [e for e in events if e['event']=='rule34.http.attempt']
        self.assertEqual(len({e['request_id'] for e in attempts}),1)
        self.assertEqual([e['attempt'] for e in attempts],[1,2,3])
        self.assertTrue(any(e.get('root_http_status')==503 for e in events))
        self.assertEqual(next(e for e in events if e['event']=='rule34.request.finished')['outcome'],'failed')

    async def test_breaker_suppressed_no_physical_attempt(self):
        api = rule34API(limiter=Rule34Limiter(),breaker=Rule34Outage(jitter=lambda:0))
        api.session = Session([Response(403)])
        for _ in range(2):
            with self.assertRaises(Rule34Unavailable):
                await api.search('tag',set())
        events = await self.events()
        finals = [e for e in events if e['event']=='rule34.request.finished']
        self.assertEqual(finals[-1]['outcome'],'suppressed')
        self.assertEqual(finals[-1]['attempts'],0)
        self.assertEqual(len(api.session.calls),1)

    async def test_rule34_real_success_recovery_once(self):
        with trace.flow_context('search'):
            trace.trace_error(APITemporaryError('HTTP 403'),'rule34')
            trace.trace_event('rule34.request.success',endpoint='post_search',status=200)
            trace.trace_event('rule34.request.success',endpoint='post_search',status=200)
        self.assertEqual(sum(e['event']=='rule34.recovered' for e in await self.events()),1)

    async def test_stale_http_success_does_not_heal_open_breaker(self):
        with trace.flow_context('search'):
            trace.trace_error(APITemporaryError('HTTP 403'),'rule34')
            trace.trace_event('rule34.request.success',endpoint='post_search',status=200,recovery_accepted=False)
        self.assertTrue(self.health.snapshot()['components']['rule34']['degraded'])
        self.assertFalse(any(e['event']=='rule34.recovered' for e in await self.events()))

    async def test_telegram_notification_has_separate_request_from_rule34(self):
        limiter = TelegramRateLimiter()
        with trace.flow_context('subscription'):
            with trace.request_context('rule34','search') as api_request:
                trace.next_attempt()
                await limiter.execute(AsyncMock(return_value=True),operation_name='admin_alert',chat_id=1)
        sent = next(e for e in await self.events() if e['event']=='telegram.send.start')
        self.assertNotEqual(sent['request_id'],api_request['request_id'])
        self.assertEqual(sent['operation'],'admin_alert')

    async def test_error_feedback_is_not_search_result_delivery(self):
        with trace.flow_context('search'):
            trace.outcome('api_error')
            trace.trace_event('telegram.send.success',operation='error_feedback')
        final = next(e for e in await self.events() if e['event']=='search.finished')
        self.assertEqual((final['outcome'],final['user_impact']),('failed','no_delivery'))

    async def test_admin_update_failed_result_is_not_success_and_admin_is_hashed(self):
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1),effective_chat=SimpleNamespace(type='private'),
                                 message=SimpleNamespace(reply_text=AsyncMock()))
        result = SimpleNamespace(status='failed',stage='compile',returncode=1,timed_out=False)
        with patch.object(bot,'ADMIN_USER_IDS',{1}), patch.object(bot,'perform_update',new=AsyncMock(return_value=result)), \
             patch.object(bot,'_update_error_text',return_value='failed'):
            await bot.update_command(update,SimpleNamespace())
        final = next(e for e in await self.events() if e['event']=='admin.update.finished')
        self.assertEqual(final['outcome'],'failed')
        self.assertEqual(final['user_hash'],trace.safe_hash(1,'u_'))
        self.assertEqual(final['error_stage'],'compile')

    async def test_media_source_failure_fallback_final_delivery(self):
        import app.telegram.media as media
        post = dict(id=42,file_url='https://host/original.jpg',sample_url='https://host/sample.jpg')
        with patch.object(media,'get_media_url_candidates',return_value=[('file_url',post['file_url']),('sample_url',post['sample_url'])]), \
             patch.object(media,'reply_media_url',new=AsyncMock(side_effect=[BadRequest('invalid dimensions'),True])):
            with trace.flow_context('search'):
                self.assertTrue(await media.send_post_media(SimpleNamespace(),post,retries=1))
        events = await self.events()
        self.assertTrue(any(e['event']=='media.source.failed' for e in events))
        self.assertTrue(any(e['event']=='media.source.fallback' for e in events))
        final = next(e for e in events if e['event']=='media.delivery.finished')
        self.assertEqual((final['outcome'],final['user_impact']),('fallback_success','delivered'))
        self.assertNotIn('https://host',self.path.read_text(encoding='utf-8'))

    async def test_db_locked_root_and_fast_same_operation_recovery(self):
        diagnostic = Diagnostics('locked.fixture')
        with trace.flow_context('subscription'):
            diagnostic.trace = trace.current_trace()
            with self.assertRaises(sqlite3.OperationalError):
                diagnostic.sync_call('execute',lambda: (_ for _ in ()).throw(sqlite3.OperationalError('database is locked')))
            diagnostic.finished()
            healthy = Diagnostics('locked.fixture')
            healthy.finished()
        events = await self.events()
        self.assertTrue(any(e.get('error_category')=='db_locked' for e in events))
        self.assertTrue(any(e['event']=='db.recovered' for e in events))

    async def test_telegram_cooldown_recovers_only_after_actual_success(self):
        clock = Clock()
        limiter = TelegramRateLimiter(clock=clock)
        limiter.apply_retry_after(1,1)
        clock.now = 100
        operation = AsyncMock(return_value=True)
        await limiter.execute(operation,operation_name='photo',chat_id=1)
        await limiter.execute(operation,operation_name='photo',chat_id=1)
        events = await self.events()
        self.assertEqual(sum(e['event']=='telegram.cooldown.recovered' for e in events),1)
        self.assertEqual(operation.await_count,2)

    async def test_writer_malformed_event_does_not_kill_writer(self):
        writer = trace._writer
        writer.accept({'bad':object()})
        with trace.flow_context('callback'):
            trace.trace_event('callback.ok')
        await self.events()
        self.assertEqual(writer.malformed,1)
        self.assertTrue(writer.flush_ok)

    async def test_writer_snapshot_after_shutdown_preserves_flush_without_duplicate_keys(self):
        await trace.shutdown_trace()
        state = trace.writer_health()
        self.assertFalse(state['trace_writer_ok'])
        self.assertFalse(state['enabled'])
        self.assertTrue(state['flush_ok'])

    async def test_writer_emit_oserror_visible(self):
        writer = trace._writer
        await asyncio.sleep(.05)
        with patch('logging.handlers.RotatingFileHandler.shouldRollover',side_effect=OSError('secret-path')):
            with trace.flow_context('callback'):
                trace.trace_event('callback.ok')
            await asyncio.to_thread(writer.queue.join)
        self.assertGreater(writer.errors,0)
        self.assertEqual(writer.last_write_error,'OSError')
        self.assertFalse(writer.snapshot()['trace_writer_ok'])

    async def test_disabled_trace_still_observes_health_without_file(self):
        await trace.shutdown_trace()
        trace.configure_trace(enabled=False,path=self.path)
        with trace.flow_context('search'):
            trace.trace_error(TimedOut(),'send','telegram.send.timeout')
        self.assertTrue(self.health.snapshot()['components']['telegram']['degraded'])
        # The enabled writer from setUp may have created its empty file.
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")


class AdminTests(unittest.IsolatedAsyncioTestCase):
    def update(self,user=1,chat_type='private'):
        return SimpleNamespace(effective_user=SimpleNamespace(id=user),
            effective_chat=SimpleNamespace(type=chat_type),message=SimpleNamespace(reply_text=AsyncMock()))

    async def test_diag_unauthorized_never_reads_database(self):
        update = self.update(2)
        with patch.object(bot,'ADMIN_USER_IDS',{1}), patch.object(bot,'subscription_snapshot',new=AsyncMock()) as read:
            await bot.diag_command(update,SimpleNamespace(args=[]))
        read.assert_not_awaited()
        update.message.reply_text.assert_awaited_once()

    async def test_diag_rejects_group_admin(self):
        with patch.object(bot,'ADMIN_USER_IDS',{1}), patch.object(bot,'subscription_snapshot',new=AsyncMock()) as read:
            await bot.diag_command(self.update(chat_type='group'),SimpleNamespace(args=[]))
        read.assert_not_awaited()

    async def test_diag_admin_no_http_or_write_helpers(self):
        update = self.update()
        with patch.object(bot,'ADMIN_USER_IDS',{1}), patch.object(bot,'subscription_snapshot',new=AsyncMock(return_value={})) as read, \
             patch.object(bot.api,'search',new=AsyncMock(side_effect=AssertionError('HTTP probe'))) as search, \
             patch.object(bot,'init_db',new=AsyncMock(side_effect=AssertionError('DB write'))) as init:
            await bot.diag_command(update,SimpleNamespace(args=['errors']))
        read.assert_awaited_once()
        search.assert_not_awaited()
        init.assert_not_awaited()
        text = update.message.reply_text.await_args.args[0]
        self.assertIn('Telegram',text)
        self.assertIn('Rule34',text)
        self.assertLessEqual(len(text),3900)

    async def test_start_ready_after_super_start_and_running_updater(self):
        application = object.__new__(bot.SearchApplication)
        application.updater = SimpleNamespace(running=True)
        order = []
        async def start(_self):
            order.append('started')
        def stage(name):
            order.append(name)
        with patch.object(bot.Application,'start',start), patch.object(bot,'diagnostic_stage',stage):
            await bot.SearchApplication.start(application)
        self.assertEqual(order,['started','polling_ready'])

    async def test_failed_start_never_ready(self):
        with patch.object(bot.Application,'start',new=AsyncMock(side_effect=RuntimeError('bootstrap'))), \
             patch.object(bot,'diagnostic_stage') as stage:
            with self.assertRaises(RuntimeError):
                application = object.__new__(bot.SearchApplication)
                application.updater = SimpleNamespace(running=True)
                await bot.SearchApplication.start(application)
        stage.assert_not_called()
