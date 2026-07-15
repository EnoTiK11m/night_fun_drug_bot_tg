import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from api_handler import APITemporaryError


class SubscriptionWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_delivery_updates_schedule_then_marks_sent(self):
        app = SimpleNamespace(bot=object())
        result = {"id": "123", "file_url": "https://example.test/file.jpg"}
        calls = []

        async def update_time(*args):
            calls.append("update")
            return True

        async def mark_sent(*args):
            calls.append("mark")

        with (
            patch.object(bot, "claim_due_subscription", AsyncMock(return_value="token")),
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=set())),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={"show_caption": False})),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(bot, "get_subscription_cached_image", AsyncMock(return_value=result)),
            patch.object(bot, "remember_and_cache_post", AsyncMock()),
            patch.object(bot, "send_post_media_to_chat", AsyncMock(return_value=True)),
            patch.object(bot, "update_subscription_time", AsyncMock(side_effect=update_time)),
            patch.object(bot, "mark_post_sent", AsyncMock(side_effect=mark_sent)),
            patch.object(bot, "release_subscription_claim", AsyncMock()) as release_claim,
        ):
            await bot.process_one_subscription(app, (1, "tag", 10, 0))

        self.assertEqual(calls, ["update", "mark"])
        release_claim.assert_not_awaited()

    async def test_failed_delivery_releases_claim_without_marking_sent(self):
        app = SimpleNamespace(bot=object())
        result = {"id": "123", "file_url": "https://example.test/file.jpg"}

        with (
            patch.object(bot, "claim_due_subscription", AsyncMock(return_value="token")),
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=set())),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={"show_caption": False})),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(bot, "get_subscription_cached_image", AsyncMock(return_value=result)),
            patch.object(bot, "remember_and_cache_post", AsyncMock()),
            patch.object(bot, "send_post_media_to_chat", AsyncMock(return_value=False)),
            patch.object(bot, "update_subscription_time", AsyncMock()) as update_time,
            patch.object(bot, "mark_post_sent", AsyncMock()) as mark_sent,
            patch.object(bot, "save_delivery_failure", AsyncMock()) as save_failure,
            patch.object(bot, "release_subscription_claim", AsyncMock()) as release_claim,
        ):
            await bot.process_one_subscription(app, (1, "tag", 10, 0))

        update_time.assert_not_awaited()
        mark_sent.assert_not_awaited()
        save_failure.assert_awaited_once()
        release_claim.assert_awaited_once_with(1, "tag", "token")

    async def test_expired_claim_update_does_not_mark_sent(self):
        app = SimpleNamespace(bot=object())
        result = {"id": "123", "file_url": "https://example.test/file.jpg"}

        with (
            patch.object(bot, "claim_due_subscription", AsyncMock(return_value="token")),
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=set())),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={"show_caption": False})),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(bot, "get_subscription_cached_image", AsyncMock(return_value=result)),
            patch.object(bot, "remember_and_cache_post", AsyncMock()),
            patch.object(bot, "send_post_media_to_chat", AsyncMock(return_value=True)),
            patch.object(bot, "update_subscription_time", AsyncMock(return_value=False)),
            patch.object(bot, "mark_post_sent", AsyncMock()) as mark_sent,
        ):
            await bot.process_one_subscription(app, (1, "tag", 10, 0))

        mark_sent.assert_not_awaited()

    async def test_api_temporary_error_releases_claim_without_empty_backoff(self):
        app = SimpleNamespace(bot=object())

        with (
            patch.object(bot, "claim_due_subscription", AsyncMock(return_value="token")),
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=set())),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={"show_caption": False})),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(
                bot,
                "get_subscription_cached_image",
                AsyncMock(side_effect=APITemporaryError("timeout")),
            ),
            patch.object(bot, "mark_subscription_empty", AsyncMock()) as mark_empty,
            patch.object(bot, "update_subscription_time", AsyncMock()) as update_time,
            patch.object(bot, "mark_post_sent", AsyncMock()) as mark_sent,
            patch.object(bot, "release_subscription_claim", AsyncMock()) as release_claim,
        ):
            await bot.process_one_subscription(app, (1, "tag", 10, 0))

        mark_empty.assert_not_awaited()
        update_time.assert_not_awaited()
        mark_sent.assert_not_awaited()
        release_claim.assert_awaited_once_with(1, "tag", "token")


class SubscriptionSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_due_subscriptions_for_same_user_are_processed_sequentially(self):
        app = SimpleNamespace(bot=object())
        due_subs = [
            (1, "tag-a", 10, 0),
            (1, "tag-b", 10, 0),
            (2, "other-user", 10, 0),
        ]
        real_sleep = asyncio.sleep
        active_by_user = {}
        duplicate_starts = []
        processed = []

        async def process_one(_app, subscription):
            user_id = subscription[0]
            if active_by_user.get(user_id, 0):
                duplicate_starts.append(subscription)
            active_by_user[user_id] = active_by_user.get(user_id, 0) + 1
            await real_sleep(0.01)
            processed.append(subscription)
            active_by_user[user_id] -= 1

        with (
            patch.object(bot, "release_stale_subscription_claims", AsyncMock()),
            patch.object(bot, "get_due_subscriptions", AsyncMock(return_value=due_subs)),
            patch.object(bot, "process_one_subscription", AsyncMock(side_effect=process_one)),
            patch.object(bot.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot.process_subscriptions(app)

        self.assertEqual(duplicate_starts, [])
        self.assertLess(processed.index(due_subs[0]), processed.index(due_subs[1]))


class SubscriptionCacheSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_refreshes_cache_and_returns_available_post(self):
        fresh_posts = [
            {"id": "1", "file_url": "https://example.test/1.jpg"},
            {"id": "2", "file_url": "https://example.test/2.jpg"},
        ]
        cached_after_refresh = [
            {"id": 1, "file_url": "https://example.test/1.jpg"},
            {"id": 2, "file_url": "https://example.test/2.jpg"},
        ]

        with (
            patch.object(bot, "get_subscription_cache", AsyncMock(side_effect=[
                ([], None),
                (cached_after_refresh, "now"),
            ])),
            patch.object(bot, "is_subscription_cache_stale", AsyncMock(return_value=True)),
            patch.object(
                bot.api,
                "search_subscription_cache",
                AsyncMock(return_value=fresh_posts),
            ) as search,
            patch.object(
                bot,
                "replace_subscription_cache",
                AsyncMock(return_value={"api": 2, "new": 2, "total": 2}),
            ) as replace_cache,
            patch.object(bot.random, "choice", return_value=cached_after_refresh[1]),
        ):
            result = await bot.get_subscription_cached_image(1, "tag", set(), {1})

        self.assertEqual(result["id"], 2)
        search.assert_awaited_once()
        replace_cache.assert_awaited_once_with(1, "tag", fresh_posts)

    async def test_uses_stale_cache_when_refresh_times_out(self):
        cached_posts = [
            {"id": 10, "file_url": "https://example.test/10.jpg"},
            {"id": 11, "file_url": "https://example.test/11.jpg"},
        ]

        with (
            patch.object(bot, "get_subscription_cache", AsyncMock(return_value=(cached_posts, "old"))),
            patch.object(bot, "is_subscription_cache_stale", AsyncMock(return_value=True)),
            patch.object(
                bot.api,
                "search_subscription_cache",
                AsyncMock(side_effect=APITemporaryError("timeout")),
            ),
            patch.object(bot, "replace_subscription_cache", AsyncMock()) as replace_cache,
            patch.object(bot.random, "choice", return_value=cached_posts[1]),
        ):
            result = await bot.get_subscription_cached_image(1, "tag", set(), {10})

        self.assertEqual(result["id"], 11)
        replace_cache.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
