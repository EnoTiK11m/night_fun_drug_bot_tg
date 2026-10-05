import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import app.telegram.application as bot
import app.telegram.delivery as bot_delivery
import app.telegram.media as bot_media
import app.storage.database as database
from telegram.error import Forbidden, NetworkError


class SettingsConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="settings_concurrency_")
        self.old_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self.tempdir.name) / "test.db")
        bot.user_operation_gate.reset_for_tests()
        await database.init_db()

    async def asyncTearDown(self):
        bot.user_operation_gate.reset_for_tests()
        database.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    async def test_independent_concurrent_mutations_keep_both_fields(self):
        await database.save_user_settings(
            1, {"rating_filter": "e", "gallery_size": 10}
        )
        first_read = asyncio.Event()
        release_first = asyncio.Event()
        read_count = 0

        async def controlled_get(user_id):
            nonlocal read_count
            read_count += 1
            if read_count == 1:
                first_read.set()
                await release_first.wait()
            return await database.get_user_settings(user_id)

        get_settings = AsyncMock(side_effect=controlled_get)
        with patch.object(bot, "get_user_settings", get_settings):
            rating = asyncio.create_task(
                bot.mutate_user_settings(1, lambda _current: {"rating_filter": "s"})
            )
            await first_read.wait()
            size = asyncio.create_task(
                bot.mutate_user_settings(1, lambda _current: {"gallery_size": 9})
            )
            await asyncio.sleep(0)
            self.assertEqual(get_settings.await_count, 1)
            release_first.set()
            await asyncio.gather(rating, size)

        settings = await database.get_user_settings(1)
        self.assertEqual(settings["rating_filter"], "s")
        self.assertEqual(settings["gallery_size"], 9)

    async def test_same_field_cycles_are_serialized_and_valid(self):
        values = ["all", "s", "q", "e"]

        def cycle(current):
            value = current["rating_filter"]
            return {"rating_filter": values[(values.index(value) + 1) % len(values)]}

        await asyncio.gather(
            bot.mutate_user_settings(1, cycle),
            bot.mutate_user_settings(1, cycle),
        )

        settings = await database.get_user_settings(1)
        self.assertEqual(settings["rating_filter"], "q")

    async def test_partial_patch_does_not_overwrite_unrelated_settings(self):
        await database.save_user_settings(
            1, {"rating_filter": "e", "gallery_size": 7, "quality_mode": "sample"}
        )
        await database.save_user_settings(1, {"rating_filter": "s"})

        settings = await database.get_user_settings(1)
        self.assertEqual(settings["rating_filter"], "s")
        self.assertEqual(settings["gallery_size"], 7)
        self.assertEqual(settings["quality_mode"], "sample")
        with self.assertRaises(ValueError):
            await database.save_user_settings(1, {"user_supplied_column": True})


class BackgroundAccessPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.user_operation_gate.reset_for_tests()

    def tearDown(self):
        bot.user_operation_gate.reset_for_tests()

    def test_empty_allowlist_keeps_private_delivery_public(self):
        with (
            patch.object(bot, "ADMIN_USER_IDS", set()),
            patch.object(bot, "ALLOWED_USER_IDS", set()),
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
        ):
            self.assertTrue(bot.is_recipient_allowed(1, 1, "private"))

    async def test_revoked_instant_subscription_does_not_send(self):
        send = AsyncMock(return_value=True)
        release = AsyncMock(return_value=True)
        with (
            patch.object(bot, "ADMIN_USER_IDS", set()),
            patch.object(bot, "ALLOWED_USER_IDS", {2}),
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
            patch.object(bot, "claim_due_subscription", AsyncMock(return_value="token")),
            patch.object(bot, "release_subscription_claim", release),
            patch.object(bot, "send_post_media_to_chat", send),
        ):
            delivered = await bot.process_one_subscription(
                SimpleNamespace(bot=object()), (1, "tag", 10, 0)
            )

        self.assertFalse(delivered)
        send.assert_not_awaited()
        release.assert_awaited_once_with(1, "tag", "token")

    async def test_revoked_digest_does_not_send(self):
        telegram_bot = AsyncMock()
        posts = [{"id": 1, "file_url": "https://example.test/1.gif"}]
        with (
            patch.object(bot, "ADMIN_USER_IDS", set()),
            patch.object(bot, "ALLOWED_USER_IDS", {2}),
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
        ):
            result = await bot.send_digest_to_chat(telegram_bot, 1, posts)

        telegram_bot.send_photo.assert_not_awaited()
        telegram_bot.send_media_group.assert_not_awaited()
        self.assertEqual(result.failed_ids, [("digest", 1)])

    async def test_revoked_failed_delivery_retry_does_not_send(self):
        failure = {
            "user_id": 1,
            "post_id": 7,
            "post": {"id": 7, "file_url": "https://example.test/7.jpg"},
            "caption": "caption",
        }
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=99), message=message
        )
        send = AsyncMock(return_value=True)
        with (
            patch.object(bot, "ADMIN_USER_IDS", {99}),
            patch.object(bot, "ALLOWED_USER_IDS", {2}),
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
            patch.object(
                bot,
                "claim_delivery_failures",
                AsyncMock(return_value=("claim", [failure])),
            ),
            patch.object(bot, "release_delivery_failure_claim", AsyncMock()),
            patch.object(bot, "send_post_media_to_chat", send),
        ):
            await bot.retry_failed_command(update, SimpleNamespace(bot=object()))

        send.assert_not_awaited()

    async def test_allowed_instant_subscription_still_sends(self):
        post = {"id": 7, "file_url": "https://example.test/7.jpg", "tags": "safe"}
        send = AsyncMock(return_value=True)
        with (
            patch.object(bot, "ADMIN_USER_IDS", set()),
            patch.object(bot, "ALLOWED_USER_IDS", {1}),
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
            patch.object(bot, "claim_due_subscription", AsyncMock(return_value="token")),
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=set())),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={"show_caption": False})),
            patch.object(bot, "get_subscription_options", AsyncMock(return_value={})),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(bot, "get_subscription_cached_image", AsyncMock(return_value=post)),
            patch.object(bot, "remember_and_cache_post", AsyncMock()),
            patch.object(bot, "is_subscription_claim_active", AsyncMock(return_value=True)),
            patch.object(bot, "send_post_media_to_chat", send),
            patch.object(bot, "update_subscription_time", AsyncMock(return_value=True)),
            patch.object(bot, "mark_post_sent", AsyncMock()),
            patch.object(bot, "clear_delivery_failure_for_post", AsyncMock()),
            patch.object(bot, "release_subscription_claim", AsyncMock()),
        ):
            delivered = await bot.process_one_subscription(
                SimpleNamespace(bot=object()), (1, "tag", 10, 0)
            )

        self.assertTrue(delivered)
        send.assert_awaited_once()

    async def test_access_revoked_during_rate_limit_wait_blocks_request(self):
        telegram_bot = AsyncMock()
        allowed = True

        async def wait_for_slot(_chat_id):
            nonlocal allowed
            allowed = False
            return True

        async def before_send():
            return allowed

        with patch.object(
            bot_delivery.telegram_rate_limiter,
            "wait_for_slot",
            side_effect=wait_for_slot,
        ):
            sent = await bot_media.send_post_media_to_chat(
                telegram_bot,
                1,
                {"id": 1, "file_url": "https://example.test/1.jpg"},
                before_send=before_send,
            )

        self.assertFalse(sent)
        telegram_bot.send_photo.assert_not_awaited()


class DigestPartialProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="digest_partial_")
        self.old_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self.tempdir.name) / "test.db")
        await database.init_db()
        await database.add_subscription(1, "tag-a", cooldown_seconds=0)
        await database.add_subscription(1, "tag-b", cooldown_seconds=0)
        self.posts = [
            {
                "id": 1,
                "file_url": "https://example.test/1.gif",
                "subscription_query": "tag-a",
            },
            {
                "id": 2,
                "file_url": "https://example.test/2.webm",
                "subscription_query": "tag-b",
            },
        ]
        for post in self.posts:
            await database.enqueue_subscription_digest(
                1, post["subscription_query"], post
            )

    async def asyncTearDown(self):
        database.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    async def _claim_and_deliver(self, side_effect):
        token, posts = await database.claim_subscription_digest(1, 10)
        self.assertIsNotNone(token)
        with patch.object(
            bot, "send_post_media_to_chat", AsyncMock(side_effect=side_effect)
        ):
            result = await bot.send_digest_to_chat(object(), 1, posts)
        await database.finish_subscription_digest_claim(
            1,
            token,
            result.delivered_ids,
            ambiguous_keys=result.ambiguous_ids,
        )
        return result

    async def _queued_post_ids(self):
        async with database.connect_db() as db:
            cursor = await db.execute(
                "SELECT post_id FROM subscription_digest_queue ORDER BY post_id"
            )
            return [int(row[0]) for row in await cursor.fetchall()]

    async def test_success_then_forbidden_does_not_requeue_success(self):
        result = await self._claim_and_deliver(
            [True, Forbidden("recipient blocked the bot")]
        )

        self.assertEqual(result.delivered_ids, [("tag-a", 1)])
        self.assertEqual(result.failed_ids, [("tag-b", 2)])
        token, posts = await database.claim_subscription_digest(1, 10)
        self.assertIsNotNone(token)
        self.assertEqual([post["id"] for post in posts], [2])

    async def test_success_then_network_error_keeps_only_ambiguous_item(self):
        result = await self._claim_and_deliver(
            [True, NetworkError("connection lost")]
        )

        self.assertEqual(result.delivered_ids, [("tag-a", 1)])
        self.assertEqual(result.ambiguous_ids, [("tag-b", 2)])
        self.assertEqual(await self._queued_post_ids(), [2])

    async def test_first_item_failure_keeps_entire_batch_retryable(self):
        result = await self._claim_and_deliver(
            [Forbidden("recipient blocked the bot")]
        )

        self.assertEqual(result.delivered_ids, [])
        token, posts = await database.claim_subscription_digest(1, 10)
        self.assertIsNotNone(token)
        self.assertEqual([post["id"] for post in posts], [1, 2])

    async def test_cancellation_after_success_removes_confirmed_item(self):
        token, posts = await database.claim_subscription_digest(1, 10)
        send = AsyncMock(side_effect=[True, asyncio.CancelledError])
        with patch.object(bot, "send_post_media_to_chat", send):
            with self.assertRaises(bot.DigestDeliveryCancelled) as raised:
                await bot.send_digest_to_chat(object(), 1, posts)

        result = raised.exception.result
        await database.finish_subscription_digest_claim(
            1,
            token,
            result.delivered_ids,
            ambiguous_keys=result.ambiguous_ids,
        )
        self.assertEqual(result.delivered_ids, [("tag-a", 1)])
        self.assertEqual(await self._queued_post_ids(), [2])


class FailedDeliveryClaimPreflightTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot_delivery.telegram_rate_limiter.reset()

    def tearDown(self):
        bot_delivery.telegram_rate_limiter.reset()

    @staticmethod
    def failure():
        return {
            "user_id": 1,
            "post_id": 7,
            "post": {"id": 7, "file_url": "https://example.test/7.jpg"},
            "caption": "caption",
        }

    async def _run_retry(self, renew_result, wait_for_slot=None):
        telegram_bot = AsyncMock()
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=99), message=message
        )
        renew = AsyncMock(return_value=renew_result)
        acknowledged = AsyncMock(return_value=True)
        contexts = [
            patch.object(bot, "ADMIN_USER_IDS", {99}),
            patch.object(bot, "ALLOWED_USER_IDS", {1}),
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
            patch.object(
                bot,
                "claim_delivery_failures",
                AsyncMock(return_value=("claim", [self.failure()])),
            ),
            patch.object(bot, "renew_delivery_failure_claim_for_post", renew),
            patch.object(bot, "delete_delivery_failure_for_post", acknowledged),
            patch.object(bot, "release_delivery_failure_claim", AsyncMock()),
        ]
        if wait_for_slot is not None:
            contexts.append(
                patch.object(
                    bot_delivery.telegram_rate_limiter,
                    "wait_for_slot",
                    side_effect=wait_for_slot,
                )
            )

        entered = []
        for context in contexts:
            entered.append(context.__enter__())
        try:
            await bot.retry_failed_command(update, SimpleNamespace(bot=telegram_bot))
        finally:
            for context in reversed(contexts):
                context.__exit__(None, None, None)
        return telegram_bot, renew, acknowledged

    async def test_expired_before_send_makes_no_telegram_call(self):
        telegram_bot, renew, acknowledged = await self._run_retry(False)

        renew.assert_awaited_once_with(1, 7, "claim")
        telegram_bot.send_photo.assert_not_awaited()
        acknowledged.assert_not_awaited()

    async def test_expired_during_limiter_wait_is_rechecked(self):
        limiter_finished = False

        async def wait_for_slot(_chat_id):
            nonlocal limiter_finished
            limiter_finished = True
            return True

        telegram_bot, renew, acknowledged = await self._run_retry(
            False, wait_for_slot
        )

        self.assertTrue(limiter_finished)
        renew.assert_awaited_once()
        telegram_bot.send_photo.assert_not_awaited()
        acknowledged.assert_not_awaited()

    async def test_successful_renewal_sends_once_and_acknowledges(self):
        telegram_bot, renew, acknowledged = await self._run_retry(True)

        renew.assert_awaited_once_with(1, 7, "claim")
        telegram_bot.send_photo.assert_awaited_once()
        acknowledged.assert_awaited_once_with(1, 7, "claim")

    async def test_stolen_ownership_makes_no_telegram_call(self):
        telegram_bot, renew, acknowledged = await self._run_retry(False)

        renew.assert_awaited_once()
        telegram_bot.send_photo.assert_not_awaited()
        acknowledged.assert_not_awaited()


class FailedDeliveryClaimDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="failed_claim_")
        self.old_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self.tempdir.name) / "test.db")
        await database.init_db()
        await database.save_delivery_failure(
            1,
            {"id": 7, "file_url": "https://example.test/7.jpg"},
            "caption",
            "timeout",
        )
        self.token, failures = await database.claim_delivery_failures(limit=1)
        self.assertEqual(len(failures), 1)

    async def asyncTearDown(self):
        database.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    async def test_owned_unexpired_item_can_be_renewed(self):
        self.assertTrue(
            await database.renew_delivery_failure_claim_for_post(
                1, 7, self.token
            )
        )

    async def test_expired_item_cannot_be_renewed(self):
        async with database.connect_db() as db:
            await db.execute(
                "UPDATE delivery_failures SET claim_until = datetime('now', '-1 minute')"
            )
            await db.commit()

        self.assertFalse(
            await database.renew_delivery_failure_claim_for_post(
                1, 7, self.token
            )
        )

    async def test_stolen_item_cannot_be_renewed_by_old_token(self):
        async with database.connect_db() as db:
            await db.execute(
                "UPDATE delivery_failures SET claim_token = ?",
                ("new-owner",),
            )
            await db.commit()

        self.assertFalse(
            await database.renew_delivery_failure_claim_for_post(
                1, 7, self.token
            )
        )


if __name__ == "__main__":
    unittest.main()
