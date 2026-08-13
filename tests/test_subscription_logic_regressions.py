import asyncio
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import NetworkError, RetryAfter

import bot
import database


class SubscriptionCachedBlacklistRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_subscription_skips_posts_matching_user_blacklist(self):
        blocked = {
            "id": 1,
            "file_url": "https://example.test/blocked.jpg",
            "tags": "safe gore portrait",
        }
        allowed = {
            "id": 2,
            "file_url": "https://example.test/allowed.jpg",
            "tags": "safe portrait",
        }

        with (
            patch.object(
                bot,
                "get_subscription_cache",
                AsyncMock(return_value=([blocked, allowed], "now")),
            ),
            patch.object(
                bot, "is_subscription_cache_stale", AsyncMock(return_value=False)
            ),
            patch.object(
                bot.api, "search_subscription_cache", AsyncMock(return_value=[])
            ) as search,
            patch.object(bot.random, "choice", side_effect=lambda posts: posts[0]),
        ):
            selected = await bot.get_subscription_cached_image(
                1, "portrait", {"gore"}, set()
            )

        self.assertEqual(selected["id"], 2)
        # A small cache may be refreshed, but a blocked cached post must never be
        # returned when that refresh yields no replacement.
        search.assert_awaited_once()

    async def test_refreshed_cache_is_rechecked_against_user_blacklist(self):
        fresh_posts = [
            {
                "id": 10,
                "file_url": "https://example.test/blocked.jpg",
                "tags": "gore",
            },
            {
                "id": 11,
                "file_url": "https://example.test/allowed.jpg",
                "tags": "landscape",
            },
        ]

        with (
            patch.object(
                bot,
                "get_subscription_cache",
                AsyncMock(side_effect=[([], None), (fresh_posts, "now")]),
            ),
            patch.object(
                bot, "is_subscription_cache_stale", AsyncMock(return_value=True)
            ),
            patch.object(
                bot.api,
                "search_subscription_cache",
                AsyncMock(return_value=fresh_posts),
            ),
            patch.object(
                bot,
                "replace_subscription_cache",
                AsyncMock(return_value={"api": 2, "new": 2, "total": 2}),
            ),
            patch.object(bot.random, "choice", side_effect=lambda posts: posts[0]),
        ):
            selected = await bot.get_subscription_cached_image(
                1, "landscape", {"gore"}, set()
            )

        self.assertEqual(selected["id"], 11)


class DigestAlbumFailureRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.telegram_rate_limiter.reset()

    def tearDown(self):
        bot.telegram_rate_limiter.reset()

    @staticmethod
    def _posts():
        return [
            {"id": 1, "file_url": "https://example.test/1.jpg"},
            {"id": 2, "file_url": "https://example.test/2.png"},
        ]

    async def test_scheduled_album_network_error_has_no_sequential_fallback(self):
        telegram_bot = AsyncMock()
        telegram_bot.send_media_group = AsyncMock(
            side_effect=NetworkError("connection lost after request")
        )

        with (
            patch.object(bot, "get_user_settings", AsyncMock(return_value={})),
            patch.object(
                bot.telegram_rate_limiter,
                "wait_for_slot",
                AsyncMock(return_value=True),
            ),
            patch.object(bot, "send_post_media_to_chat", AsyncMock()) as send_one,
        ):
            result = await bot.send_digest_to_chat(
                telegram_bot, 1, self._posts()
            )

        telegram_bot.send_media_group.assert_awaited_once()
        send_one.assert_not_awaited()
        self.assertEqual(result.delivered_ids, [])
        self.assertEqual(result.failed_ids, [])
        self.assertEqual(result.ambiguous_ids, [("digest", 1), ("digest", 2)])

    async def test_scheduled_album_retry_after_has_no_sequential_fallback(self):
        telegram_bot = AsyncMock()
        telegram_bot.send_media_group = AsyncMock(side_effect=RetryAfter(60))

        with (
            patch.object(bot, "get_user_settings", AsyncMock(return_value={})),
            patch.object(
                bot.telegram_rate_limiter,
                "wait_for_slot",
                AsyncMock(return_value=True),
            ),
            patch.object(bot, "send_post_media_to_chat", AsyncMock()) as send_one,
        ):
            result = await bot.send_digest_to_chat(
                telegram_bot, 1, self._posts()
            )

        # The Telegram limiter may retry RetryAfter at the same album boundary.
        self.assertGreaterEqual(telegram_bot.send_media_group.await_count, 1)
        send_one.assert_not_awaited()
        self.assertEqual(result.delivered_ids, [])
        self.assertEqual(result.failed_ids, [("digest", 1), ("digest", 2)])
        self.assertEqual(result.ambiguous_ids, [])


class SubscriptionDatabaseRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="subscription_logic_regression_")
        self.old_db_path = database.DB_PATH
        self.old_cooldown = database.SUBSCRIPTION_CREATE_COOLDOWN_SECONDS
        database.DB_PATH = os.path.join(self.tempdir, "test.db")
        database.SUBSCRIPTION_CREATE_COOLDOWN_SECONDS = 0
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.old_db_path
        database.SUBSCRIPTION_CREATE_COOLDOWN_SECONDS = self.old_cooldown
        shutil.rmtree(self.tempdir, ignore_errors=True)

    async def _run_instant_delivery_with_state_change(self, change_state):
        self.assertTrue(await database.add_subscription(1, "tag", 10))
        selected = asyncio.Event()
        state_changed = asyncio.Event()
        post = {
            "id": 42,
            "file_url": "https://example.test/42.jpg",
            "tags": "safe",
        }

        async def select_post(*_args, **_kwargs):
            selected.set()
            await state_changed.wait()
            return post

        async def change_after_selection():
            await selected.wait()
            await change_state()
            state_changed.set()

        send = AsyncMock(return_value=True)
        with (
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=set())),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={})),
            patch.object(bot, "get_subscription_options", AsyncMock(return_value={})),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(bot, "get_subscription_cached_image", side_effect=select_post),
            patch.object(bot, "remember_and_cache_post", AsyncMock()),
            patch.object(bot, "send_post_media_to_chat", send),
        ):
            delivered, _changed = await asyncio.gather(
                bot.process_one_subscription(
                    SimpleNamespace(bot=object()), (1, "tag", 10, 0)
                ),
                change_after_selection(),
            )

        self.assertFalse(delivered)
        send.assert_not_awaited()

    async def test_pause_after_selection_fences_instant_delivery(self):
        await self._run_instant_delivery_with_state_change(
            lambda: database.pause_all_active_subscriptions(1, 60)
        )

    async def test_deactivate_after_selection_fences_instant_delivery(self):
        async def deactivate():
            result = await database.toggle_subscription(1, "tag")
            self.assertEqual(result.status, "deactivated")

        await self._run_instant_delivery_with_state_change(deactivate)

    async def test_successful_subscription_delivery_clears_stale_failure(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))
        post = {
            "id": 42,
            "file_url": "https://example.test/42.jpg",
            "tags": "safe",
        }
        await database.save_delivery_failure(1, post, "old caption", "timeout")

        with (
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=set())),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={})),
            patch.object(bot, "get_subscription_options", AsyncMock(return_value={})),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(
                bot, "get_subscription_cached_image", AsyncMock(return_value=post)
            ),
            patch.object(bot, "remember_and_cache_post", AsyncMock()),
            patch.object(
                bot, "send_post_media_to_chat", AsyncMock(return_value=True)
            ),
        ):
            delivered = await bot.process_one_subscription(
                SimpleNamespace(bot=object()), (1, "tag", 10, 0)
            )

        self.assertTrue(delivered)
        self.assertEqual(await database.get_delivery_failures(), [])

    async def test_retry_failed_concurrent_commands_send_claimed_failure_once(self):
        post = {"id": 9, "file_url": "https://example.test/9.jpg"}
        await database.save_delivery_failure(1, post, "caption", "timeout")
        first_send_started = asyncio.Event()
        duplicate_send_started = asyncio.Event()
        release_send = asyncio.Event()
        send_count = 0

        async def send_once(*_args, **_kwargs):
            nonlocal send_count
            send_count += 1
            if send_count > 1:
                duplicate_send_started.set()
            first_send_started.set()
            await release_send.wait()
            return True

        def make_call():
            message = SimpleNamespace(reply_text=AsyncMock())
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=99), message=message
            )
            context = SimpleNamespace(bot=object())
            return update, context

        first_update, first_context = make_call()
        second_update, second_context = make_call()
        with (
            patch.object(bot, "ADMIN_USER_IDS", {99}),
            patch.object(bot, "send_post_media_to_chat", side_effect=send_once) as send,
        ):
            first = asyncio.create_task(
                bot.retry_failed_command(first_update, first_context)
            )
            await first_send_started.wait()
            second = asyncio.create_task(
                bot.retry_failed_command(second_update, second_context)
            )
            duplicate_waiter = asyncio.create_task(duplicate_send_started.wait())
            await asyncio.wait(
                {second, duplicate_waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            release_send.set()
            await asyncio.gather(first, second)
            duplicate_waiter.cancel()
            await asyncio.gather(duplicate_waiter, return_exceptions=True)

        send.assert_awaited_once()
        self.assertEqual(await database.get_delivery_failures(), [])

    async def test_ambiguous_digest_item_remains_quarantined(self):
        self.assertTrue(await database.add_subscription(1, "tag", 10))
        post = {"id": 7, "file_url": "https://example.test/7.jpg"}
        self.assertTrue(await database.enqueue_subscription_digest(1, "tag", post))
        token, claimed = await database.claim_subscription_digest(1, 10)
        self.assertIsNotNone(token)
        key = claimed[0]["digest_item_key"]

        await database.finish_subscription_digest_claim(
            1,
            token,
            [],
            ambiguous_keys=[key],
            ambiguous_backoff_seconds=300,
        )

        self.assertEqual(await database.claim_subscription_digest(1, 10), (None, []))
        self.assertEqual(await database.count_subscription_digest(1), 1)
        async with database.connect_db() as db:
            cursor = await db.execute(
                """
                SELECT delivery_state, retry_after, claim_token
                FROM subscription_digest_queue
                WHERE user_id = ? AND query = ? AND post_id = ?
                """,
                (1, "tag", 7),
            )
            row = await cursor.fetchone()
        self.assertEqual(row[0], "ambiguous")
        self.assertIsNotNone(row[1])
        self.assertIsNone(row[2])


if __name__ == "__main__":
    unittest.main()
