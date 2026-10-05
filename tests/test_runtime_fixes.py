"""Real SQLite regressions for cross-query delivery history and full-page random."""
import asyncio
import json
from contextlib import ExitStack
from telegram.error import TimedOut
import app.telegram.application as bot
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import tempfile
import unittest
import app.storage.database as database
import app.services.subscriptions as worker
import app.observability.logic_trace as trace
from app.services.search import ProgressiveSearch
from app.integrations.rule34.client import GLOBAL_RANDOM_MAX_ATTEMPTS
from test_api_pagination import FakeRule34API
from test_database_integration import TempDatabaseTestCase


def post(pid, **fields):
    return dict(id=pid, file_url='https://example.test/a.jpg', tags='', rating='s',
                width=1000, height=1500, **fields)


class SubscriptionUserHistoryTests(TempDatabaseTestCase):
    async def service(self, user, query, posts):
        await database.add_subscription(user, query, 60)
        api = SimpleNamespace(search=AsyncMock(side_effect=lambda *a, **k: posts if k['pid'] == 0 else []))
        return ProgressiveSearch(api)

    async def history(self):
        async with database.connect_db() as db:
            return await (await db.execute('SELECT user_id,post_id FROM subscription_user_delivery_history ORDER BY user_id,post_id')).fetchall()

    async def test_two_queries_choose_other_candidate(self):
        a = await self.service(1, 'a', [post(123)])
        selected = await a.select(1, 'a', set(), {}, subscription=True)
        await a.delivered(1, 'a', selected, subscription=True)
        b = await self.service(1, 'b', [post(123), post(456)])
        self.assertEqual((await b.select(1, 'b', set(), {}, subscription=True))['id'], 456)

    async def test_four_overlapping_queries_deliver_shared_post_once(self):
        received = []
        for query in ('sex', 'Patreon', 'genshin_impact', 'hu_tao_(genshin_impact)'):
            service = await self.service(1, query, [post(123)])
            chosen = await service.select(1, query, set(), {}, subscription=True)
            if chosen:
                received.append(chosen['id'])
                await service.delivered(1, query, chosen, subscription=True)
        self.assertEqual(received, [123])

    async def test_other_users_are_independent(self):
        for user in (1, 2):
            service = await self.service(user, 'a', [post(123)])
            chosen = await service.select(user, 'a', set(), {}, subscription=True)
            self.assertEqual(chosen['id'], 123)
            await service.delivered(user, 'a', chosen, subscription=True)
        self.assertEqual(await self.history(), [(1, 123), (2, 123)])

    async def test_restart_and_delete_preserve_global_history(self):
        service = await self.service(1, 'a', [post(123)])
        await service.delivered(1, 'a', post(123), subscription=True)
        await database.remove_subscription(1, 'a')
        await database.init_db()
        service = await self.service(1, 'b', [post(123), post(456)])
        self.assertEqual((await service.select(1, 'b', set(), {}, subscription=True))['id'], 456)
        self.assertEqual(await self.history(), [(1, 123)])

    async def test_selection_without_delivery_is_not_globally_seen(self):
        a = await self.service(1, 'a', [post(123)])
        b = await self.service(1, 'b', [post(123)])
        await a.select(1, 'a', set(), {}, subscription=True)
        self.assertEqual((await b.select(1, 'b', set(), {}, subscription=True))['id'], 123)
        self.assertEqual(await self.history(), [])

    async def test_digest_enqueue_does_not_acknowledge_telegram_delivery(self):
        service = await self.service(1, 'a', [post(123)])
        await service.select(1, 'a', set(), {}, subscription=True)
        await service.delivered(1, 'a', post(123), subscription=True, user_delivery=False)
        self.assertEqual(await self.history(), [])
        async with database.connect_db() as db:
            row = await (await db.execute('SELECT post_id FROM subscription_delivery_history')).fetchone()
        self.assertEqual(row, (123,))

    async def test_concurrent_acknowledgements_are_idempotent(self):
        a = await self.service(1, 'a', [post(123)])
        b = await self.service(1, 'b', [post(123)])
        await asyncio.gather(a.delivered(1, 'a', post(123), subscription=True), b.delivered(1, 'b', post(123), subscription=True))
        self.assertEqual(await self.history(), [(1, 123)])

    async def test_retention_keeps_recent_and_is_user_scoped(self):
        service = await self.service(1, 'a', [])
        async with database.connect_db() as db:
            await db.execute("INSERT INTO subscription_user_delivery_history VALUES (1,1,'2000-01-01')")
            await db.execute("INSERT INTO subscription_user_delivery_history VALUES (1,2,'2001-01-01')")
            await db.execute("INSERT INTO subscription_user_delivery_history VALUES (2,1,'2000-01-01')")
            await db.commit()
        with patch.object(database, 'SUBSCRIPTION_USER_HISTORY_RETENTION_PER_USER', 2):
            await service.delivered(1, 'a', post(3), subscription=True)
        self.assertEqual(await self.history(), [(1, 2), (1, 3), (2, 1)])

    async def test_migration_seven_to_eight_is_repeatable_without_backfill(self):
        async with database.connect_db() as db:
            await db.execute('DROP TABLE subscription_user_delivery_history')
            await db.execute('DELETE FROM schema_migrations WHERE version=8')
            await db.commit()
        await database.init_db()
        await database.init_db()
        async with database.connect_db() as db:
            versions = await (await db.execute('SELECT version FROM schema_migrations ORDER BY version')).fetchall()
            index = await (await db.execute('PRAGMA index_info(idx_subscription_user_history_sent)')).fetchall()
        self.assertEqual(versions, [(i,) for i in range(1, 9)])
        self.assertEqual([r[2] for r in index], ['user_id', 'sent_at'])
        self.assertEqual(await self.history(), [])

    async def test_retention_ties_keep_latest_delivery_not_highest_post_id(self):
        service = await self.service(1, 'a', [])
        async with database.connect_db() as db:
            await db.execute("INSERT INTO subscription_user_delivery_history VALUES (1,999,datetime('now', '+1 day'))")
            await db.execute("INSERT INTO subscription_user_delivery_history VALUES (1,100,datetime('now', '+1 day'))")
            await db.execute("UPDATE subscription_user_delivery_history SET sent_at=CURRENT_TIMESTAMP")
            await db.commit()
        with patch.object(database, 'SUBSCRIPTION_USER_HISTORY_RETENTION_PER_USER', 2):
            await service.delivered(1, 'a', post(1), subscription=True)
        self.assertEqual(await self.history(), [(1, 1), (1, 100)])

    async def test_recorded_migration_rejects_broken_schema(self):
        async with database.connect_db() as db:
            await db.execute('DROP INDEX idx_subscription_user_history_sent')
            await db.commit()
        with self.assertRaisesRegex(RuntimeError, 'Migration 8'):
            await database.init_db()

    async def test_storage_cleanup_is_scoped_to_user_and_age(self):
        async with database.connect_db() as db:
            await db.execute("INSERT INTO subscription_user_delivery_history VALUES (1,1,'2000-01-01')")
            await db.execute("INSERT INTO subscription_user_delivery_history VALUES (2,1,'2000-01-01')")
            await db.execute('INSERT INTO subscription_user_delivery_history(user_id,post_id) VALUES (1,2)')
            await db.commit()
        await database.cleanup_user_storage(1)
        self.assertEqual(await self.history(), [(1, 2), (2, 1)])


    async def run_delivery(self, send):
        service = await self.service(1, 'a', [post(123)])
        chosen = await service.select(1, 'a', set(), {}, subscription=True)
        with ExitStack() as stack:
            for name, value in {
                'claim_due_subscription': 'token', 'get_user_blacklist': set(),
                'get_user_settings': {'show_caption': False}, 'get_subscription_options': {},
                'get_subscription_cached_image': chosen, 'remember_and_cache_post': None,
                'is_subscription_claim_active': True, 'release_subscription_claim': None,
                'update_subscription_time': True,
            }.items():
                stack.enter_context(patch.object(bot, name, AsyncMock(return_value=value)))
            stack.enter_context(patch.object(bot, 'is_recipient_allowed', return_value=True))
            stack.enter_context(patch.object(bot, 'search_service', service))
            stack.enter_context(patch.object(bot, 'send_post_media_to_chat', send))
            defer = stack.enter_context(patch.object(bot, 'defer_subscription_after_transient_failure', AsyncMock(return_value=True)))
            result = await worker.process_one_subscription(bot, SimpleNamespace(bot=object()), (1, 'a', 60, 0))
        return result, defer

    async def test_telegram_failure_does_not_record_global_success(self):
        result, _ = await self.run_delivery(AsyncMock(return_value=False))
        self.assertFalse(result)
        self.assertEqual(await self.history(), [])
        failures = await database.get_delivery_failures()
        self.assertEqual(int(failures[0]['post_id']), 123)

    async def test_ambiguous_timeout_keeps_existing_defer_without_success(self):
        result, defer = await self.run_delivery(AsyncMock(side_effect=TimedOut('ambiguous')))
        self.assertFalse(result)
        self.assertEqual(await self.history(), [])
        defer.assert_awaited_once_with(1, 'a', 'token', backoff_seconds=1800)

    async def test_history_trace_distinguishes_local_and_cross_query(self):
        a = await self.service(1, 'a', [post(123)])
        await a.delivered(1, 'a', post(123), subscription=True)
        b = await self.service(1, 'b', [post(123), post(456)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace.jsonl'
            try:
                trace.configure_trace(enabled=True, path=path, level='normal')
                await b.select(1, 'b', set(), {}, subscription=True)
            finally:
                await trace.shutdown_trace()
            events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        counts = [e for e in events if e['event'] == 'subscription.dedup.filtered']
        self.assertTrue(any(e['subscription_global_dedup'] == 1 and e['subscription_query_dedup'] == 0 for e in counts))


class RandomPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_matching_among_fifty_uses_first_response(self):
        posts = [dict(post(i), rating='e') for i in range(1, 50)] + [post(50)]
        api = FakeRule34API({17: posts})
        with patch('app.integrations.rule34.client.random.randint', return_value=17):
            selected = await api.get_global_random_image(set(), settings={'rating_filter': 's'})
        self.assertEqual(selected['id'], 50)
        self.assertEqual(api.requested_pids, [17])

    async def test_empty_filtered_page_moves_to_next(self):
        api = FakeRule34API({1: [dict(post(1), rating='e')], 2: [post(2)]})
        with patch('app.integrations.rule34.client.random.randint', side_effect=[1, 2]):
            selected = await api.get_global_random_image(set(), settings={'rating_filter': 's'})
        self.assertEqual(selected['id'], 2)
        self.assertEqual(api.requested_pids, [1, 2])

    async def test_no_candidates_respects_page_request_budget(self):
        api = FakeRule34API({i: [dict(post(i), rating='e')] for i in (1, 2, 3)})
        with patch('app.integrations.rule34.client.random.randint', side_effect=[1, 2, 3]):
            selected = await api.get_global_random_image(set(), settings={'rating_filter': 's'})
        self.assertIsNone(selected)
        self.assertEqual(len(api.requested_pids), GLOBAL_RANDOM_MAX_ATTEMPTS)

    async def test_sent_blacklisted_invalid_and_strict_filters_excluded(self):
        good = post(50)
        bad = [dict(good, id=1), dict(good, id=2, tags='blocked'), dict(good, id=3, rating='e'),
               dict(good, id=4, file_url='https://example.test/a.mp4'), dict(good, id=5, width=1800, height=1000),
               dict(good, id=6, width=20), dict(good, id=0), dict(good, id='bad'), dict(good, id=8, file_url='')]
        api = FakeRule34API({17: bad + [good]})
        with patch('app.integrations.rule34.client.random.randint', return_value=17):
            selected = await api.get_global_random_image({'blocked'}, {1}, settings={
                'rating_filter': 's', 'media_type': 'images', 'orientation': 'portrait', 'min_width': 900})
        self.assertEqual(selected['id'], 50)
        self.assertEqual(api.requested_pids, [17])


class WorkerSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_subscriptions_share_user_lock_and_release_registry(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []
        async def process(runtime, app, subscription):
            calls.append(subscription[1])
            if subscription[1] == 'a':
                entered.set()
                await release.wait()
            return True
        with patch.object(worker, '_process_one_subscription', side_effect=process):
            a = asyncio.create_task(worker.process_one_subscription(None, None, (1, 'a', 30, 0)))
            await entered.wait()
            b = asyncio.create_task(worker.process_one_subscription(None, None, (1, 'b', 30, 0)))
            await asyncio.sleep(0)
            self.assertEqual(calls, ['a'])
            release.set()
            self.assertEqual(await asyncio.gather(a, b), [True, True])
        self.assertEqual(calls, ['a', 'b'])
        self.assertEqual(worker._delivery_locks, {})


class CanonicalTraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_trace_writes_canonical_name_and_preserves_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'logs').mkdir()
            legacy = root / 'logs/app.observability.logic_trace.jsonl'
            legacy.write_text('legacy diagnostic history', encoding='utf-8')
            with patch('app.config.PROJECT_ROOT', root):
                trace.configure_trace(enabled=True)
                with trace.flow_context('search'):
                    trace.trace_event('canonical.test')
                await trace.shutdown_trace()
            self.assertIn('canonical.test', (root / 'logs/logic_trace.jsonl').read_text(encoding='utf-8'))
            self.assertEqual(legacy.read_text(encoding='utf-8'), 'legacy diagnostic history')
