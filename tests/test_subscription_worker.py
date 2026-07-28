import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from api_handler import APITemporaryError
from bot_delivery import telegram_rate_limiter


class SubscriptionWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        telegram_rate_limiter.reset()
        bot.user_operation_gate.reset_for_tests()

    def tearDown(self):
        telegram_rate_limiter.reset()
        bot.user_operation_gate.reset_for_tests()

    async def test_digest_subscription_queues_without_immediate_send(self):
        app = SimpleNamespace(bot=object())
        result = {"id": "123", "file_url": "https://example.test/file.jpg"}

        with (
            patch.object(bot, "claim_due_subscription", AsyncMock(return_value="token")),
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=set())),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={"show_caption": False})),
            patch.object(bot, "get_subscription_options", AsyncMock(return_value={
                "digest_mode": "digest", "rating_filter": "s"
            })),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(bot, "get_subscription_cached_image", AsyncMock(return_value=result)) as select_post,
            patch.object(bot, "remember_and_cache_post", AsyncMock()),
            patch.object(bot, "enqueue_subscription_digest", AsyncMock(return_value=True)) as enqueue,
            patch.object(bot, "update_subscription_time", AsyncMock(return_value=True)),
            patch.object(bot, "mark_post_sent", AsyncMock()),
            patch.object(bot, "send_post_media_to_chat", AsyncMock()) as send_now,
        ):
            delivered = await bot.process_one_subscription(app, (1, "tag", 10, 0))

        self.assertTrue(delivered)
        enqueue.assert_awaited_once_with(1, "tag", result)
        send_now.assert_not_awaited()
        settings = select_post.await_args.args[4]
        self.assertEqual(settings["rating_filter"], "s")

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

    async def test_cancellation_during_delivery_releases_claim_and_propagates(self):
        app = SimpleNamespace(bot=object())
        result = {"id": "123", "file_url": "https://example.test/file.jpg"}

        with (
            patch.object(bot, "claim_due_subscription", AsyncMock(return_value="token")),
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=set())),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={"show_caption": False})),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(bot, "get_subscription_cached_image", AsyncMock(return_value=result)),
            patch.object(bot, "remember_and_cache_post", AsyncMock()),
            patch.object(
                bot,
                "send_post_media_to_chat",
                AsyncMock(side_effect=asyncio.CancelledError),
            ),
            patch.object(bot, "release_subscription_claim", AsyncMock()) as release_claim,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot.process_one_subscription(app, (1, "tag", 10, 0))

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
            patch.object(bot, "release_subscription_claim", AsyncMock()) as release_claim,
        ):
            await bot.process_one_subscription(app, (1, "tag", 10, 0))

        mark_sent.assert_not_awaited()
        release_claim.assert_awaited_once_with(1, "tag", "token")

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

    async def test_scheduled_digest_with_partial_gif_webm_delivery_is_not_successful(self):
        app_bot = object()
        posts = [
            {"id": 1, "file_url": "https://example.test/one.gif"},
            {"id": 2, "file_url": "https://example.test/two.webm"},
        ]

        with (
            patch.object(bot, "get_user_settings", AsyncMock(return_value={})),
            patch.object(
                bot,
                "send_post_media_to_chat",
                AsyncMock(side_effect=[True, False]),
            ) as send_post,
        ):
            result = await bot.send_digest_to_chat(app_bot, 1, posts)

        self.assertEqual(result.delivered_ids, [("digest", 1)])
        self.assertEqual(result.failed_ids, [("digest", 2)])
        self.assertEqual(result.ambiguous_ids, [])
        self.assertEqual(send_post.await_count, 2)
        self.assertEqual(
            [call.args[2]["id"] for call in send_post.await_args_list],
            [1, 2],
        )

    async def test_scheduled_digest_album_timeout_is_ambiguous_without_resend(self):
        telegram_bot = AsyncMock()
        telegram_bot.send_media_group = AsyncMock(side_effect=bot.TimedOut())
        posts = [
            {"id": 1, "file_url": "https://example.test/one.jpg"},
            {"id": 2, "file_url": "https://example.test/two.png"},
        ]

        with (
            patch.object(bot, "get_user_settings", AsyncMock(return_value={})),
            patch.object(
                bot.telegram_rate_limiter,
                "wait_for_slot",
                AsyncMock(return_value=True),
            ),
            patch.object(bot, "send_post_media_to_chat", AsyncMock()) as send_post,
        ):
            result = await bot.send_digest_to_chat(telegram_bot, 1, posts)

        self.assertEqual(result.delivered_ids, [])
        self.assertEqual(result.failed_ids, [])
        self.assertEqual(
            result.ambiguous_ids,
            [("digest", 1), ("digest", 2)],
        )
        send_post.assert_not_awaited()

    async def test_manual_digest_finishes_only_confirmed_query_identity(self):
        posts = [
            {
                "id": 7,
                "file_url": "https://example.test/7.gif",
                "subscription_query": "tag-a",
            },
            {
                "id": 7,
                "file_url": "https://example.test/7.webm",
                "subscription_query": "tag-b",
            },
        ]
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            data="sub_digest_send",
            answer=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        update = SimpleNamespace(callback_query=query)
        delivery = bot.DigestDeliveryResult(
            delivered_ids=[("tag-a", 7)],
            failed_ids=[("tag-b", 7)],
        )

        with (
            patch.object(
                bot,
                "claim_subscription_digest",
                AsyncMock(return_value=("claim-token", posts)),
            ),
            patch.object(bot, "send_digest_posts", AsyncMock(return_value=delivery)),
            patch.object(
                bot,
                "get_subscription_digest_claim_keys",
                AsyncMock(return_value={("tag-a", 7), ("tag-b", 7)}),
            ),
            patch.object(
                bot, "renew_subscription_digest_claim", AsyncMock(return_value=True)
            ),
            patch.object(
                bot,
                "finish_subscription_digest_claim",
                AsyncMock(return_value=(1, 1)),
            ) as finish,
        ):
            await bot.button_handler(update, SimpleNamespace())

        finish.assert_awaited_once_with(
            1, "claim-token", [("tag-a", 7)]
        )

    async def test_manual_digest_cancellation_finishes_confirmed_items_and_propagates(self):
        posts = [
            {"id": 7, "file_url": "https://example.test/7.gif", "digest_item_key": ("tag-a", 7)},
            {"id": 8, "file_url": "https://example.test/8.webm", "digest_item_key": ("tag-b", 8)},
        ]
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            data="sub_digest_send",
            answer=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        update = SimpleNamespace(callback_query=query)
        partial = bot.DigestDeliveryResult(
            delivered_ids=[("tag-a", 7)],
            ambiguous_ids=[("tag-b", 8)],
        )

        with (
            patch.object(
                bot,
                "claim_subscription_digest",
                AsyncMock(return_value=("claim-token", posts)),
            ),
            patch.object(
                bot,
                "send_digest_posts",
                AsyncMock(side_effect=bot.DigestDeliveryCancelled(partial)),
            ),
            patch.object(
                bot,
                "get_subscription_digest_claim_keys",
                AsyncMock(return_value={("tag-a", 7), ("tag-b", 8)}),
            ),
            patch.object(
                bot, "renew_subscription_digest_claim", AsyncMock(return_value=True)
            ),
            patch.object(
                bot,
                "finish_subscription_digest_claim",
                AsyncMock(return_value=(1, 1)),
            ) as finish,
        ):
            with self.assertRaises(bot.DigestDeliveryCancelled):
                await bot.button_handler(update, SimpleNamespace())

        finish.assert_awaited_once_with(
            1, "claim-token", [("tag-a", 7)]
        )

    async def test_manual_digest_cancelled_before_send_releases_claim_and_propagates(self):
        posts = [
            {"id": 7, "file_url": "https://example.test/7.gif", "digest_item_key": ("tag-a", 7)},
        ]
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            data="sub_digest_send",
            answer=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        update = SimpleNamespace(callback_query=query)

        with (
            patch.object(
                bot,
                "claim_subscription_digest",
                AsyncMock(return_value=("claim-token", posts)),
            ),
            patch.object(
                bot,
                "send_digest_posts",
                AsyncMock(side_effect=asyncio.CancelledError),
            ),
            patch.object(
                bot,
                "get_subscription_digest_claim_keys",
                AsyncMock(return_value={("tag-a", 7)}),
            ),
            patch.object(
                bot, "renew_subscription_digest_claim", AsyncMock(return_value=True)
            ),
            patch.object(
                bot,
                "finish_subscription_digest_claim",
                AsyncMock(),
            ) as finish,
            patch.object(
                bot,
                "release_subscription_digest_claim",
                AsyncMock(return_value=1),
            ) as release,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot.button_handler(update, SimpleNamespace())

        finish.assert_not_awaited()
        release.assert_awaited_once_with(1, "claim-token")

    async def test_manual_album_cancellation_marks_started_request_ambiguous(self):
        message = SimpleNamespace(
            reply_media_group=AsyncMock(side_effect=asyncio.CancelledError)
        )
        posts = [
            {"id": 1, "file_url": "https://example.test/1.jpg"},
            {"id": 2, "file_url": "https://example.test/2.png"},
        ]
        with patch.object(bot, "get_user_settings", AsyncMock(return_value={})):
            with self.assertRaises(bot.DigestDeliveryCancelled) as raised:
                await bot.send_digest_posts(message, 1, posts)

        self.assertEqual(
            raised.exception.result.ambiguous_ids,
            [("digest", 1), ("digest", 2)],
        )

    async def test_scheduled_album_cancellation_marks_started_request_ambiguous(self):
        telegram_bot = SimpleNamespace(
            send_media_group=AsyncMock(side_effect=asyncio.CancelledError)
        )
        posts = [
            {"id": 1, "file_url": "https://example.test/1.jpg"},
            {"id": 2, "file_url": "https://example.test/2.png"},
        ]
        with (
            patch.object(bot, "get_user_settings", AsyncMock(return_value={})),
            patch.object(
                bot.telegram_rate_limiter,
                "wait_for_slot",
                AsyncMock(return_value=True),
            ),
        ):
            with self.assertRaises(bot.DigestDeliveryCancelled) as raised:
                await bot.send_digest_to_chat(telegram_bot, 1, posts)

        self.assertEqual(
            raised.exception.result.ambiguous_ids,
            [("digest", 1), ("digest", 2)],
        )

    async def test_lost_lease_stops_delivery_before_telegram_request(self):
        message = SimpleNamespace(reply_media_group=AsyncMock())
        lease = SimpleNamespace(ensure_owned=AsyncMock(return_value=False))
        posts = [
            {"id": 1, "file_url": "https://example.test/1.jpg"},
            {"id": 2, "file_url": "https://example.test/2.png"},
        ]
        with patch.object(bot, "get_user_settings", AsyncMock(return_value={})):
            result = await bot.send_digest_posts(message, 1, posts, lease=lease)

        message.reply_media_group.assert_not_awaited()
        self.assertEqual(result.failed_ids, [("digest", 1), ("digest", 2)])

    async def test_expired_lease_stops_before_next_sequential_telegram_request(self):
        message = object()
        posts = [
            {"id": 1, "file_url": "https://example.test/1.gif"},
            {"id": 2, "file_url": "https://example.test/2.webm"},
        ]
        lease = bot.DigestClaimLease(1, "claim-token")
        renew = AsyncMock(side_effect=[True, True, False])
        with (
            patch.object(bot, "renew_subscription_digest_claim", renew),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={})),
            patch.object(bot, "send_post_media", AsyncMock(return_value=True)) as send,
        ):
            self.assertTrue(await lease.start())
            result = await bot.send_digest_posts(message, 1, posts, lease=lease)
            await lease.stop()

        self.assertEqual(send.await_count, 1)
        self.assertEqual(result.delivered_ids, [("digest", 1)])
        self.assertEqual(result.failed_ids, [("digest", 2)])

    async def test_digest_lease_heartbeat_renews_while_delivery_is_blocked(self):
        lease = bot.DigestClaimLease(1, "claim-token")
        lease.HEARTBEAT_SECONDS = 0.01
        renew = AsyncMock(return_value=True)
        with patch.object(bot, "renew_subscription_digest_claim", renew):
            self.assertTrue(await lease.start())
            await asyncio.sleep(0.035)
            await lease.stop()

        # One immediate renewal plus at least one background heartbeat.
        self.assertGreaterEqual(renew.await_count, 2)
        renew.assert_any_await(1, "claim-token")

    async def test_removed_claim_items_are_filtered_before_manual_delivery(self):
        posts = [{
            "id": 7,
            "file_url": "https://example.test/7.jpg",
            "digest_item_key": ("tag", 7),
        }]
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            data="sub_digest_send",
            answer=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        with (
            patch.object(
                bot, "claim_subscription_digest",
                AsyncMock(return_value=("claim-token", posts)),
            ),
            patch.object(
                bot, "get_subscription_digest_claim_keys",
                AsyncMock(return_value=set()),
            ),
            patch.object(bot, "send_digest_posts", AsyncMock()) as send_digest,
            patch.object(
                bot, "finish_subscription_digest_claim",
                AsyncMock(return_value=(0, 0)),
            ) as finish,
        ):
            await bot.button_handler(
                SimpleNamespace(callback_query=query), SimpleNamespace()
            )

        send_digest.assert_not_awaited()
        finish.assert_awaited_once_with(1, "claim-token", [])


class DigestSubscriptionLockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertEqual(bot.digest_subscription_locks, {})

    async def asyncTearDown(self):
        self.assertEqual(bot.digest_subscription_locks, {})

    async def test_concurrent_operations_share_one_registry_lock(self):
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def first_operation():
            async with bot.digest_subscription_lock(1, "tag"):
                first_entered.set()
                await release_first.wait()

        async def second_operation():
            async with bot.digest_subscription_lock(1, "tag"):
                return

        first = asyncio.create_task(first_operation())
        await first_entered.wait()
        original_entry = bot.digest_subscription_locks[(1, "tag")]
        second = asyncio.create_task(second_operation())
        await asyncio.sleep(0)

        self.assertIs(bot.digest_subscription_locks[(1, "tag")], original_entry)
        self.assertEqual(original_entry.references, 2)
        release_first.set()
        await asyncio.gather(first, second)

    async def test_registry_entry_is_removed_after_last_operation(self):
        async with bot.digest_subscription_lock(1, "tag"):
            self.assertIn((1, "tag"), bot.digest_subscription_locks)

        self.assertNotIn((1, "tag"), bot.digest_subscription_locks)

    async def test_cancelled_waiter_releases_its_registry_reference(self):
        async with bot.digest_subscription_lock(1, "tag"):
            entry = bot.digest_subscription_locks[(1, "tag")]

            async def wait_for_same_lock():
                async with bot.digest_subscription_lock(1, "tag"):
                    return

            waiter = asyncio.create_task(wait_for_same_lock())
            await asyncio.sleep(0)
            self.assertEqual(entry.references, 2)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertEqual(entry.references, 1)

        self.assertNotIn((1, "tag"), bot.digest_subscription_locks)

    async def test_different_subscriptions_do_not_block_each_other(self):
        second_entered = asyncio.Event()

        async def use_second_lock():
            async with bot.digest_subscription_lock(1, "other-tag"):
                second_entered.set()

        async with bot.digest_subscription_lock(1, "tag"):
            task = asyncio.create_task(use_second_lock())
            await asyncio.wait_for(second_entered.wait(), timeout=0.5)
            await task

    async def test_exception_releases_lock_and_registry_entry(self):
        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with bot.digest_subscription_lock(1, "tag"):
                raise RuntimeError("boom")

        self.assertNotIn((1, "tag"), bot.digest_subscription_locks)
        async with bot.digest_subscription_lock(1, "tag"):
            pass


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

    async def test_ambiguous_digest_waits_for_next_scheduler_interval(self):
        app = SimpleNamespace(bot=object())
        posts = [{
            "id": 1,
            "file_url": "https://example.test/1.jpg",
            "digest_item_key": ("tag", 1),
        }]
        delivery = bot.DigestDeliveryResult(ambiguous_ids=[("tag", 1)])
        lease = SimpleNamespace(
            start=AsyncMock(return_value=True),
            stop=AsyncMock(),
        )

        with (
            patch.object(bot, "release_stale_subscription_claims", AsyncMock()),
            patch.object(bot, "get_due_subscriptions", AsyncMock(return_value=[])),
            patch.object(
                bot, "get_due_digest_users", AsyncMock(return_value=[1])
            ) as due_users,
            patch.object(
                bot, "claim_subscription_digest",
                AsyncMock(return_value=("claim-token", posts)),
            ) as claim,
            patch.object(
                bot, "get_subscription_digest_claim_keys",
                AsyncMock(return_value={("tag", 1)}),
            ),
            patch.object(bot, "DigestClaimLease", return_value=lease),
            patch.object(
                bot, "send_digest_to_chat", AsyncMock(return_value=delivery)
            ) as send,
            patch.object(
                bot, "finish_subscription_digest_claim",
                AsyncMock(return_value=(0, 1)),
            ) as finish,
            patch.object(
                bot.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)
            ) as sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot.process_subscriptions(app)

        due_users.assert_awaited_once()
        claim.assert_awaited_once_with(1, 10)
        send.assert_awaited_once()
        finish.assert_awaited_once_with(1, "claim-token", [])
        sleep.assert_awaited_once_with(bot.SUBSCRIPTION_CHECK_INTERVAL_SECONDS)


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
