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


if __name__ == "__main__":
    unittest.main()
