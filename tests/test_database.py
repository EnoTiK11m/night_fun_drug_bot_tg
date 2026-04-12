import os
import tempfile
import unittest

import database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = self.tmp.name
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.original_db_path
        os.unlink(self.tmp.name)

    async def test_save_user_query_adds_search_history(self):
        await database.save_user_query(1, "blue eyes")
        await database.save_user_query(1, "green eyes")

        history = await database.get_search_history(1)

        self.assertEqual(history[:2], ["green eyes", "blue eyes"])

    async def test_sent_posts_round_trip(self):
        await database.mark_post_sent(1, 42)
        await database.mark_post_sent(1, 43)
        await database.mark_post_sent(2, 99)

        self.assertEqual(await database.get_sent_post_ids(1), {42, 43})

    async def test_favorites_round_trip(self):
        post = {
            "id": 123,
            "file_url": "https://example.test/post.jpg",
            "tags": "blue_eyes",
            "rating": "s",
            "score": 7,
        }

        self.assertTrue(await database.add_favorite(1, post))
        self.assertFalse(await database.add_favorite(1, post))

        favorites = await database.get_favorites(1)
        self.assertEqual(favorites[0]["id"], 123)

        self.assertTrue(await database.remove_favorite(1, 123))
        self.assertEqual(await database.get_favorites(1), [])

    async def test_update_subscription_interval(self):
        await database.add_subscription(1, "blue eyes", 10)

        self.assertTrue(await database.update_subscription_interval(1, "blue eyes", 45))

        subscriptions = await database.get_all_user_subscriptions(1)
        self.assertEqual(subscriptions[0][1], 45)

    async def test_subscription_posts_returns_only_favorites(self):
        post = {
            "id": 321,
            "file_url": "https://example.test/sub.jpg",
            "tags": "red_eyes",
            "rating": "q",
            "score": 11,
        }

        self.assertTrue(await database.add_subscription_post(1, "red eyes", post))
        self.assertFalse(await database.add_subscription_post(1, "red eyes", post))
        self.assertEqual(await database.get_subscription_posts(1, "red eyes"), [])

        await database.add_favorite(1, post)

        posts = await database.get_subscription_posts(1, "red eyes")
        self.assertEqual(posts[0]["id"], 321)

        self.assertTrue(await database.remove_subscription_post(1, "red eyes", 321))
        self.assertEqual(await database.get_subscription_posts(1, "red eyes"), [])


if __name__ == "__main__":
    unittest.main()
