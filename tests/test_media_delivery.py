import io
import logging
import unittest
from unittest.mock import AsyncMock, patch

import bot
from bot_delivery import (
    TELEGRAM_MESSAGES_PER_CHAT_MINUTE,
    TelegramRateLimiter,
    telegram_rate_limiter,
)
from telegram.error import RetryAfter


class MediaDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        telegram_rate_limiter.reset()

    def test_default_rate_limit_is_45_messages_per_chat_per_minute(self):
        limiter = TelegramRateLimiter()

        self.assertEqual(TELEGRAM_MESSAGES_PER_CHAT_MINUTE, 45)
        self.assertAlmostEqual(limiter.per_user_seconds, 60 / 45)

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

    async def test_send_post_media_downloads_photo_when_telegram_cannot_fetch_url(self):
        message = AsyncMock()
        message.reply_photo = AsyncMock(
            side_effect=[Exception("Failed to get http url content"), None]
        )
        post = {
            "id": 1,
            "file_url": "https://example.test/original.jpg",
        }
        downloaded = io.BytesIO(b"image-data")
        downloaded.name = "original.jpg"

        with (
            patch.object(bot, "MEDIA_SEND_RETRIES", 1),
            patch("bot_media._download_photo_file", AsyncMock(return_value=downloaded)),
        ):
            delivered = await bot.send_post_media(message, post, keyboard=object())

        self.assertTrue(delivered)
        self.assertEqual(message.reply_photo.await_count, 2)
        self.assertEqual(
            message.reply_photo.await_args_list[0].args[0],
            "https://example.test/original.jpg",
        )
        self.assertIs(message.reply_photo.await_args_list[1].args[0], downloaded)
        message.reply_text.assert_not_awaited()

    async def test_send_post_media_to_chat_downloads_photo_when_telegram_cannot_fetch_url(self):
        telegram_bot = AsyncMock()
        telegram_bot.send_photo = AsyncMock(
            side_effect=[Exception("Wrong type of the web page content"), None]
        )
        post = {
            "id": 1,
            "file_url": "https://example.test/original.png?token=abc",
        }
        downloaded = io.BytesIO(b"image-data")
        downloaded.name = "original.png"

        with (
            patch.object(bot, "MEDIA_SEND_RETRIES", 1),
            patch("bot_media._download_photo_file", AsyncMock(return_value=downloaded)),
        ):
            delivered = await bot.send_post_media_to_chat(
                telegram_bot, 123, post, keyboard=object()
            )

        self.assertTrue(delivered)
        self.assertEqual(telegram_bot.send_photo.await_count, 2)
        self.assertEqual(
            telegram_bot.send_photo.await_args_list[0].kwargs["photo"],
            "https://example.test/original.png?token=abc",
        )
        self.assertIs(
            telegram_bot.send_photo.await_args_list[1].kwargs["photo"], downloaded
        )
        telegram_bot.send_message.assert_not_awaited()

    async def test_send_post_media_to_chat_detects_media_type_before_query_string(self):
        telegram_bot = AsyncMock()
        post = {
            "id": 1,
            "file_url": "https://example.test/animated.GIF?download=1",
        }

        delivered = await bot.send_post_media_to_chat(
            telegram_bot, 123, post, keyboard=object()
        )

        self.assertTrue(delivered)
        telegram_bot.send_animation.assert_awaited_once()
        self.assertEqual(
            telegram_bot.send_animation.await_args.kwargs["animation"],
            "https://example.test/animated.GIF?download=1",
        )
        telegram_bot.send_photo.assert_not_awaited()

    def test_media_from_post_detects_media_type_before_query_string(self):
        media = bot.media_from_post(
            {"file_url": "https://example.test/video.WEBM?token=abc"}
        )

        self.assertIsInstance(media, bot.InputMediaVideo)

    async def test_retry_after_retries_same_url_without_fallback(self):
        telegram_bot = AsyncMock()
        telegram_bot.send_photo = AsyncMock(side_effect=[RetryAfter(10), None])
        post = {
            "id": 1,
            "file_url": "https://example.test/original.jpg",
            "sample_url": "https://example.test/sample.jpg",
            "preview_url": "https://example.test/preview.jpg",
        }

        with patch.object(
            telegram_rate_limiter,
            "wait_for_slot",
            AsyncMock(return_value=True),
        ):
            delivered = await bot.send_post_media_to_chat(
                telegram_bot, 123, post, keyboard=object()
            )

        self.assertTrue(delivered)
        self.assertEqual(telegram_bot.send_photo.await_count, 2)
        self.assertEqual(
            telegram_bot.send_photo.await_args_list[0].kwargs["photo"],
            "https://example.test/original.jpg",
        )
        self.assertEqual(
            telegram_bot.send_photo.await_args_list[1].kwargs["photo"],
            "https://example.test/original.jpg",
        )
        telegram_bot.send_message.assert_not_awaited()

    async def test_rate_limiter_waits_for_retry_after_cooldown(self):
        limiter = TelegramRateLimiter(per_user_seconds=0, global_per_second=25)

        with (
            patch(
                "bot_delivery.time.monotonic",
                side_effect=[100.0, 100.0, 107.0, 107.0, 107.0],
            ),
            patch("bot_delivery.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            limiter.apply_retry_after(123, RetryAfter(2))
            allowed = await limiter.wait_for_slot(123)

        self.assertTrue(allowed)
        sleep.assert_awaited_once_with(7.0)

    async def test_user_cooldown_does_not_delay_other_users(self):
        limiter = TelegramRateLimiter(per_user_seconds=0, global_per_second=25)

        with (
            patch(
                "bot_delivery.time.monotonic",
                side_effect=[100.0, 100.0, 100.0, 100.0],
            ),
            patch("bot_delivery.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            limiter.apply_retry_after(123, RetryAfter(2))
            allowed = await limiter.wait_for_slot(456)

        self.assertTrue(allowed)
        sleep.assert_not_awaited()

    async def test_new_cooldown_during_pacing_wait_is_honored(self):
        limiter = TelegramRateLimiter(per_user_seconds=0, global_per_second=25)
        limiter._next_global_send = 101.0

        async def apply_cooldown_during_first_sleep(_delay):
            if sleep.await_count == 1:
                limiter.apply_retry_after(123, RetryAfter(2))

        sleep = AsyncMock(side_effect=apply_cooldown_during_first_sleep)
        with (
            patch(
                "bot_delivery.time.monotonic",
                side_effect=[
                    100.0,
                    100.0,
                    100.0,
                    100.0,
                    100.0,
                    107.0,
                    107.0,
                    107.0,
                ],
            ),
            patch("bot_delivery.asyncio.sleep", new=sleep),
        ):
            allowed = await limiter.wait_for_slot(123)

        self.assertTrue(allowed)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1.0, 7.0])

    def test_redacting_formatter_masks_known_secrets(self):
        formatter = bot.RedactingFormatter("%(message)s")
        record = logging.LogRecord(
            "test",
            logging.INFO,
            __file__,
            1,
            "token=secret-token key=secret-key uid=secret-user",
            (),
            None,
        )

        with (
            patch.object(bot, "BOT_TOKEN", "secret-token"),
            patch.object(bot, "API_KEY", "secret-key"),
            patch.object(bot, "API_USER_ID", "secret-user"),
        ):
            message = formatter.format(record)

        self.assertEqual(
            message,
            "token=<BOT_TOKEN> key=<API_KEY> uid=<API_USER_ID>",
        )


if __name__ == "__main__":
    unittest.main()
