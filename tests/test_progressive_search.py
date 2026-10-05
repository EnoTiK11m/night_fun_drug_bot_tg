import unittest
import asyncio
from unittest.mock import patch
import app.storage.database as database
from test_database_integration import TempDatabaseTestCase
from app.services.search import ProgressiveSearch, SearchBudgetExceeded, SearchAdmission, SearchBusy


class Pages:
    def __init__(self):
        self.calls = []
        self.new = False

    async def search(self, tags, blacklist, limit=250, pid=0, **kwargs):
        self.calls.append(pid)
        posts = [{'id': 10000 - pid * 250 - i, 'file_url': 'https://example.test/a.jpg', 'rating': 's', 'tags': 'tag'} for i in range(250) if 10000 - pid * 250 - i > 0]
        if pid == 0 and self.new:
            posts.insert(0, {'id': 10001, 'file_url': 'https://example.test/new.jpg', 'rating': 's'})
        return posts[:250]


class ProgressiveTests(TempDatabaseTestCase):
    async def test_shutdown_cancels_active_and_queued_searches(self):
        admission = SearchAdmission(concurrency=1)
        entered = asyncio.Event()
        async def search(user):
            async with admission.hold(user):
                entered.set()
                await asyncio.Event().wait()
        tasks = [asyncio.create_task(search(user)) for user in (1, 2)]
        await entered.wait()
        async with asyncio.timeout(1):
            await admission.stop()
        self.assertTrue(all(task.cancelled() for task in tasks))
        self.assertEqual(admission.users, set())
        self.assertEqual(admission.tasks, set())
    async def test_one_search_per_user_and_bounded_admission(self):
        admission = SearchAdmission(capacity=1)
        async with admission.hold(1):
            for user in (1, 2):
                with self.assertRaises(SearchBusy):
                    async with admission.hold(user):
                        self.fail('Rejected search entered')
        async with admission.hold(2):
            self.assertEqual(admission.users, {2})
        self.assertEqual(admission.users, set())

    async def test_search_beyond_five_pages_and_filters_whole_page(self):
        api = Pages()
        service = ProgressiveSearch(api)
        result = await service.select(1, 'tag', set(), {'rating_filter': 's'}, excluded=set(range(8001, 10001)))
        self.assertLessEqual(int(result['id']), 8000)
        self.assertIn(8, api.calls)

    async def test_ten_thousand_subscription_archive_progresses_past_first_thousand(self):
        await database.add_subscription(1, 'tag', 60)
        async with database.connect_db() as db:
            await db.executemany('INSERT INTO subscription_delivery_history(user_id,query,post_id) VALUES (1,?,?)', [('tag', i) for i in range(8751, 10001)])
            await db.commit()
        api = Pages()
        service = ProgressiveSearch(api)
        with self.assertRaises(SearchBudgetExceeded):
            await service.select(1, 'tag', set(), {}, subscription=True)
        api.calls.clear()
        result = await ProgressiveSearch(api).select(1, 'tag', set(), {}, subscription=True)
        self.assertLessEqual(result['id'], 8750)
        self.assertIn(5, api.calls)
        self.assertLessEqual(len(api.calls), 4)

    async def test_filters_select_only_ten_matching_posts_from_full_page(self):
        api = Pages()
        async def mixed(*args, **kwargs):
            return [{'id': i, 'file_url': 'https://example.test/a.jpg', 'rating': 's' if i <= 10 else 'e'} for i in range(1, 101)]
        api.search = mixed
        result = await ProgressiveSearch(api).select(1, 'tag', set(), {'rating_filter': 's'})
        self.assertIn(result['id'], range(1, 11))

    async def test_subscription_restart_archive_and_new_priority(self):
        await database.add_subscription(1, 'tag', 60)
        api = Pages()
        service = ProgressiveSearch(api)
        first = await service.select(1, 'tag', set(), {}, subscription=True)
        await service.delivered(1, 'tag', first, subscription=True)
        async with database.connect_db() as db:
            await db.execute("UPDATE query_progress SET pid=7, page_json='[]', used_json='[]' WHERE kind='subscription'")
            await db.commit()
        service = ProgressiveSearch(api)
        archive = await service.select(1, 'tag', set(), {}, subscription=True)
        self.assertIn(7, api.calls)
        self.assertLessEqual(archive['id'], 8250)
        await service.delivered(1, 'tag', archive, subscription=True)
        api.new = True
        service = ProgressiveSearch(api)
        fresh = await service.select(1, 'tag', set(), {}, subscription=True)
        self.assertEqual(fresh['id'], 10001)

    async def test_subscription_progress_resets_but_user_delivery_survives_delete(self):
        api = Pages()
        service = ProgressiveSearch(api)
        for query in ('a', 'b'):
            await database.add_subscription(1, query, 60)
        with patch('app.services.search.random.choice', side_effect=lambda posts: posts[0]):
            first = await service.select(1, 'a', set(), {}, subscription=True)
            await service.delivered(1, 'a', first, subscription=True)
            second = await service.select(1, 'a', set(), {}, subscription=True)
            other = await service.select(1, 'b', set(), {}, subscription=True)
            self.assertNotEqual(first['id'], second['id'])
            self.assertNotEqual(first['id'], other['id'])
            await database.remove_subscription(1, 'a')
            await database.add_subscription(1, 'a', 60)
            reset = await service.select(1, 'a', set(), {}, subscription=True)
            self.assertNotEqual(first['id'], reset['id'])

    async def test_budget_exhaustion_preserves_cursor_and_is_not_empty(self):
        service = ProgressiveSearch(Pages(), request_budget=2)
        with self.assertRaises(SearchBudgetExceeded):
            await service.select(1, 'tag', set(), {'rating_filter': 'e'})
        async with database.connect_db() as db:
            row = await (await db.execute("SELECT pid FROM query_progress WHERE user_id=1")).fetchone()
            self.assertEqual(row[0], 2)

    async def test_full_archive_exhaustion_returns_empty(self):
        api = Pages()
        async def empty(*args, **kwargs):
            return []
        api.search = empty
        await database.add_subscription(1, 'tag', 60)
        self.assertIsNone(await ProgressiveSearch(api).select(1, 'tag', set(), {}, subscription=True))
