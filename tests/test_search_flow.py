import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bot


class SearchFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_background_task_uses_application_create_task(self):
        async def noop():
            return None

        coroutine = noop()
        application = SimpleNamespace(create_task=MagicMock())
        context = SimpleNamespace(application=application)

        bot.schedule_background_task(context, coroutine)

        application.create_task.assert_called_once_with(coroutine)
        coroutine.close()

    def test_parse_pause_minutes_accepts_common_units(self):
        self.assertEqual(bot.parse_pause_minutes("30"), 30)
        self.assertEqual(bot.parse_pause_minutes("2ч"), 120)
        self.assertEqual(bot.parse_pause_minutes("1д"), 1440)
        self.assertEqual(bot.parse_pause_minutes("1день"), 1440)
        self.assertEqual(bot.parse_pause_minutes("999д"), 10080)

    def test_format_remaining_pause_shows_time_left(self):
        pause_until = (
            datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=2, minutes=5)
        ).strftime(bot.SQLITE_TIMESTAMP_FORMAT)

        self.assertIn("2 ч.", bot.format_remaining_pause(pause_until))

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

    async def test_random_search_uses_empty_tags_and_does_not_save_query(self):
        message = AsyncMock()
        status_message = AsyncMock()
        message.reply_text = AsyncMock(return_value=status_message)
        result = {"id": "123", "file_url": "https://example.test/123.jpg"}

        with (
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value={"blocked"})),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={"show_caption": False})),
            patch.object(bot, "is_rate_limited", return_value=False),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value={456})),
            patch.object(bot.api, "get_global_random_image", AsyncMock(return_value=result)) as get_global_random_image,
            patch.object(bot, "save_user_query", AsyncMock()) as save_user_query,
            patch.object(bot, "remember_and_cache_post", AsyncMock()),
            patch.object(bot, "mark_post_sent", AsyncMock()) as mark_sent,
            patch.object(bot, "send_post_media", AsyncMock(return_value=True)),
        ):
            delivered = await bot.send_random_image(message, 1)

        self.assertTrue(delivered)
        get_global_random_image.assert_awaited_once_with({"blocked"}, {456})
        save_user_query.assert_not_awaited()
        mark_sent.assert_awaited_once_with(1, 123)

if __name__ == "__main__":
    unittest.main()
