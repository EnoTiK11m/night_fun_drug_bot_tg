import unittest
from unittest.mock import AsyncMock, patch

import bot


class SearchFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_regular_search_marks_post_sent(self):
        message = AsyncMock()
        status_message = AsyncMock()
        message.reply_text = AsyncMock(return_value=status_message)
        result = {"id": "123", "file_url": "https://example.test/123.jpg"}

        with (
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=set())),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={"show_caption": False})),
            patch.object(bot, "is_rate_limited", return_value=False),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(bot.api, "get_random_image", AsyncMock(return_value=result)),
            patch.object(bot.api, "save_search_state", AsyncMock()),
            patch.object(bot, "save_user_query", AsyncMock()),
            patch.object(bot, "remember_and_cache_post", AsyncMock()),
            patch.object(bot, "mark_post_sent", AsyncMock()) as mark_sent,
            patch.object(bot, "send_post_media", AsyncMock(return_value=True)),
        ):
            delivered = await bot.send_image(message, 1, "tag")

        self.assertTrue(delivered)
        mark_sent.assert_awaited_once_with(1, 123)

    async def test_regular_search_does_not_mark_post_sent_when_delivery_fails(self):
        message = AsyncMock()
        status_message = AsyncMock()
        message.reply_text = AsyncMock(return_value=status_message)
        result = {"id": "123", "file_url": "https://example.test/123.jpg"}

        with (
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=set())),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={"show_caption": False})),
            patch.object(bot, "is_rate_limited", return_value=False),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(bot.api, "get_random_image", AsyncMock(return_value=result)),
            patch.object(bot.api, "save_search_state", AsyncMock()),
            patch.object(bot, "save_user_query", AsyncMock()),
            patch.object(bot, "remember_and_cache_post", AsyncMock()),
            patch.object(bot, "mark_post_sent", AsyncMock()) as mark_sent,
            patch.object(bot, "send_post_media", AsyncMock(return_value=False)),
        ):
            delivered = await bot.send_image(message, 1, "tag")

        self.assertFalse(delivered)
        mark_sent.assert_not_awaited()

    async def test_regular_search_api_timeout_shows_user_message(self):
        message = AsyncMock()
        status_message = AsyncMock()
        message.reply_text = AsyncMock(return_value=status_message)

        with (
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=set())),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={"show_caption": False})),
            patch.object(bot, "is_rate_limited", return_value=False),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(bot.api, "get_random_image", AsyncMock(side_effect=bot.APITemporaryError("timeout"))),
            patch.object(bot, "mark_post_sent", AsyncMock()) as mark_sent,
        ):
            delivered = await bot.send_image(message, 1, "tag")

        self.assertFalse(delivered)
        status_message.delete.assert_awaited_once()
        mark_sent.assert_not_awaited()
        self.assertIn("Rule34", message.reply_text.await_args_list[-1].args[0])


if __name__ == "__main__":
    unittest.main()
