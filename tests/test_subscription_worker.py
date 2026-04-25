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
            patch.object(bot.api, "get_random_image", AsyncMock(return_value=result)),
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
            patch.object(bot.api, "get_random_image", AsyncMock(return_value=result)),
            patch.object(bot, "send_post_media_to_chat", AsyncMock(return_value=False)),
            patch.object(bot, "update_subscription_time", AsyncMock()) as update_time,
            patch.object(bot, "mark_post_sent", AsyncMock()) as mark_sent,
            patch.object(bot, "release_subscription_claim", AsyncMock()) as release_claim,
        ):
            await bot.process_one_subscription(app, (1, "tag", 10, 0))

        update_time.assert_not_awaited()
        mark_sent.assert_not_awaited()
        release_claim.assert_awaited_once_with(1, "tag", "token")

    async def test_expired_claim_update_does_not_mark_sent(self):
        app = SimpleNamespace(bot=object())
        result = {"id": "123", "file_url": "https://example.test/file.jpg"}

        with (
            patch.object(bot, "claim_due_subscription", AsyncMock(return_value="token")),
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=set())),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={"show_caption": False})),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(bot.api, "get_random_image", AsyncMock(return_value=result)),
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
                bot.api,
                "get_random_image",
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


if __name__ == "__main__":
    unittest.main()
