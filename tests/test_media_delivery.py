import logging
import unittest
from unittest.mock import AsyncMock, patch

import bot


class MediaDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_post_media_tries_sample_url_after_file_url_failure(self):
        message = AsyncMock()
        message.reply_photo = AsyncMock(side_effect=[Exception("bad file"), None])
        post = {
            "id": 1,
            "file_url": "https://example.test/original.jpg",
            "sample_url": "https://example.test/sample.jpg",
        }

        with patch.object(bot, "MEDIA_SEND_RETRIES", 1):
            delivered = await bot.send_post_media(message, post, keyboard=object())

        self.assertTrue(delivered)
        self.assertEqual(message.reply_photo.await_count, 2)
        self.assertEqual(
            message.reply_photo.await_args_list[0].args[0],
            "https://example.test/original.jpg",
        )
        self.assertEqual(
            message.reply_photo.await_args_list[1].args[0],
            "https://example.test/sample.jpg",
        )
        message.reply_text.assert_not_awaited()

    async def test_send_post_media_to_chat_tries_preview_url_after_failures(self):
        telegram_bot = AsyncMock()
        telegram_bot.send_photo = AsyncMock(side_effect=[
            Exception("bad file"),
            Exception("bad sample"),
            None,
        ])
        post = {
            "id": 1,
            "file_url": "https://example.test/original.jpg",
            "sample_url": "https://example.test/sample.jpg",
            "preview_url": "https://example.test/preview.jpg",
        }

        with patch.object(bot, "MEDIA_SEND_RETRIES", 1):
            delivered = await bot.send_post_media_to_chat(
                telegram_bot, 123, post, keyboard=object()
            )

        self.assertTrue(delivered)
        self.assertEqual(telegram_bot.send_photo.await_count, 3)
        self.assertEqual(
            telegram_bot.send_photo.await_args_list[2].kwargs["photo"],
            "https://example.test/preview.jpg",
        )
        telegram_bot.send_message.assert_not_awaited()

    async def test_send_post_media_without_any_url_returns_false(self):
        message = AsyncMock()
        post = {"id": 1}

        delivered = await bot.send_post_media(message, post, keyboard=object())

        self.assertFalse(delivered)
        message.reply_text.assert_awaited_once()

    def test_redacting_formatter_masks_bot_token(self):
        formatter = bot.RedactingFormatter("%(message)s")
        record = logging.LogRecord(
            "test",
            logging.INFO,
            __file__,
            1,
            "https://api.telegram.org/botsecret-token/getMe",
            (),
            None,
        )

        with patch.object(bot, "BOT_TOKEN", "secret-token"):
            message = formatter.format(record)

        self.assertEqual(message, "https://api.telegram.org/bot<BOT_TOKEN>/getMe")


if __name__ == "__main__":
    unittest.main()
