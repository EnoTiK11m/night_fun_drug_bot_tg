import asyncio
import io
import logging
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import app.telegram.application as bot
import app.telegram.media as bot_media
from app.telegram.delivery import (
    TELEGRAM_MESSAGES_PER_CHAT_MINUTE,
    TelegramRateLimiter,
    telegram_rate_limiter,
)
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut


class CloseCountingBytesIO(io.BytesIO):
    def __init__(self, payload):
        super().__init__(payload)
        self.close_count = 0

    def close(self):
        self.close_count += 1
        super().close()


class NonSeekableUpload:
    def __init__(self, payload):
        self._buffer = io.BytesIO(payload)
        self.closed = False

    def read(self, *args):
        return self._buffer.read(*args)

    def seekable(self):
        return False

    def close(self):
        self.closed = True
        self._buffer.close()


def resolved(host: str, port: int = 443):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return {
        "hostname": "media.example",
        "host": host,
        "port": port,
        "family": family,
        "proto": socket.IPPROTO_TCP,
        "flags": 0,
    }


class MediaDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        telegram_rate_limiter.reset()

    def tearDown(self):
        telegram_rate_limiter.reset()

    def test_default_rate_limit_is_45_messages_per_chat_per_minute(self):
        limiter = TelegramRateLimiter()

        self.assertEqual(TELEGRAM_MESSAGES_PER_CHAT_MINUTE, 45)
        self.assertAlmostEqual(limiter.per_user_seconds, 60 / 45)

    async def _run_explicit_limiter(self, operation, **kwargs):
        limiter = TelegramRateLimiter(
            global_requests_per_second=1000,
            per_chat_requests_per_second=1000,
            burst=10,
            max_retry_after_attempts=1,
        )
        return await limiter.execute(operation, **kwargs)

    async def test_downloaded_upload_rewinds_for_retry_after_and_closes_once(self):
        upload = CloseCountingBytesIO(b"complete-image")
        upload.name = "image.jpg"
        received = []
        message = AsyncMock()

        async def reply_photo(photo, **_kwargs):
            received.append(photo.read())
            if len(received) == 1:
                raise RetryAfter(0)

        message.reply_photo.side_effect = reply_photo
        with patch('app.telegram.media._download_photo_file', AsyncMock(return_value=upload)), patch(
            'app.telegram.media.execute_telegram_request', side_effect=self._run_explicit_limiter
        ):
            self.assertTrue(await bot_media.reply_downloaded_photo(message, "u", "", None))
        self.assertEqual(received, [b"complete-image", b"complete-image"])
        self.assertEqual(upload.close_count, 1)

    async def test_downloaded_upload_timeout_is_not_retried_and_closes(self):
        upload = CloseCountingBytesIO(b"image")
        message = AsyncMock()
        message.reply_photo.side_effect = TimedOut()
        with patch('app.telegram.media._download_photo_file', AsyncMock(return_value=upload)), patch(
            'app.telegram.media.execute_telegram_request', side_effect=self._run_explicit_limiter
        ):
            with self.assertRaises(TimedOut):
                await bot_media.reply_downloaded_photo(message, "u", "", None)
        self.assertEqual(message.reply_photo.await_count, 1)
        self.assertEqual(upload.close_count, 1)

    async def test_downloaded_upload_cancellation_is_not_retried_and_closes(self):
        upload = CloseCountingBytesIO(b"image")
        telegram_bot = AsyncMock()
        telegram_bot.send_photo.side_effect = asyncio.CancelledError()
        with patch('app.telegram.media._download_photo_file', AsyncMock(return_value=upload)), patch(
            'app.telegram.media.execute_telegram_request', side_effect=self._run_explicit_limiter
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot_media.send_downloaded_photo(telegram_bot, 1, "u", "", None)
        self.assertEqual(telegram_bot.send_photo.await_count, 1)
        self.assertEqual(upload.close_count, 1)

    async def test_non_seekable_upload_is_not_retried_after_retry_after(self):
        upload = NonSeekableUpload(b"image")
        message = AsyncMock()

        async def reply_photo(photo, **_kwargs):
            photo.read()
            raise RetryAfter(0)

        message.reply_photo.side_effect = reply_photo
        with patch('app.telegram.media._download_photo_file', AsyncMock(return_value=upload)), patch(
            'app.telegram.media.execute_telegram_request', side_effect=self._run_explicit_limiter
        ):
            with self.assertRaises(RetryAfter):
                await bot_media.reply_downloaded_photo(message, "u", "", None)
        self.assertEqual(message.reply_photo.await_count, 1)
        self.assertTrue(upload.closed)

    def test_downloaded_photo_magic_validation(self):
        self.assertTrue(bot_media._looks_like_supported_photo(b"\xff\xd8\xffrest"))
        self.assertTrue(bot_media._looks_like_supported_photo(b"\x89PNG\r\n\x1a\nrest"))
        self.assertTrue(bot_media._looks_like_supported_photo(b"RIFF1234WEBPrest"))
        self.assertFalse(bot_media._looks_like_supported_photo(b"<html>bad"))

    def test_download_blocks_private_network_addresses(self):
        self.assertFalse(bot_media._is_public_ip("127.0.0.1"))
        self.assertFalse(bot_media._is_public_ip("10.0.0.1"))
        self.assertFalse(bot_media._is_public_ip("169.254.169.254"))
        self.assertTrue(bot_media._is_public_ip("1.1.1.1"))

    async def test_photo_url_validation_rejects_private_hosts_and_credentials(self):
        with self.assertRaises(ValueError):
            await bot_media._validate_public_photo_url("http://127.0.0.1/image.jpg")
        with self.assertRaises(ValueError):
            await bot_media._validate_public_photo_url(
                "https://user:password@1.1.1.1/image.jpg"
            )
        await bot_media._validate_public_photo_url("https://1.1.1.1/image.jpg")

    async def test_public_photo_resolver_accepts_public_dns_results(self):
        delegate = AsyncMock()
        delegate.resolve = AsyncMock(
            return_value=[resolved("1.1.1.1"), resolved("2606:4700:4700::1111")]
        )
        delegate.close = AsyncMock()
        resolver = bot_media.PublicPhotoResolver(delegate)

        result = await resolver.resolve("media.example", 443)

        self.assertEqual([item["host"] for item in result], ["1.1.1.1", "2606:4700:4700::1111"])
        delegate.resolve.assert_awaited_once()

    async def test_public_photo_resolver_rejects_every_non_public_address_class(self):
        blocked = (
            "127.0.0.1", "10.0.0.1", "169.254.169.254", "0.0.0.0",
            "100.64.0.1", "::1", "fc00::1", "fe80::1",
        )
        for address in blocked:
            with self.subTest(address=address):
                delegate = AsyncMock()
                delegate.resolve = AsyncMock(return_value=[resolved(address)])
                delegate.close = AsyncMock()
                with self.assertRaisesRegex(ValueError, "non-public"):
                    await bot_media.PublicPhotoResolver(delegate).resolve(
                        "media.example", 443
                    )

    async def test_public_photo_resolver_rejects_mixed_public_private_answers(self):
        delegate = AsyncMock()
        delegate.resolve = AsyncMock(
            return_value=[resolved("1.1.1.1"), resolved("127.0.0.1")]
        )
        delegate.close = AsyncMock()
        with self.assertRaisesRegex(ValueError, "non-public"):
            await bot_media.PublicPhotoResolver(delegate).resolve("media.example", 443)

    async def test_redirect_to_private_literal_is_rejected_before_second_request(self):
        class RedirectResponse:
            status = 302
            headers = {"Location": "http://127.0.0.1/private.jpg"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class InjectedSession:
            closed = False

            def __init__(self):
                self.calls = []

            def get(self, url, **_kwargs):
                self.calls.append(url)
                return RedirectResponse()

        session = InjectedSession()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "non-public"):
                await bot_media.download_photo_to_path(
                    "https://1.1.1.1/photo.jpg",
                    str(Path(directory) / "download"),
                    session=session,
                )
        self.assertEqual(session.calls, ["https://1.1.1.1/photo.jpg"])

    async def test_download_session_uses_pinned_public_resolver_without_dns_cache(self):
        session = bot_media.create_public_photo_session()
        try:
            self.assertIsInstance(session.connector, bot_media.PublicPhotoConnector)
            self.assertIsInstance(session.connector._resolver, bot_media.PublicPhotoResolver)
            self.assertFalse(session.connector.use_dns_cache)
        finally:
            await session.close()

    async def test_plain_external_client_session_cannot_bypass_safe_connector(self):
        class MarkerSession:
            closed = False

            def get(self, *_args, **_kwargs):
                raise RuntimeError("safe connector selected")

            async def close(self):
                self.closed = True

        external = aiohttp.ClientSession()
        marker = MarkerSession()
        try:
            with tempfile.TemporaryDirectory() as directory, patch.object(
                bot_media, "create_public_photo_session", return_value=marker
            ):
                with self.assertRaisesRegex(RuntimeError, "safe connector selected"):
                    await bot_media.download_photo_to_path(
                        "https://1.1.1.1/photo.jpg",
                        str(Path(directory) / "download"),
                        session=external,
                    )
            self.assertTrue(marker.closed)
        finally:
            await external.close()

    async def test_send_post_media_tries_sample_url_after_file_url_failure(self):
        message = AsyncMock()
        message.reply_photo = AsyncMock(side_effect=[BadRequest("bad file"), None])
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

    async def test_send_post_media_network_error_is_not_retried_or_fallen_back(self):
        message = AsyncMock()
        error = NetworkError("connection lost after request")
        message.reply_photo.side_effect = error
        post = {
            "id": 1,
            "file_url": "https://example.test/original.jpg",
            "sample_url": "https://example.test/sample.jpg",
        }

        with self.assertRaises(NetworkError) as raised:
            await bot_media.send_post_media(message, post, retries=2)

        self.assertIs(raised.exception, error)
        message.reply_photo.assert_awaited_once()
        self.assertEqual(
            message.reply_photo.await_args.args[0],
            "https://example.test/original.jpg",
        )
        message.reply_text.assert_not_awaited()

    async def test_send_post_media_passes_spoiler_to_telegram(self):
        message = AsyncMock()
        post = {"id": 1, "file_url": "https://example.test/1.jpg", "rating": "e"}

        delivered = await bot.send_post_media(
            message, post, settings={"spoiler_mode": "explicit"}
        )

        self.assertTrue(delivered)
        self.assertTrue(message.reply_photo.await_args.kwargs["has_spoiler"])

    async def test_send_post_media_to_chat_tries_preview_url_after_failures(self):
        telegram_bot = AsyncMock()
        telegram_bot.send_photo = AsyncMock(side_effect=[
            BadRequest("bad file"),
            BadRequest("bad sample"),
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

    async def test_send_post_media_to_chat_network_error_is_not_retried_or_fallen_back(self):
        telegram_bot = AsyncMock()
        error = NetworkError("connection lost after request")
        telegram_bot.send_photo.side_effect = error
        post = {
            "id": 1,
            "file_url": "https://example.test/original.jpg",
            "sample_url": "https://example.test/sample.jpg",
        }

        with self.assertRaises(NetworkError) as raised:
            await bot_media.send_post_media_to_chat(
                telegram_bot, 123, post, retries=2
            )

        self.assertIs(raised.exception, error)
        telegram_bot.send_photo.assert_awaited_once()
        self.assertEqual(
            telegram_bot.send_photo.await_args.kwargs["photo"],
            "https://example.test/original.jpg",
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
            side_effect=[BadRequest("Failed to get http url content"), None]
        )
        post = {
            "id": 1,
            "file_url": "https://example.test/original.jpg",
        }
        downloaded = io.BytesIO(b"image-data")
        downloaded.name = "original.jpg"

        with (
            patch.object(bot, "MEDIA_SEND_RETRIES", 1),
            patch('app.telegram.media._download_photo_file', AsyncMock(return_value=downloaded)),
        ):
            delivered = await bot.send_post_media(message, post, keyboard=object())

        self.assertTrue(delivered)
        self.assertEqual(message.reply_photo.await_count, 2)
        self.assertEqual(
            message.reply_photo.await_args_list[0].args[0],
            "https://example.test/original.jpg",
        )
        self.assertIs(message.reply_photo.await_args_list[1].args[0], downloaded)
        self.assertTrue(downloaded.closed)
        message.reply_text.assert_not_awaited()

    async def test_send_post_media_to_chat_downloads_photo_when_telegram_cannot_fetch_url(self):
        telegram_bot = AsyncMock()
        telegram_bot.send_photo = AsyncMock(
            side_effect=[BadRequest("Wrong type of the web page content"), None]
        )
        post = {
            "id": 1,
            "file_url": "https://example.test/original.png?token=abc",
        }
        downloaded = io.BytesIO(b"image-data")
        downloaded.name = "original.png"

        with (
            patch.object(bot, "MEDIA_SEND_RETRIES", 1),
            patch('app.telegram.media._download_photo_file', AsyncMock(return_value=downloaded)),
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
        self.assertTrue(downloaded.closed)
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
