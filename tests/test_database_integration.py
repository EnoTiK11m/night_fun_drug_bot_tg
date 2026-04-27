import os
import shutil
import unittest
import uuid

import database


class TempDatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = f"test_db_{uuid.uuid4().hex}"
        os.makedirs(self.tempdir)
        self.old_db_path = database.DB_PATH
        self.old_history_retention = database.SEARCH_HISTORY_RETENTION_PER_USER
        self.old_sent_retention = database.SENT_POSTS_RETENTION_PER_USER
        database.DB_PATH = os.path.join(self.tempdir, "test.db")
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.old_db_path
        database.SEARCH_HISTORY_RETENTION_PER_USER = self.old_history_retention
        database.SENT_POSTS_RETENTION_PER_USER = self.old_sent_retention
        shutil.rmtree(self.tempdir, ignore_errors=True)


class SubscriptionClaimTests(TempDatabaseTestCase):
    async def test_claim_blocks_second_claim_until_release(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))

        token = await database.claim_due_subscription(1, "tag")
        self.assertIsNotNone(token)
        self.assertIsNone(await database.claim_due_subscription(1, "tag"))

        await database.release_subscription_claim(1, "tag", token)
        self.assertIsNotNone(await database.claim_due_subscription(1, "tag"))

    async def test_update_subscription_time_requires_matching_token(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))
        token = await database.claim_due_subscription(1, "tag")
        self.assertIsNotNone(token)

        self.assertFalse(
            await database.update_subscription_time(1, "tag", "wrong-token")
        )
        self.assertIsNone(await database.claim_due_subscription(1, "tag"))

        self.assertTrue(await database.update_subscription_time(1, "tag", token))
        self.assertIsNone(await database.claim_due_subscription(1, "tag"))

    async def test_pause_all_active_subscriptions_defers_due_work(self):
        self.assertTrue(await database.add_subscription(1, "tag-a", 10))
        self.assertTrue(await database.add_subscription(1, "tag-b", 10))
        self.assertTrue(await database.add_subscription(2, "other-user", 10))

        paused = await database.pause_all_active_subscriptions(1, 60)

        self.assertEqual(paused, 2)
        due = await database.get_due_subscriptions()
        self.assertNotIn((1, "tag-a", 10, 0), due)
        self.assertNotIn((1, "tag-b", 10, 0), due)
        self.assertIn((2, "other-user", 10, 0), due)

    async def test_new_subscription_inherits_active_pause_until_resume(self):
        paused = await database.pause_all_active_subscriptions(1, 60)
        self.assertEqual(paused, 0)
        self.assertIsNotNone(await database.get_subscription_pause_until(1))

        self.assertTrue(await database.add_subscription(1, "paused-new", 10))
        due = await database.get_due_subscriptions()
        self.assertNotIn((1, "paused-new", 10, 0), due)

        resumed = await database.resume_all_active_subscriptions(1)
        self.assertEqual(resumed, 1)
        self.assertIsNone(await database.get_subscription_pause_until(1))
        due = await database.get_due_subscriptions()
        self.assertIn((1, "paused-new", 10, 0), due)


class RetentionTests(TempDatabaseTestCase):
    async def test_search_history_retention_is_per_user(self):
        database.SEARCH_HISTORY_RETENTION_PER_USER = 3

        for index in range(5):
            await database.save_user_query(1, f"user1-{index}")
        await database.save_user_query(2, "user2-keep")

        user1_history = await database.get_search_history(1, limit=10)
        user2_history = await database.get_search_history(2, limit=10)

        self.assertEqual(user1_history, ["user1-4", "user1-3", "user1-2"])
        self.assertEqual(user2_history, ["user2-keep"])

    async def test_sent_posts_retention_is_per_user(self):
        database.SENT_POSTS_RETENTION_PER_USER = 3

        for post_id in range(5):
            await database.mark_post_sent(1, post_id)
        await database.mark_post_sent(2, 100)

        self.assertEqual(await database.get_sent_post_ids(1), {2, 3, 4})
        self.assertEqual(await database.get_sent_post_ids(2), {100})


class SubscriptionCacheTests(TempDatabaseTestCase):
    async def test_replace_subscription_cache_merges_deduplicates_and_filters_invalid_posts(self):
        saved = await database.replace_subscription_cache(1, "tag", [
            {
                "id": "10",
                "file_url": "https://example.test/10.jpg",
                "sample_url": "https://example.test/10-sample.jpg",
                "preview_url": "https://example.test/10-preview.jpg",
                "tags": "a",
                "rating": "s",
                "score": 5,
            },
            {"id": "10", "file_url": "https://example.test/10-dup.jpg"},
            {"id": "11", "file_url": ""},
            {"id": "bad", "file_url": "https://example.test/bad.jpg"},
            {"id": "12", "file_url": "https://example.test/12.jpg"},
        ])
        merged = await database.replace_subscription_cache(1, "tag", [
            {"id": "12", "file_url": "https://example.test/12-new.jpg"},
            {"id": "13", "file_url": "https://example.test/13.jpg"},
        ])

        posts, cached_at = await database.get_subscription_cache(1, "tag")

        self.assertEqual(saved, {"api": 2, "new": 2, "total": 2})
        self.assertEqual(merged, {"api": 2, "new": 1, "total": 3})
        self.assertIsNotNone(cached_at)
        self.assertEqual({post["id"] for post in posts}, {10, 12, 13})
        post_10 = next(post for post in posts if post["id"] == 10)
        self.assertEqual(post_10["sample_url"], "https://example.test/10-sample.jpg")
        self.assertEqual(post_10["preview_url"], "https://example.test/10-preview.jpg")

    async def test_subscription_cache_stale_when_empty_or_old(self):
        self.assertTrue(await database.is_subscription_cache_stale(1, "tag"))


class PostCacheTests(TempDatabaseTestCase):
    async def test_cache_post_and_favorite_preserve_fallback_urls(self):
        post = {
            "id": "55",
            "file_url": "https://example.test/original.jpg",
            "sample_url": "https://example.test/sample.jpg",
            "preview_url": "https://example.test/preview.jpg",
            "tags": "tag",
            "rating": "s",
            "score": 7,
        }

        self.assertTrue(await database.cache_post(post))
        self.assertTrue(await database.add_favorite(1, {"id": "55"}))

        cached = await database.get_cached_post(55)
        favorite = await database.get_favorite(1, 55)

        self.assertEqual(cached["sample_url"], "https://example.test/sample.jpg")
        self.assertEqual(favorite["preview_url"], "https://example.test/preview.jpg")
        self.assertEqual(favorite["tags"], "tag")

        await database.replace_subscription_cache(1, "tag", [
            {"id": "10", "file_url": "https://example.test/10.jpg"},
        ])
        self.assertFalse(await database.is_subscription_cache_stale(1, "tag"))

        async with database.connect_db() as db:
            await db.execute("""
                UPDATE subscription_cache
                SET cached_at = datetime('now', '-2 hours')
                WHERE user_id = ? AND query = ?
            """, (1, "tag"))
            await db.commit()

        self.assertTrue(await database.is_subscription_cache_stale(1, "tag"))

    async def test_get_favorites_without_limit_returns_all_saved_posts(self):
        for post_id in range(12):
            self.assertTrue(await database.add_favorite(1, {
                "id": post_id,
                "file_url": f"https://example.test/{post_id}.jpg",
                "tags": "keep",
            }))

        favorites = await database.get_favorites(1, limit=None)
        tagged = await database.get_favorites(1, limit=None, tag_filter="keep")

        self.assertEqual(len(favorites), 12)
        self.assertEqual(len(tagged), 12)

    async def test_get_subscription_posts_without_limit_returns_all_saved_posts(self):
        for post_id in range(55):
            post = {
                "id": post_id,
                "file_url": f"https://example.test/{post_id}.jpg",
                "tags": "subtag",
            }
            self.assertTrue(await database.add_favorite(1, post))
            self.assertTrue(await database.add_subscription_post(1, "sub", post))

        posts = await database.get_subscription_posts(1, "sub")

        self.assertEqual(len(posts), 55)


if __name__ == "__main__":
    unittest.main()
