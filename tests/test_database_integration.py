import asyncio
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
        self.old_subscription_cooldown = database.SUBSCRIPTION_CREATE_COOLDOWN_SECONDS
        database.DB_PATH = os.path.join(self.tempdir, "test.db")
        database.SUBSCRIPTION_CREATE_COOLDOWN_SECONDS = 0
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.old_db_path
        database.SEARCH_HISTORY_RETENTION_PER_USER = self.old_history_retention
        database.SENT_POSTS_RETENTION_PER_USER = self.old_sent_retention
        database.SUBSCRIPTION_CREATE_COOLDOWN_SECONDS = self.old_subscription_cooldown
        shutil.rmtree(self.tempdir, ignore_errors=True)


class SubscriptionClaimTests(TempDatabaseTestCase):
    async def test_due_subscriptions_have_deterministic_schedule_user_query_order(self):
        for user_id, query in ((2, "z"), (1, "b"), (1, "a")):
            self.assertTrue(await database.add_subscription(user_id, query, 10))
        async with database.connect_db() as db:
            await db.execute("""
                UPDATE subscriptions SET next_check_at = '2026-01-01 00:00:00'
            """)
            await db.commit()

        due = await database.get_due_subscriptions()

        self.assertEqual(
            [(row[0], row[1]) for row in due],
            [(1, "a"), (1, "b"), (2, "z")],
        )

    async def test_concurrent_claims_have_exactly_one_winner(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))

        claims = await asyncio.gather(*(
            database.claim_due_subscription(1, "tag")
            for _ in range(8)
        ))

        winners = [token for token in claims if token is not None]
        self.assertEqual(len(winners), 1)

    async def test_claim_blocks_second_claim_until_release(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))

        token = await database.claim_due_subscription(1, "tag")
        self.assertIsNotNone(token)
        self.assertIsNone(await database.claim_due_subscription(1, "tag"))

        await database.release_subscription_claim(1, "tag", token)
        self.assertIsNotNone(await database.claim_due_subscription(1, "tag"))

    async def test_release_stale_claims_keeps_active_claim(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))

        token = await database.claim_due_subscription(1, "tag")
        self.assertIsNotNone(token)

        await database.release_stale_subscription_claims()

        self.assertIsNone(await database.claim_due_subscription(1, "tag"))

    async def test_release_stale_claims_releases_expired_claim(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))

        token = await database.claim_due_subscription(1, "tag")
        self.assertIsNotNone(token)
        async with database.connect_db() as db:
            await db.execute("""
                UPDATE subscriptions
                SET processing_until = datetime('now', '-1 minute')
                WHERE user_id = ? AND query = ?
            """, (1, "tag"))
            await db.commit()

        await database.release_stale_subscription_claims()

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

    async def test_transient_failure_backoff_is_token_fenced_and_persisted(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))
        token = await database.claim_due_subscription(1, "tag")

        self.assertFalse(await database.defer_subscription_after_transient_failure(
            1, "tag", "wrong-token", 120
        ))
        self.assertTrue(await database.defer_subscription_after_transient_failure(
            1, "tag", token, 120
        ))
        self.assertNotIn((1, "tag", 10, 0), await database.get_due_subscriptions())
        self.assertIsNone(await database.claim_due_subscription(1, "tag"))

    async def test_claim_revalidation_observes_disable_and_global_pause(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))
        token = await database.claim_due_subscription(1, "tag")
        self.assertTrue(await database.is_subscription_claim_active(1, "tag", token))
        self.assertFalse(await database.is_subscription_claim_active(
            1, "tag", "wrong-token"
        ))

        await database.pause_all_active_subscriptions(1, 60)
        self.assertFalse(await database.is_subscription_claim_active(1, "tag", token))

    async def test_expired_claim_token_cannot_complete_reclaimed_subscription(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))
        expired_token = await database.claim_due_subscription(1, "tag")
        self.assertIsNotNone(expired_token)

        async with database.connect_db() as db:
            await db.execute("""
                UPDATE subscriptions
                SET processing_until = datetime('now', '-1 minute')
                WHERE user_id = ? AND query = ?
            """, (1, "tag"))
            await db.commit()

        replacement_token = await database.claim_due_subscription(1, "tag")
        self.assertIsNotNone(replacement_token)
        self.assertNotEqual(expired_token, replacement_token)
        self.assertFalse(
            await database.update_subscription_time(1, "tag", expired_token)
        )
        self.assertTrue(
            await database.update_subscription_time(1, "tag", replacement_token)
        )

    async def test_wrong_token_does_not_mark_subscription_empty_or_notify(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))
        token = await database.claim_due_subscription(1, "tag")
        self.assertIsNotNone(token)

        empty_count, backoff_minutes, should_notify = (
            await database.mark_subscription_empty(1, "tag", "wrong-token")
        )

        self.assertEqual((empty_count, backoff_minutes, should_notify), (0, 0, False))
        async with database.connect_db() as db:
            cursor = await db.execute("""
                SELECT no_new_posts_count, processing_token
                FROM subscriptions WHERE user_id = ? AND query = ?
            """, (1, "tag"))
            row = await cursor.fetchone()
        self.assertEqual(row, (0, token))

    async def test_removed_and_readded_subscription_rejects_old_claim_token(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))
        old_token = await database.claim_due_subscription(1, "tag")
        self.assertIsNotNone(old_token)
        self.assertTrue(await database.remove_subscription(1, "tag"))
        self.assertTrue(await database.add_subscription(1, "tag", 10))

        self.assertFalse(
            await database.update_subscription_time(1, "tag", old_token)
        )
        new_token = await database.claim_due_subscription(1, "tag")
        self.assertIsNotNone(new_token)
        self.assertNotEqual(old_token, new_token)

    async def test_readding_existing_subscription_preserves_options_and_schedule(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))
        await database.update_subscription_options(1, "tag", {
            "digest_mode": "digest",
            "rating_filter": "s",
        })
        fixed_next_check = "2030-01-02 03:04:05"
        async with database.connect_db() as db:
            await db.execute("""
                UPDATE subscriptions SET next_check_at = ?
                WHERE user_id = ? AND query = ?
            """, (fixed_next_check, 1, "tag"))
            await db.commit()

        self.assertTrue(await database.add_subscription(1, "tag", 30))

        options = await database.get_subscription_options(1, "tag")
        async with database.connect_db() as db:
            cursor = await db.execute("""
                SELECT interval_minutes, is_active, next_check_at
                FROM subscriptions WHERE user_id = ? AND query = ?
            """, (1, "tag"))
            row = await cursor.fetchone()
        self.assertEqual(options["digest_mode"], "digest")
        self.assertEqual(options["rating_filter"], "s")
        self.assertEqual(row, (30, 1, fixed_next_check))

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


class DigestClaimTests(TempDatabaseTestCase):
    async def _enqueue_same_post_for_two_queries(self):
        post = {"id": 42, "file_url": "https://example.test/42.jpg"}
        for query in ("tag-b", "tag-a"):
            self.assertTrue(await database.add_subscription(1, query, 10))
            self.assertTrue(await database.enqueue_subscription_digest(1, query, post))
        async with database.connect_db() as db:
            await db.execute("""
                UPDATE subscription_digest_queue
                SET queued_at = '2026-01-01 00:00:00'
                WHERE user_id = ?
            """, (1,))
            await db.commit()

    async def test_claim_is_atomic_and_preserves_stable_query_post_identity(self):
        await self._enqueue_same_post_for_two_queries()

        claims = await asyncio.gather(
            database.claim_subscription_digest(1, 10),
            database.claim_subscription_digest(1, 10),
        )

        non_empty = [(token, posts) for token, posts in claims if token]
        self.assertEqual(len(non_empty), 1)
        _token, posts = non_empty[0]
        self.assertEqual(
            [post["digest_item_key"] for post in posts],
            [("tag-a", 42), ("tag-b", 42)],
        )

    async def test_partial_finish_deletes_confirmed_item_and_releases_remainder(self):
        await self._enqueue_same_post_for_two_queries()
        token, posts = await database.claim_subscription_digest(1, 10)

        delivered, released = await database.finish_subscription_digest_claim(
            1, token, [posts[0]["digest_item_key"]]
        )

        self.assertEqual((delivered, released), (1, 1))
        next_token, remaining = await database.claim_subscription_digest(1, 10)
        self.assertIsNotNone(next_token)
        expected_remaining = [
            post["digest_item_key"] for post in posts[1:]
        ]
        self.assertEqual(
            [post["digest_item_key"] for post in remaining],
            expected_remaining,
        )

    async def test_finishing_three_of_ten_releases_other_seven_in_stable_order(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))
        for post_id in range(1, 11):
            self.assertTrue(await database.enqueue_subscription_digest(
                1,
                "tag",
                {"id": post_id, "file_url": f"https://example.test/{post_id}.jpg"},
            ))
        token, posts = await database.claim_subscription_digest(1, 10)
        delivered_keys = [post["digest_item_key"] for post in posts[:3]]

        self.assertEqual(
            await database.finish_subscription_digest_claim(
                1, token, delivered_keys
            ),
            (3, 7),
        )
        _next_token, remaining = await database.claim_subscription_digest(1, 10)
        self.assertEqual(
            [post["digest_item_key"] for post in remaining],
            [("tag", post_id) for post_id in range(4, 11)],
        )

    async def test_wrong_claim_token_cannot_delete_or_release_items(self):
        await self._enqueue_same_post_for_two_queries()
        token, _posts = await database.claim_subscription_digest(1, 10)

        self.assertEqual(
            await database.finish_subscription_digest_claim(
                1, "wrong-token", [("tag-a", 42)]
            ),
            (0, 0),
        )
        self.assertEqual(
            await database.release_subscription_digest_claim(1, "wrong-token"),
            0,
        )
        blocked_token, blocked_posts = await database.claim_subscription_digest(1, 10)
        self.assertIsNone(blocked_token)
        self.assertEqual(blocked_posts, [])
        self.assertEqual(await database.release_subscription_digest_claim(1, token), 2)

    async def test_expired_claim_can_be_reclaimed_and_old_token_cannot_ack(self):
        await self._enqueue_same_post_for_two_queries()
        old_token, _posts = await database.claim_subscription_digest(1, 10)
        async with database.connect_db() as db:
            await db.execute("""
                UPDATE subscription_digest_queue
                SET claim_until = datetime('now', '-1 minute')
                WHERE user_id = ? AND claim_token = ?
            """, (1, old_token))
            await db.commit()

        self.assertFalse(
            await database.renew_subscription_digest_claim(1, old_token)
        )
        new_token, posts = await database.claim_subscription_digest(1, 10)

        self.assertIsNotNone(new_token)
        self.assertNotEqual(old_token, new_token)
        self.assertEqual(
            await database.finish_subscription_digest_claim(
                1, old_token, [posts[0]["digest_item_key"]]
            ),
            (0, 0),
        )
        self.assertEqual(
            await database.release_subscription_digest_claim(1, old_token), 0
        )
        self.assertFalse(
            await database.renew_subscription_digest_claim(1, old_token)
        )
        self.assertEqual(
            await database.get_subscription_digest_claim_keys(1, new_token),
            {("tag-a", 42), ("tag-b", 42)},
        )
        self.assertEqual(await database.count_subscription_digest(1), 2)

    async def test_expired_digest_lease_cannot_be_renewed(self):
        await self._enqueue_same_post_for_two_queries()
        token, _posts = await database.claim_subscription_digest(1, 10)
        async with database.connect_db() as db:
            await db.execute("""
                UPDATE subscription_digest_queue
                SET claim_until = datetime('now', '-1 second')
                WHERE user_id = ? AND claim_token = ?
            """, (1, token))
            await db.commit()

        self.assertFalse(
            await database.renew_subscription_digest_claim(1, token)
        )

    async def test_removing_subscription_removes_only_its_digest_items(self):
        await self._enqueue_same_post_for_two_queries()

        self.assertTrue(await database.remove_subscription(1, "tag-a"))

        token, posts = await database.claim_subscription_digest(1, 10)
        self.assertIsNotNone(token)
        self.assertEqual(
            [post["digest_item_key"] for post in posts],
            [("tag-b", 42)],
        )

    async def test_disabled_or_globally_paused_subscription_cannot_be_claimed(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))
        self.assertTrue(await database.enqueue_subscription_digest(
            1, "tag", {"id": 1, "file_url": "https://example.test/1.jpg"}
        ))

        self.assertFalse((await database.toggle_subscription(1, "tag")).is_active)
        self.assertEqual(await database.claim_subscription_digest(1), (None, []))
        self.assertEqual(await database.get_due_digest_users(), [])

        self.assertTrue((await database.toggle_subscription(1, "tag")).is_active)
        await database.pause_all_active_subscriptions(1, 60)
        self.assertEqual(await database.claim_subscription_digest(1), (None, []))
        self.assertEqual(await database.get_due_digest_users(), [])

        await database.resume_all_active_subscriptions(1)
        token, posts = await database.claim_subscription_digest(1)
        self.assertIsNotNone(token)
        self.assertEqual([post["digest_item_key"] for post in posts], [("tag", 1)])

    async def test_ambiguous_digest_item_is_deferred_before_becoming_retryable(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))
        self.assertTrue(await database.enqueue_subscription_digest(
            1, "tag", {"id": 1, "file_url": "https://example.test/1.jpg"}
        ))
        token, posts = await database.claim_subscription_digest(1)

        self.assertEqual(await database.finish_subscription_digest_claim(
            1,
            token,
            [],
            ambiguous_keys=[posts[0]["digest_item_key"]],
            ambiguous_backoff_seconds=120,
        ), (0, 0))
        self.assertEqual(await database.claim_subscription_digest(1), (None, []))
        async with database.connect_db() as db:
            cursor = await db.execute("""
                SELECT delivery_state FROM subscription_digest_queue
                WHERE user_id = 1 AND query = 'tag' AND post_id = 1
            """)
            self.assertEqual((await cursor.fetchone())[0], "ambiguous")
            await db.execute("""
                UPDATE subscription_digest_queue
                SET retry_after = datetime('now', '-1 second')
            """)
            await db.commit()
        retry_token, retry_posts = await database.claim_subscription_digest(1)
        self.assertIsNotNone(retry_token)
        self.assertEqual([post["digest_item_key"] for post in retry_posts], [("tag", 1)])

    async def test_renew_and_active_keys_are_guarded_by_claim_ownership(self):
        await self._enqueue_same_post_for_two_queries()
        token, _posts = await database.claim_subscription_digest(1, 10)

        self.assertTrue(await database.renew_subscription_digest_claim(1, token))
        self.assertEqual(
            await database.get_subscription_digest_claim_keys(1, token),
            {("tag-a", 42), ("tag-b", 42)},
        )
        self.assertFalse(
            await database.renew_subscription_digest_claim(1, "wrong-token")
        )
        self.assertEqual(
            await database.get_subscription_digest_claim_keys(1, "wrong-token"),
            set(),
        )


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

    async def test_get_favorite_by_index_returns_single_saved_post(self):
        for post_id in range(3):
            self.assertTrue(await database.add_favorite(1, {
                "id": post_id,
                "file_url": f"https://example.test/{post_id}.jpg",
                "tags": "keep" if post_id != 1 else "skip",
            }))

        newest = await database.get_favorite_by_index(1, 0)
        filtered = await database.get_favorite_by_index(1, 1, tag_filter="keep")

        self.assertEqual(newest["id"], 2)
        self.assertEqual(filtered["id"], 0)

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

    async def test_get_subscription_post_by_index_and_count(self):
        for post_id in range(4):
            post = {
                "id": post_id,
                "file_url": f"https://example.test/{post_id}.jpg",
                "tags": "subtag",
            }
            self.assertTrue(await database.add_favorite(1, post))
            self.assertTrue(await database.add_subscription_post(1, "sub", post))

        self.assertEqual(await database.count_subscription_posts(1, "sub"), 4)
        post = await database.get_subscription_post_by_index(1, "sub", 2)

        self.assertEqual(post["id"], 1)

    async def test_get_subscription_queries_for_post_uses_subscription_cache(self):
        self.assertTrue(await database.add_subscription(1, "tag-a", 10))
        self.assertTrue(await database.add_subscription(1, "tag-b", 10))
        self.assertTrue(await database.add_subscription(2, "other-user", 10))

        post = {
            "id": 42,
            "file_url": "https://example.test/42.jpg",
            "tags": "subtag",
        }
        await database.replace_subscription_cache(1, "tag-a", [post])
        await database.replace_subscription_cache(1, "tag-b", [post])
        await database.replace_subscription_cache(2, "other-user", [post])

        queries = await database.get_subscription_queries_for_post(1, 42)

        self.assertEqual(queries, ["tag-a", "tag-b"])


class UserSettingsTests(TempDatabaseTestCase):
    async def test_concurrent_partial_saves_do_not_lose_json_updates(self):
        await asyncio.gather(
            database.save_user_settings(1, {"gallery_size": 25}),
            database.save_user_settings(1, {"quality_mode": "sample"}),
            database.save_user_settings(1, {"spoiler_mode": "all"}),
        )

        settings = await database.get_user_settings(1)
        self.assertEqual(settings["gallery_size"], 25)
        self.assertEqual(settings["quality_mode"], "sample")
        self.assertEqual(settings["spoiler_mode"], "all")

    async def test_partial_save_preserves_existing_settings(self):
        await database.save_user_settings(1, {
            "show_caption": False,
            "show_score": False,
            "show_tags_button": False,
        })
        await database.save_user_settings(1, {"show_rating": False})

        settings = await database.get_user_settings(1)

        self.assertFalse(settings["show_caption"])
        self.assertFalse(settings["show_score"])
        self.assertFalse(settings["show_rating"])
        self.assertFalse(settings["show_tags_button"])

    async def test_new_settings_include_tags_button_default(self):
        settings = await database.get_user_settings(1)

        self.assertTrue(settings["show_tags_button"])


class DeliveryFailureClaimTests(TempDatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for post_id in range(1, 4):
            await database.save_delivery_failure(1, {
                "id": post_id,
                "file_url": f"https://example.test/{post_id}.jpg",
            })

    async def test_concurrent_retry_claims_do_not_overlap(self):
        claims = await asyncio.gather(
            database.claim_delivery_failures(limit=3),
            database.claim_delivery_failures(limit=3),
        )

        non_empty = [(token, rows) for token, rows in claims if token]
        self.assertEqual(len(non_empty), 1)
        self.assertEqual([row["post_id"] for row in non_empty[0][1]], [1, 2, 3])

    async def test_confirmed_delete_requires_matching_live_user_post_claim(self):
        token, rows = await database.claim_delivery_failures(limit=3)
        row = rows[0]

        self.assertFalse(await database.delete_delivery_failure_for_post(
            row["user_id"], row["post_id"], "wrong-token"
        ))
        self.assertTrue(await database.delete_delivery_failure_for_post(
            row["user_id"], row["post_id"], token
        ))
        self.assertEqual(await database.release_delivery_failure_claim(token), 2)

    async def test_independent_confirmed_send_clears_failure_by_unique_key(self):
        self.assertTrue(await database.clear_delivery_failure_for_post(1, 2))
        self.assertFalse(await database.clear_delivery_failure_for_post(1, 2))
        self.assertEqual(
            [row["post_id"] for row in await database.get_delivery_failures()],
            [1, 3],
        )


if __name__ == "__main__":
    unittest.main()
