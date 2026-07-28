import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from api_handler import APITemporaryError


def callback_update(data: str, user_id: int = 1):
    message = SimpleNamespace(reply_text=AsyncMock())
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=message,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(callback_query=query), query


class UserStateRaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.user_operation_gate.reset_for_tests()
        bot.temporary_user_state.clear_all()
        bot.issued_one_shot_callbacks.clear()
        bot.stale_flow_results_discarded = 0
        bot.duplicate_callbacks_rejected = 0

    def tearDown(self):
        bot.temporary_user_state.clear_all()
        bot.issued_one_shot_callbacks.clear()
        bot.user_operation_gate.reset_for_tests()

    async def test_two_messages_consume_state_once(self):
        await bot.begin_user_flow(1, "waiting_search")
        results = await asyncio.gather(
            bot.claim_user_message_state(1),
            bot.claim_user_message_state(1),
        )
        self.assertEqual([result[0] for result in results].count("waiting_search"), 1)

    async def test_cancel_invalidates_late_commit_and_new_flow_wins(self):
        generation = await bot.begin_user_flow(1, "waiting_builder_include")
        state, claimed_generation, _snapshot = await bot.claim_user_message_state(1)
        self.assertEqual((state, claimed_generation), ("waiting_builder_include", generation))
        await bot.invalidate_user_flow(1)
        await bot.begin_user_flow(1, "waiting_gallery")
        committed = await bot.commit_flow_if_current(
            1,
            claimed_generation,
            lambda: bot.user_states.__setitem__(1, "waiting_builder_exclude"),
        )
        self.assertFalse(committed)
        self.assertEqual(bot.user_states[1], "waiting_gallery")
        self.assertEqual(bot.stale_flow_results_discarded, 1)

    async def test_mutable_builder_snapshot_cannot_replace_new_builder(self):
        generation = await bot.begin_user_flow(
            1,
            "waiting_builder_exclude",
            builder=(bot.search_builders, {"include": "old"}),
        )
        _state, _generation, snapshot = await bot.claim_user_message_state(1)
        await bot.invalidate_user_flow(1)
        await bot.begin_user_flow(
            1,
            "waiting_builder_exclude",
            builder=(bot.search_builders, {"include": "new"}),
        )
        snapshot["builder"]["include"] = "changed-old"
        committed = await bot.commit_flow_if_current(
            1,
            generation,
            lambda: bot.search_builders.__setitem__(1, snapshot["builder"]),
        )
        self.assertFalse(committed)
        self.assertEqual(bot.search_builders[1]["include"], "new")

    async def test_cleanup_does_not_remove_state_in_guarded_section(self):
        bot.user_states[1] = "waiting"
        bot.temporary_user_state.touch(1, now=0)
        async with bot.guarded_user_state(1):
            self.assertEqual(bot.temporary_user_state.cleanup_expired(1, now=10), 0)
            self.assertEqual(bot.user_states[1], "waiting")

    async def test_search_cooldown_is_atomic_between_concurrent_updates(self):
        with patch.object(bot, "SEARCH_COOLDOWN_SECONDS", 30):
            results = await asyncio.gather(
                bot.reserve_search_cooldown(1),
                bot.reserve_search_cooldown(1),
            )
        self.assertEqual(sorted(results), [False, True])

    async def test_validation_error_does_not_consume_search_cooldown(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        self.assertFalse(await bot.send_image(message, 1, "   "))
        self.assertNotIn(1, bot.user_last_search_at)

    async def test_flow_invalidation_does_not_reset_search_cooldown(self):
        with patch.object(bot, "SEARCH_COOLDOWN_SECONDS", 30):
            self.assertFalse(await bot.reserve_search_cooldown(1))
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=1),
                message=SimpleNamespace(reply_text=AsyncMock()),
            )
            with (
                patch.object(bot, "zip_export_manager", None),
                patch.object(bot, "build_main_menu_text", AsyncMock(return_value="menu")),
                patch.object(bot, "get_user_main_keyboard", AsyncMock(return_value=None)),
            ):
                await bot.cancel_command(update, SimpleNamespace())
            self.assertTrue(await bot.reserve_search_cooldown(1))

    async def test_start_back_and_persistent_menu_preserve_search_cooldown(self):
        bot.user_last_search_at[1] = 123.0
        message = SimpleNamespace(reply_text=AsyncMock())
        start_update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1), message=message
        )
        with (
            patch.object(bot, "get_subscription_pause_until", AsyncMock(return_value=None)),
            patch.object(bot, "get_user_persistent_keyboard", AsyncMock(return_value=None)),
        ):
            await bot.start(start_update, SimpleNamespace())
        self.assertEqual(bot.user_last_search_at[1], 123.0)

        back_update, _query = callback_update("back")
        with (
            patch.object(bot, "safe_query_answer", AsyncMock()),
            patch.object(bot, "build_main_menu_text", AsyncMock(return_value="menu")),
            patch.object(bot, "get_user_main_keyboard", AsyncMock(return_value=None)),
        ):
            await bot.button_handler(back_update, SimpleNamespace())
        self.assertEqual(bot.user_last_search_at[1], 123.0)

        menu_message = SimpleNamespace(text=bot.PERSISTENT_MENU, reply_text=AsyncMock())
        menu_update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1), message=menu_message
        )
        with (
            patch.object(bot, "build_main_menu_text", AsyncMock(return_value="menu")),
            patch.object(bot, "get_user_main_keyboard", AsyncMock(return_value=None)),
        ):
            await bot.message_handler(menu_update, SimpleNamespace())
        self.assertEqual(bot.user_last_search_at[1], 123.0)

    async def test_search_is_available_after_cooldown_expires(self):
        with (
            patch.object(bot, "SEARCH_COOLDOWN_SECONDS", 30),
            patch.object(bot.time, "monotonic", side_effect=[100.0, 131.0]),
        ):
            self.assertFalse(await bot.reserve_search_cooldown(1))
            self.assertFalse(await bot.reserve_search_cooldown(1))

    async def test_external_api_runs_outside_user_gate(self):
        message = SimpleNamespace(
            reply_text=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock()))
        )

        async def api_call(*_args, **_kwargs):
            self.assertFalse(bot.user_operation_gate.held_by_current_task())
            raise APITemporaryError("temporary")

        with (
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=set())),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={})),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(bot.api, "get_random_image", side_effect=api_call),
        ):
            self.assertFalse(await bot.send_image(message, 1, "tag"))

    async def test_duplicate_subscription_create_executes_db_once(self):
        _text, keyboard = bot.get_subscription_preview(
            "tag", 10, 1, issuer=bot.callback_issuer_for(1)
        )
        data = keyboard.inline_keyboard[0][0].callback_data
        first_update, first_query = callback_update(data)
        second_update, second_query = callback_update(data)
        add = AsyncMock(return_value=True)
        with (
            patch.object(bot, "safe_query_answer", AsyncMock()),
            patch.object(bot, "add_subscription", add),
            patch.object(bot, "build_subscription_added_text", AsyncMock(return_value="ok")),
            patch.object(bot, "get_user_subscriptions_keyboard", AsyncMock(return_value=None)),
        ):
            await asyncio.gather(
                bot.button_handler(first_update, SimpleNamespace()),
                bot.button_handler(second_update, SimpleNamespace()),
            )
        add.assert_awaited_once()
        self.assertEqual(bot.duplicate_callbacks_rejected, 1)
        self.assertEqual(
            first_query.edit_message_text.await_count
            + second_query.edit_message_text.await_count,
            1,
        )

    async def test_duplicate_toggle_and_delete_are_rejected(self):
        toggle_data = bot.store_user_one_shot_payload("sub_toggle", "tag", 1)
        delete_data = bot.store_user_one_shot_payload("sub_remove_do", "tag", 1)
        toggle = AsyncMock(return_value=SimpleNamespace(status="ok", is_active=False))
        remove = AsyncMock(return_value=True)

        @asynccontextmanager
        async def digest_lock(*_args):
            yield

        with (
            patch.object(bot, "safe_query_answer", AsyncMock()),
            patch.object(bot, "toggle_subscription", toggle),
            patch.object(bot, "remove_subscription", remove),
            patch.object(bot, "digest_subscription_lock", digest_lock),
            patch.object(bot, "get_user_subscriptions_keyboard", AsyncMock(return_value=None)),
        ):
            for data in (toggle_data, delete_data):
                updates = [callback_update(data)[0] for _ in range(2)]
                await asyncio.gather(*(
                    bot.button_handler(update, SimpleNamespace()) for update in updates
                ))
        toggle.assert_awaited_once()
        remove.assert_awaited_once()

    async def test_read_only_callback_is_repeatable_and_stale_is_rejected(self):
        self.assertTrue(await bot.consume_one_shot_callback(1, "fav_page_0"))
        self.assertTrue(await bot.consume_one_shot_callback(1, "fav_page_0"))
        data = bot.store_user_one_shot_payload("sub_create", "{}", 1)
        await bot.invalidate_user_flow(1)
        self.assertFalse(await bot.consume_one_shot_callback(1, data))

    async def test_message_state_consumes_only_its_related_snapshot(self):
        bot.pending_bulk_posts[1] = [1, 2]
        bot.pending_preset_queries[1] = "preset"
        bot.pending_subscription_options[1] = "subscription"
        state, _generation, snapshot = await bot.claim_user_message_state(1)
        self.assertIsNone(state)
        self.assertEqual(snapshot, {})
        self.assertEqual(bot.pending_bulk_posts[1], [1, 2])
        self.assertEqual(bot.pending_preset_queries[1], "preset")
        self.assertEqual(bot.pending_subscription_options[1], "subscription")

        await bot.begin_user_flow(1, "waiting_builder_exclude")
        bot.search_builders[1] = {"include": "tag"}
        state, _generation, snapshot = await bot.claim_user_message_state(1)
        self.assertEqual(state, "waiting_builder_exclude")
        self.assertEqual(snapshot["builder"], {"include": "tag"})
        self.assertEqual(bot.pending_bulk_posts[1], [1, 2])

    async def test_strict_issuance_eviction_and_forgery_are_rejected(self):
        consumed = bot.store_side_effect_callback("later_add_1", 1)
        self.assertTrue(await bot.consume_one_shot_callback(1, consumed))
        for post_id in range(bot.ONE_SHOT_CALLBACK_MAX_PER_USER + 1):
            data = f"later_add_{post_id + 100}"
            bot.register_one_shot_callback(
                1,
                data,
                logical_action="side_effect",
                canonical_payload=data,
            )
        self.assertFalse(await bot.consume_one_shot_callback(1, consumed))

        oldest_issued = next(iter(bot.issued_one_shot_callbacks))
        for post_id in range(bot.ONE_SHOT_CALLBACK_MAX_PER_USER + 1):
            data = f"later_del_{post_id + 1000}"
            bot.register_one_shot_callback(
                1,
                data,
                logical_action="side_effect",
                canonical_payload=data,
            )
        self.assertFalse(await bot.consume_one_shot_callback(1, oldest_issued))
        forged = f"act_p{bot.ONE_SHOT_PROCESS_EPOCH}u1g0-deadbeefdeadbeef"
        self.assertFalse(await bot.consume_one_shot_callback(1, forged))

    def test_registry_keeps_240_fresh_tokens_for_one_user(self):
        first = None
        for index in range(240):
            data = f"later_add_{index}"
            bot.register_one_shot_callback(
                1,
                data,
                logical_action="side_effect",
                canonical_payload=data,
            )
            first = first or data
        self.assertIn(first, bot.issued_one_shot_callbacks)
        self.assertEqual(len(bot.issued_one_shot_callbacks), 240)

    def test_user_flood_does_not_evict_other_users_fresh_token(self):
        for index in range(100):
            data = f"later_add_{index}"
            bot.register_one_shot_callback(
                1, data, logical_action="side_effect", canonical_payload=data
            )
        other = bot.register_one_shot_callback(
            2,
            "later_add_99999",
            logical_action="side_effect",
            canonical_payload="later_add_99999",
        )
        for index in range(700):
            data = f"later_del_{index}"
            bot.register_one_shot_callback(
                1, data, logical_action="side_effect", canonical_payload=data
            )
        self.assertIn(other, bot.issued_one_shot_callbacks)
        self.assertLessEqual(
            sum(entry.owner_id == 1 for entry in bot.issued_one_shot_callbacks.values()),
            bot.ONE_SHOT_CALLBACK_MAX_PER_USER,
        )

    def test_terminal_entries_are_evicted_before_issued(self):
        with patch.object(bot, "ONE_SHOT_CALLBACK_MAX_PER_USER", 3):
            for data in ("later_add_1", "later_add_2", "later_add_3"):
                bot.register_one_shot_callback(
                    1, data, logical_action="side_effect", canonical_payload=data
                )
            bot.issued_one_shot_callbacks["later_add_2"].status = "consumed"
            bot.issued_one_shot_callbacks["later_add_3"].status = "stale"
            bot.register_one_shot_callback(
                1,
                "later_add_4",
                logical_action="side_effect",
                canonical_payload="later_add_4",
            )
        self.assertIn("later_add_1", bot.issued_one_shot_callbacks)
        self.assertNotIn("later_add_2", bot.issued_one_shot_callbacks)

    def test_global_limit_and_processing_ttl_rules(self):
        with (
            patch.object(bot, "ONE_SHOT_CALLBACK_MAX_PER_USER", 10),
            patch.object(bot, "ONE_SHOT_CALLBACK_MAX_GLOBAL", 5),
        ):
            for user_id in range(1, 7):
                data = f"later_add_{user_id}"
                bot.register_one_shot_callback(
                    user_id,
                    data,
                    logical_action="side_effect",
                    canonical_payload=data,
                )
            self.assertLessEqual(len(bot.issued_one_shot_callbacks), 5)

        bot.issued_one_shot_callbacks.clear()
        processing = bot.register_one_shot_callback(
            1,
            "later_add_10",
            logical_action="side_effect",
            canonical_payload="later_add_10",
        )
        expired = bot.register_one_shot_callback(
            2,
            "later_add_20",
            logical_action="side_effect",
            canonical_payload="later_add_20",
        )
        bot.issued_one_shot_callbacks[processing].status = "processing"
        for entry in bot.issued_one_shot_callbacks.values():
            entry.created_at = 0
        bot.cleanup_one_shot_callbacks(
            now=bot.ONE_SHOT_CALLBACK_TTL_SECONDS + 1, max_removals=10
        )
        self.assertIn(processing, bot.issued_one_shot_callbacks)
        self.assertNotIn(expired, bot.issued_one_shot_callbacks)

    async def test_logical_action_deduplicates_different_tokens(self):
        first = bot.store_user_one_shot_payload("sub_toggle", "tag", 1)
        second = bot.store_user_one_shot_payload("sub_toggle", "tag", 1)
        self.assertNotEqual(first, second)
        toggle = AsyncMock(return_value=SimpleNamespace(status="ok", is_active=False))
        with (
            patch.object(bot, "safe_query_answer", AsyncMock()),
            patch.object(bot, "toggle_subscription", toggle),
            patch.object(bot, "get_user_subscriptions_keyboard", AsyncMock(return_value=None)),
        ):
            await asyncio.gather(
                bot.button_handler(callback_update(first)[0], SimpleNamespace()),
                bot.button_handler(callback_update(second)[0], SimpleNamespace()),
            )
        toggle.assert_awaited_once()

    async def test_old_gallery_collection_button_keeps_original_post_ids(self):
        data = bot.store_side_effect_callback("gallery_col_add:5:1,2", 1)
        bot.pending_bulk_posts[1] = [9]
        update, query = callback_update(data)
        posts = {1: {"id": 1}, 2: {"id": 2}, 9: {"id": 9}}
        get_post = AsyncMock(side_effect=lambda post_id: posts[post_id])
        with (
            patch.object(bot, "safe_query_answer", AsyncMock()),
            patch.object(bot, "get_known_post", get_post),
            patch.object(bot, "add_favorite", AsyncMock(return_value=True)),
            patch.object(
                bot, "add_favorite_to_collection", AsyncMock(return_value=True)
            ),
        ):
            await bot.button_handler(update, SimpleNamespace())
        self.assertEqual([call.args[0] for call in get_post.await_args_list], [1, 2])
        query.message.reply_text.assert_awaited_once()

    async def test_cancel_during_collection_read_creates_no_prompt_or_tokens(self):
        data = bot.store_user_one_shot_payload("gallery_collection", "1,2", 1)
        initial_tokens = set(bot.issued_one_shot_callbacks)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_collections(*_args):
            entered.set()
            await release.wait()
            return [{"id": 5, "name": "old"}]

        update, query = callback_update(data)
        with (
            patch.object(bot, "safe_query_answer", AsyncMock()),
            patch.object(bot, "get_favorite_collections", side_effect=delayed_collections),
        ):
            task = asyncio.create_task(bot.button_handler(update, SimpleNamespace()))
            await entered.wait()
            await bot.invalidate_user_flow(1)
            release.set()
            await task
        self.assertEqual(set(bot.issued_one_shot_callbacks), initial_tokens)
        query.message.reply_text.assert_not_awaited()

    async def test_cancel_during_search_does_not_issue_new_generation_tokens(self):
        generation = await bot.begin_user_flow(1, "waiting_search")
        entered = asyncio.Event()
        release = asyncio.Event()
        message = SimpleNamespace(
            reply_text=AsyncMock(
                return_value=SimpleNamespace(delete=AsyncMock())
            )
        )

        async def delayed_search(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return {"id": 42, "file_url": "https://example.test/42.jpg"}

        send_media = AsyncMock(return_value=True)
        with (
            patch.object(bot, "SEARCH_COOLDOWN_SECONDS", 0),
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=set())),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={"show_caption": False})),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(bot.api, "get_random_image", side_effect=delayed_search),
            patch.object(bot.api, "save_search_state", AsyncMock()),
            patch.object(bot, "remember_and_cache_post", AsyncMock()),
            patch.object(bot, "save_user_query", AsyncMock()),
            patch.object(bot, "send_post_media", send_media),
            patch.object(bot, "mark_post_sent", AsyncMock()),
        ):
            task = asyncio.create_task(
                bot.send_image(
                    message,
                    1,
                    "tag",
                    expected_generation=generation,
                )
            )
            await entered.wait()
            await bot.invalidate_user_flow(1)
            release.set()
            await task
        self.assertEqual(bot.issued_one_shot_callbacks, {})
        self.assertIsNone(send_media.await_args.args[3])

    async def test_stale_generation_bound_issuer_cannot_register(self):
        generation = await bot.begin_user_flow(1, "waiting_search")
        issuer = bot.callback_issuer_for(1, generation)
        await bot.invalidate_user_flow(1)
        with self.assertRaises(bot.StaleCallbackIssuer):
            issuer.side_effect("fav_42")
        self.assertEqual(bot.issued_one_shot_callbacks, {})

    async def test_later_open_media_keyboard_issues_clickable_side_effect_token(self):
        post = {"id": 42, "file_url": "https://example.test/42.jpg"}
        send_media = AsyncMock(return_value=True)
        with (
            patch.object(bot, "safe_query_answer", AsyncMock()),
            patch.object(bot, "get_read_later", AsyncMock(return_value=[post])),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={})),
            patch.object(bot, "send_post_media", send_media),
        ):
            await bot.button_handler(
                callback_update("later_open_42")[0], SimpleNamespace()
            )
        keyboard = send_media.await_args.kwargs["keyboard"]
        favorite_token = keyboard.inline_keyboard[0][0].callback_data
        self.assertIn(favorite_token, bot.issued_one_shot_callbacks)

        add_favorite = AsyncMock(return_value=True)
        with (
            patch.object(bot, "safe_query_answer", AsyncMock()),
            patch.object(bot, "get_known_post", AsyncMock(return_value=post)),
            patch.object(bot, "add_favorite", add_favorite),
        ):
            await bot.button_handler(
                callback_update(favorite_token)[0], SimpleNamespace()
            )
        add_favorite.assert_awaited_once_with(1, post)

    async def test_scheduled_digest_gets_working_side_effect_tokens(self):
        post = {"id": 42, "file_url": "https://example.test/42.jpg"}
        send_media = AsyncMock(return_value=True)
        with (
            patch.object(bot, "get_user_settings", AsyncMock(return_value={})),
            patch.object(bot, "partition_digest_posts", return_value=([], [post])),
            patch.object(bot, "send_post_media_to_chat", send_media),
        ):
            result = await bot.send_digest_to_chat(SimpleNamespace(), 1, [post])
        self.assertEqual(len(result.delivered_ids), 1)
        keyboard = send_media.await_args.kwargs["keyboard"]
        token = keyboard.inline_keyboard[0][0].callback_data
        self.assertIn(token, bot.issued_one_shot_callbacks)
        self.assertEqual(bot.resolved_callback_data(token), "fav_42")

    def test_production_media_keyboards_require_and_issue_opaque_tokens(self):
        with self.assertRaises((TypeError, ValueError)):
            bot.get_image_keyboard(42)

        issuer = bot.callback_issuer_for(1)
        keyboards = [
            bot.get_image_keyboard(42, side_effect_callback=issuer),
            bot.get_random_image_keyboard(42, side_effect_callback=issuer),
            bot.get_subscription_image_keyboard(
                42, "tag", side_effect_callback=issuer
            ),
            bot.get_subscription_gallery_keyboard(
                "token", 0, 1, 42, side_effect_callback=issuer
            ),
            bot.get_favorites_gallery_keyboard(
                0, 1, 42, side_effect_callback=issuer
            ),
        ]
        logical_side_effects = {
            "fav_42",
            "later_add_42",
            "sub_fav_42",
            "sub_post_del_token_42_0",
            "fav_del_42_0",
            "fav_col_pick_42",
            "fav_note_42",
        }
        found = set()
        for keyboard in keyboards:
            for row in keyboard.inline_keyboard:
                for button in row:
                    data = button.callback_data
                    if data in bot.issued_one_shot_callbacks:
                        self.assertTrue(data.startswith("act_"))
                        found.add(bot.resolved_callback_data(data))
        self.assertTrue(logical_side_effects.issubset(found))

    def test_one_shot_callback_data_stays_within_telegram_limit(self):
        user_id = 2**63 - 1
        generation = 2**63 - 1
        bot.temporary_user_state._generations[user_id] = generation
        issuer = bot.callback_issuer_for(user_id, generation)
        callbacks = [
            issuer.side_effect("gallery_col_add:999999999999999999:1,2,3,4,5,6,7,8,9"),
            issuer.payload("gallery_collection", "1,2,3,4,5,6,7,8,9"),
            issuer.payload("sub_remove_do", "x" * 200),
        ]
        self.assertTrue(all(len(data.encode("utf-8")) <= 64 for data in callbacks))

    async def test_other_user_cannot_consume_owned_callback(self):
        data = bot.store_user_one_shot_payload("sub_create", "{}", 1)
        self.assertFalse(await bot.consume_one_shot_callback(2, data))
        self.assertTrue(await bot.consume_one_shot_callback(1, data))
        data = bot.store_user_one_shot_payload("sub_create", "{\"new\": true}", 1)
        bot.issued_one_shot_callbacks.clear()
        self.assertFalse(await bot.consume_one_shot_callback(1, data))

    async def test_consumed_callback_survives_temporary_state_cleanup(self):
        data = bot.store_user_one_shot_payload("sub_create", "{}", 1)
        self.assertTrue(await bot.consume_one_shot_callback(1, data))
        bot.temporary_user_state.clear_user(1)
        self.assertFalse(await bot.consume_one_shot_callback(1, data))

    async def test_static_one_shot_is_scoped_by_user_and_generation(self):
        data = bot.store_side_effect_callback("later_add_42", 1)
        self.assertTrue(await bot.consume_one_shot_callback(1, data))
        self.assertFalse(await bot.consume_one_shot_callback(1, data))
        self.assertFalse(await bot.consume_one_shot_callback(2, data))
        await bot.invalidate_user_flow(1)
        self.assertFalse(await bot.consume_one_shot_callback(1, data))

    async def test_rerendered_mutable_controls_remain_repeatable(self):
        for data in ("settings_spoiler", "toggle_show_tags", "gallery_size_up"):
            self.assertTrue(await bot.consume_one_shot_callback(1, data))
            self.assertTrue(await bot.consume_one_shot_callback(1, data))
            self.assertTrue(await bot.consume_one_shot_callback(2, data))

    async def test_zip_and_telegram_actions_run_outside_user_gate(self):
        update, _query = callback_update("fav_export")

        async def enqueue(*_args):
            self.assertFalse(bot.user_operation_gate.held_by_current_task())

        async def answer(*_args, **_kwargs):
            self.assertFalse(bot.user_operation_gate.held_by_current_task())

        with (
            patch.object(bot, "enqueue_favorites_zip_export", side_effect=enqueue),
            patch.object(bot, "safe_query_answer", side_effect=answer),
        ):
            await bot.button_handler(update, SimpleNamespace())

    async def test_callback_concurrent_with_cancel_cannot_restore_state(self):
        await bot.begin_user_flow(1, "waiting_gallery")
        update, _query = callback_update("search")
        answer_entered = asyncio.Event()
        release_answer = asyncio.Event()

        async def delayed_answer(*_args, **_kwargs):
            answer_entered.set()
            await release_answer.wait()

        with patch.object(bot, "safe_query_answer", side_effect=delayed_answer):
            callback_task = asyncio.create_task(
                bot.button_handler(update, SimpleNamespace())
            )
            await answer_entered.wait()
            await bot.invalidate_user_flow(1)
            release_answer.set()
            await callback_task
        self.assertNotIn(1, bot.user_states)
        _query.edit_message_text.assert_not_awaited()

    async def test_callback_db_result_after_cancel_cannot_start_old_flow(self):
        update, _query = callback_update("preset_save_current")
        db_entered = asyncio.Event()
        release_db = asyncio.Event()

        async def delayed_query(_user_id):
            self.assertFalse(bot.user_operation_gate.held_by_current_task())
            db_entered.set()
            await release_db.wait()
            return ("old query",)

        with (
            patch.object(bot, "safe_query_answer", AsyncMock()),
            patch.object(bot, "get_user_query", side_effect=delayed_query),
        ):
            callback_task = asyncio.create_task(
                bot.button_handler(update, SimpleNamespace())
            )
            await db_entered.wait()
            await bot.invalidate_user_flow(1)
            release_db.set()
            await callback_task
        self.assertNotIn(1, bot.user_states)
        self.assertNotIn(1, bot.pending_preset_queries)
        _query.message.reply_text.assert_not_awaited()

    async def test_cancel_during_favorite_note_read_suppresses_stale_prompt(self):
        data = bot.store_side_effect_callback("fav_note_42", 1)
        update, query = callback_update(data)
        read_started = asyncio.Event()
        release_read = asyncio.Event()

        async def delayed_note(*_args):
            read_started.set()
            await release_read.wait()
            return "old"

        with (
            patch.object(bot, "safe_query_answer", AsyncMock()),
            patch.object(bot, "get_favorite_note", side_effect=delayed_note),
        ):
            task = asyncio.create_task(bot.button_handler(update, SimpleNamespace()))
            await read_started.wait()
            await bot.invalidate_user_flow(1)
            release_read.set()
            await task
        self.assertNotIn(1, bot.user_states)
        query.message.reply_text.assert_not_awaited()

    async def test_callback_is_rejected_after_shutdown_start(self):
        data = bot.store_side_effect_callback("later_add_42", 1)
        await bot.user_operation_gate.shutdown()
        bot.issued_one_shot_callbacks.clear()
        await bot.user_operation_gate.start()
        self.assertFalse(await bot.consume_one_shot_callback(1, data))

    async def test_generation_counter_does_not_aba_after_state_cleanup(self):
        old_generation = await bot.begin_user_flow(1, "waiting_search")
        data = bot.store_side_effect_callback("later_add_42", 1)
        bot.temporary_user_state.clear_all()
        new_generation = await bot.begin_user_flow(1, "waiting_search")
        self.assertGreater(new_generation, old_generation)
        self.assertFalse(await bot.consume_one_shot_callback(1, data))

    async def test_toggle_error_and_idempotent_failure_remain_consumed(self):
        toggle_data = bot.store_user_one_shot_payload("sub_toggle", "tag", 1)
        toggle = AsyncMock(side_effect=RuntimeError("ambiguous"))
        with (
            patch.object(bot, "safe_query_answer", AsyncMock()),
            patch.object(bot, "toggle_subscription", toggle),
        ):
            with self.assertRaises(RuntimeError):
                await bot.button_handler(
                    callback_update(toggle_data)[0], SimpleNamespace()
                )
            await bot.button_handler(callback_update(toggle_data)[0], SimpleNamespace())
        toggle.assert_awaited_once()
        self.assertEqual(
            bot.issued_one_shot_callbacks[toggle_data].status, "consumed"
        )

        later_data = bot.store_side_effect_callback("later_add_42", 1)
        add_later = AsyncMock(return_value=False)
        with (
            patch.object(bot, "safe_query_answer", AsyncMock()),
            patch.object(bot, "get_known_post", AsyncMock(return_value={"id": 42})),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={})),
            patch.object(bot, "add_read_later", add_later),
        ):
            await bot.button_handler(callback_update(later_data)[0], SimpleNamespace())
            await bot.button_handler(callback_update(later_data)[0], SimpleNamespace())
        add_later.assert_awaited_once()

    async def test_cancel_between_reserve_and_processing_blocks_toggle(self):
        data = bot.store_user_one_shot_payload("sub_toggle", "tag", 1)
        entered = asyncio.Event()
        release = asyncio.Event()
        original = bot.begin_one_shot_processing

        async def delayed_processing(user_id, callback_data):
            entered.set()
            await release.wait()
            return await original(user_id, callback_data)

        toggle = AsyncMock()
        with (
            patch.object(bot, "safe_query_answer", AsyncMock()),
            patch.object(bot, "begin_one_shot_processing", side_effect=delayed_processing),
            patch.object(bot, "toggle_subscription", toggle),
        ):
            task = asyncio.create_task(
                bot.button_handler(callback_update(data)[0], SimpleNamespace())
            )
            await entered.wait()
            await bot.invalidate_user_flow(1)
            release.set()
            await task
        toggle.assert_not_awaited()
        self.assertEqual(bot.issued_one_shot_callbacks[data].status, "stale")

    async def test_cancel_between_reserve_and_processing_blocks_delete(self):
        data = bot.store_user_one_shot_payload("sub_remove_do", "tag", 1)
        entered = asyncio.Event()
        release = asyncio.Event()
        original = bot.begin_one_shot_processing

        async def delayed_processing(user_id, callback_data):
            entered.set()
            await release.wait()
            return await original(user_id, callback_data)

        remove = AsyncMock()
        with (
            patch.object(bot, "safe_query_answer", AsyncMock()),
            patch.object(bot, "begin_one_shot_processing", side_effect=delayed_processing),
            patch.object(bot, "remove_subscription", remove),
        ):
            task = asyncio.create_task(
                bot.button_handler(callback_update(data)[0], SimpleNamespace())
            )
            await entered.wait()
            await bot.invalidate_user_flow(1)
            release.set()
            await task
        remove.assert_not_awaited()

    async def test_cancel_after_processing_does_not_allow_second_toggle(self):
        data = bot.store_user_one_shot_payload("sub_toggle", "tag", 1)
        started = asyncio.Event()
        release = asyncio.Event()

        async def delayed_toggle(*_args):
            self.assertEqual(bot.issued_one_shot_callbacks[data].status, "processing")
            started.set()
            await release.wait()
            return SimpleNamespace(status="ok", is_active=False)

        toggle = AsyncMock(side_effect=delayed_toggle)
        with (
            patch.object(bot, "safe_query_answer", AsyncMock()),
            patch.object(bot, "toggle_subscription", toggle),
            patch.object(bot, "get_user_subscriptions_keyboard", AsyncMock(return_value=None)),
        ):
            first = asyncio.create_task(
                bot.button_handler(callback_update(data)[0], SimpleNamespace())
            )
            await started.wait()
            await bot.invalidate_user_flow(1)
            await bot.button_handler(callback_update(data)[0], SimpleNamespace())
            release.set()
            await first
        toggle.assert_awaited_once()
        self.assertEqual(bot.issued_one_shot_callbacks[data].status, "consumed")


if __name__ == "__main__":
    unittest.main()
