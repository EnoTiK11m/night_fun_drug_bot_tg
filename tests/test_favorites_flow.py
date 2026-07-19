import os
import shutil
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import bot
import bot_state


def make_callback_update(data: str, user_id: int = 1):
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(reply_text=AsyncMock(), edit_text=AsyncMock()),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id, type="private"),
        effective_message=query.message,
    )
    return update, query


class FavoritesFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = f"test_payloads_{uuid.uuid4().hex}"
        os.makedirs(self.tempdir)
        self.old_payload_db_path = bot_state.DB_PATH
        bot_state.DB_PATH = os.path.join(self.tempdir, "callbacks.db")
        bot_state.callback_payloads.clear()
        bot.recent_posts.clear()

    def tearDown(self):
        bot_state.callback_payloads.clear()
        bot_state.DB_PATH = self.old_payload_db_path
        shutil.rmtree(self.tempdir, ignore_errors=True)
        bot.user_states.pop(1, None)
        bot.search_builders.pop(1, None)
        bot.pending_preset_queries.pop(1, None)
        bot.pending_bulk_posts.pop(1, None)
        bot.pending_subscription_options.pop(1, None)
        bot.favorites_export_users.clear()
        bot.favorites_export_last_finished_at.clear()

    async def test_favorite_button_uses_cached_post_without_api_lookup(self):
        post = {
            "id": "123",
            "file_url": "https://example.test/123.jpg",
            "tags": "tag",
            "rating": "s",
            "score": 10,
        }
        bot.remember_post(post)
        update, query = make_callback_update("fav_123")

        with (
            patch.object(bot.api, "get_post_by_id", AsyncMock()) as get_post_by_id,
            patch.object(bot, "get_cached_post", AsyncMock()) as get_cached_post,
            patch.object(bot, "add_favorite", AsyncMock(return_value=True)) as add_favorite,
        ):
            await bot.button_handler(update, SimpleNamespace())

        get_post_by_id.assert_not_awaited()
        get_cached_post.assert_not_awaited()
        add_favorite.assert_awaited_once()
        saved_post = add_favorite.await_args.args[1]
        self.assertEqual(saved_post["id"], "123")
        self.assertEqual(saved_post["file_url"], "https://example.test/123.jpg")
        query.answer.assert_awaited_once()

    async def test_start_message_describes_current_features(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )

        with (
            patch.object(bot, "get_subscription_pause_until", AsyncMock(return_value=None)),
            patch.object(
                bot,
                "get_user_settings",
                AsyncMock(return_value={"interface_mode": "simple"}),
            ),
        ):
            await bot.start(update, SimpleNamespace())

        first_call = update.message.reply_text.await_args_list[0]
        text = first_call.args[0]
        self.assertIn("18+", text)
        self.assertIs(
            first_call.kwargs["reply_markup"].is_persistent,
            True,
        )
        self.assertEqual(len(update.message.reply_text.await_args_list), 2)

    async def test_favorite_button_without_cache_saves_id_without_blocking_api(self):
        update, query = make_callback_update("fav_456")

        with (
            patch.object(bot.api, "get_post_by_id", AsyncMock()) as get_post_by_id,
            patch.object(bot, "get_cached_post", AsyncMock(return_value=None)),
            patch.object(bot, "add_favorite", AsyncMock(return_value=True)) as add_favorite,
        ):
            await bot.button_handler(update, SimpleNamespace())

        get_post_by_id.assert_not_awaited()
        add_favorite.assert_awaited_once()
        saved_post = add_favorite.await_args.args[1]
        self.assertEqual(saved_post["id"], 456)
        self.assertEqual(saved_post["file_url"], "")
        query.answer.assert_awaited_once()

    async def test_favorite_button_uses_persistent_post_cache_after_memory_cache_miss(self):
        cached_post = {
            "id": 789,
            "file_url": "https://example.test/cached.jpg",
            "sample_url": "https://example.test/cached-sample.jpg",
        }
        update, _query = make_callback_update("fav_789")

        with (
            patch.object(bot.api, "get_post_by_id", AsyncMock()) as get_post_by_id,
            patch.object(bot, "get_cached_post", AsyncMock(return_value=cached_post)),
            patch.object(bot, "add_favorite", AsyncMock(return_value=True)) as add_favorite,
        ):
            await bot.button_handler(update, SimpleNamespace())

        get_post_by_id.assert_not_awaited()
        add_favorite.assert_awaited_once_with(1, cached_post)

    async def test_subscribe_button_under_media_replies_instead_of_editing_text(self):
        data = bot.store_callback_payload("subscribe", "tag")
        update, query = make_callback_update(data)

        with patch.object(bot, "add_subscription", AsyncMock()) as add_subscription:
            await bot.button_handler(update, SimpleNamespace())

        add_subscription.assert_not_awaited()
        query.edit_message_text.assert_not_awaited()
        query.message.reply_text.assert_awaited_once()
        markup = query.message.reply_text.await_args.kwargs["reply_markup"]
        confirm_data = markup.inline_keyboard[0][0].callback_data

        confirm_update, confirm_query = make_callback_update(confirm_data)
        with (
            patch.object(bot, "add_subscription", AsyncMock(return_value=True)) as add_subscription,
            patch.object(bot, "get_subscription_pause_until", AsyncMock(return_value=None)),
            patch.object(bot, "count_subscription_digest", AsyncMock(return_value=0)),
        ):
            await bot.button_handler(confirm_update, SimpleNamespace())

        add_subscription.assert_awaited_once_with(1, "tag", 10)
        confirm_query.edit_message_text.assert_awaited_once()

    async def test_post_tags_button_replies_with_full_tags(self):
        post = {"id": "123", "tags": "alpha beta gamma"}
        update, query = make_callback_update("post_tags_123")

        with (
            patch.object(bot, "get_known_post", AsyncMock(return_value=post)),
            patch.object(
                bot.tag_translation_service,
                "translate_tags",
                AsyncMock(return_value={"alpha": "альфа", "beta": "бета"}),
            ) as translate_tags,
        ):
            await bot.button_handler(update, SimpleNamespace())

        translate_tags.assert_awaited_once_with(["alpha", "beta", "gamma"])
        query.message.reply_text.assert_awaited_once()
        text = query.message.reply_text.await_args.args[0]
        self.assertIn("• `alpha` — альфа", text)
        self.assertIn("• `beta` — бета", text)

    async def test_post_tags_actions_are_paginated_for_every_tag(self):
        tags = [f"tag_{index}" for index in range(18)]
        post = {"id": 123, "tags": " ".join(tags)}
        update, query = make_callback_update("post_tags_123")

        with (
            patch.object(bot, "get_known_post", AsyncMock(return_value=post)),
            patch.object(
                bot.tag_translation_service,
                "translate_tags",
                AsyncMock(return_value={}),
            ),
        ):
            await bot.button_handler(update, SimpleNamespace())

        first_text = query.message.reply_text.await_args.args[0]
        first_markup = query.message.reply_text.await_args.kwargs["reply_markup"]
        first_callbacks = [
            button.callback_data
            for row in first_markup.inline_keyboard
            for button in row
        ]
        self.assertIn("Страница 1/3", first_text)
        self.assertIn("`tag_7`", first_text)
        self.assertNotIn("`tag_8`", first_text)
        self.assertIn("post_tags_page_123_1", first_callbacks)
        self.assertEqual(len(first_markup.inline_keyboard), 9)

        update, query = make_callback_update("post_tags_page_123_1")
        with (
            patch.object(bot, "get_known_post", AsyncMock(return_value=post)),
            patch.object(
                bot.tag_translation_service,
                "translate_tags",
                AsyncMock(return_value={}),
            ) as translate_tags,
        ):
            await bot.button_handler(update, SimpleNamespace())

        translate_tags.assert_awaited_once_with(tags[8:16])
        second_text = query.message.edit_text.await_args.args[0]
        second_markup = query.message.edit_text.await_args.kwargs["reply_markup"]
        second_callbacks = [
            button.callback_data
            for row in second_markup.inline_keyboard
            for button in row
        ]
        self.assertIn("Страница 2/3", second_text)
        self.assertIn("`tag_8`", second_text)
        self.assertIn("`tag_15`", second_text)
        self.assertNotIn("`tag_16`", second_text)
        self.assertIn("post_tags_page_123_0", second_callbacks)
        self.assertIn("post_tags_page_123_2", second_callbacks)

        update, query = make_callback_update("post_tags_page_123_2")
        with (
            patch.object(bot, "get_known_post", AsyncMock(return_value=post)),
            patch.object(
                bot.tag_translation_service,
                "translate_tags",
                AsyncMock(return_value={}),
            ) as translate_tags,
        ):
            await bot.button_handler(update, SimpleNamespace())

        translate_tags.assert_awaited_once_with(tags[16:18])
        last_text = query.message.edit_text.await_args.args[0]
        last_markup = query.message.edit_text.await_args.kwargs["reply_markup"]
        last_callbacks = [
            button.callback_data
            for row in last_markup.inline_keyboard
            for button in row
        ]
        self.assertIn("Страница 3/3", last_text)
        self.assertIn("`tag_16`", last_text)
        self.assertIn("`tag_17`", last_text)
        self.assertEqual(len(last_markup.inline_keyboard), 3)
        self.assertIn("post_tags_page_123_1", last_callbacks)
        self.assertNotIn("post_tags_page_123_3", last_callbacks)

    async def test_blacklist_view_shows_english_and_russian_tags(self):
        update, query = make_callback_update("bl_show")
        entries = [
            {"tag": "blue_hair", "expires_at": None, "source": "manual"},
            {"tag": "gore", "expires_at": "2030-01-01", "source": "temporary"},
        ]

        with (
            patch.object(bot, "get_blacklist_entries", AsyncMock(return_value=entries)),
            patch.object(
                bot.tag_translation_service,
                "translate_tags",
                AsyncMock(return_value={
                    "blue_hair": "голубые волосы",
                    "gore": "жестокий контент",
                }),
            ) as translate_tags,
        ):
            await bot.button_handler(update, SimpleNamespace())

        translate_tags.assert_awaited_once_with(
            ["blue_hair", "gore"], immediate_limit=50
        )
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("`blue_hair` — голубые волосы", text)
        self.assertIn("`gore` — жестокий контент", text)

    def test_tags_button_can_be_hidden_from_image_keyboard(self):
        visible_keyboard = bot.get_post_more_keyboard(123, show_tags_button=True)
        hidden_keyboard = bot.get_post_more_keyboard(123, show_tags_button=False)

        visible_callbacks = [
            button.callback_data
            for row in visible_keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        hidden_callbacks = [
            button.callback_data
            for row in hidden_keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]

        self.assertIn("post_tags_123", visible_callbacks)
        self.assertNotIn("post_tags_123", hidden_callbacks)

    def test_image_keyboard_keeps_subscribe_and_post_actions(self):
        keyboard = bot.get_image_keyboard(123, query="tag", show_tags_button=True)
        rows = keyboard.inline_keyboard

        callbacks = [
            button.callback_data
            for row in rows for button in row if button.callback_data
        ]
        self.assertIn("similar_123", callbacks)
        self.assertIn("later_add_123", callbacks)
        self.assertIn("post_more_123", callbacks)
        self.assertTrue(any(data.startswith("subscribe_") for data in callbacks))
        self.assertNotIn("post_tags_123", callbacks)

        expanded = bot.get_post_more_keyboard(123)
        expanded_callbacks = [
            button.callback_data
            for row in expanded.inline_keyboard
            for button in row if button.callback_data
        ]
        self.assertIn("post_tags_123", expanded_callbacks)
        self.assertIn("post_original_123", expanded_callbacks)
        self.assertTrue(any("id=123" in (button.url or "") for row in expanded.inline_keyboard for button in row))

    def test_persistent_keyboard_contains_only_primary_actions(self):
        keyboard = bot.get_persistent_keyboard("simple")
        labels = [button.text for row in keyboard.keyboard for button in row]

        self.assertEqual(len(labels), 4)
        self.assertIn(bot.PERSISTENT_SEARCH, labels)
        self.assertIn(bot.PERSISTENT_RANDOM, labels)
        self.assertIn(bot.PERSISTENT_FAVORITES, labels)
        self.assertIn(bot.PERSISTENT_MENU, labels)
        self.assertTrue(keyboard.resize_keyboard)
        self.assertTrue(keyboard.is_persistent)

        advanced_labels = [
            button.text
            for row in bot.get_persistent_keyboard("advanced").keyboard
            for button in row
        ]
        self.assertIn(bot.PERSISTENT_GALLERY, advanced_labels)
        self.assertIn(bot.PERSISTENT_SUBSCRIPTIONS, advanced_labels)

    def test_main_menu_exposes_new_user_features(self):
        callbacks = {
            button.callback_data
            for row in bot.get_main_keyboard("advanced").inline_keyboard
            for button in row if button.callback_data
        }

        self.assertTrue(
            {
                "search_hub",
                "gallery",
                "library",
                "subscriptions",
                "blacklist",
                "settings",
                "my_data",
                "help",
            }.issubset(callbacks)
        )
        search_callbacks = {
            button.callback_data for row in bot.get_search_hub_keyboard().inline_keyboard
            for button in row if button.callback_data
        }
        library_callbacks = {
            button.callback_data for row in bot.get_library_keyboard().inline_keyboard
            for button in row if button.callback_data
        }
        self.assertTrue(
            {"search", "random", "gallery", "search_builder", "presets"}.issubset(
                search_callbacks
            )
        )
        self.assertTrue(
            {"recommendations", "later_list", "fav_gallery"}.issubset(
                library_callbacks
            )
        )

    async def test_help_describes_current_navigation_and_cancel_command(self):
        update, query = make_callback_update("help")
        with patch.object(
            bot,
            "get_user_settings",
            AsyncMock(return_value={"interface_mode": "advanced"}),
        ):
            await bot.button_handler(update, SimpleNamespace())

        text = query.edit_message_text.await_args.args[0]
        self.assertIn("/cancel", text)
        self.assertIn("Мои данные", text)
        self.assertIn("расширенном режиме", text)
        callbacks = {
            button.callback_data
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        }
        self.assertIn("my_data", callbacks)
        self.assertIn("back", callbacks)

    async def test_post_more_opens_secondary_actions_without_replacing_media(self):
        update, query = make_callback_update("post_more_123")
        query.edit_message_reply_markup = AsyncMock()

        with patch.object(
            bot, "get_user_settings", AsyncMock(return_value={"show_tags_button": True})
        ):
            await bot.button_handler(update, SimpleNamespace())

        query.edit_message_reply_markup.assert_awaited_once()
        markup = query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data for row in markup.inline_keyboard
            for button in row if button.callback_data
        ]
        self.assertIn("post_tags_123", callbacks)
        self.assertIn("post_original_123", callbacks)
        query.edit_message_text.assert_not_awaited()

    def test_spoiler_mode_respects_rating(self):
        self.assertTrue(bot.should_spoiler({"spoiler_mode": "all"}, {"rating": "s"}))
        self.assertTrue(bot.should_spoiler({"spoiler_mode": "explicit"}, {"rating": "e"}))
        self.assertFalse(bot.should_spoiler({"spoiler_mode": "explicit"}, {"rating": "s"}))
        self.assertFalse(bot.should_spoiler({"spoiler_mode": "off"}, {"rating": "e"}))

    async def test_search_builder_creates_runnable_query(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            message=SimpleNamespace(text="ellen_joe solo", reply_text=AsyncMock()),
        )
        bot.user_states[1] = "waiting_builder_include"

        await bot.message_handler(update, SimpleNamespace())
        self.assertEqual(bot.user_states[1], "waiting_builder_exclude")

        update.message.text = "comic text"
        await bot.message_handler(update, SimpleNamespace())

        self.assertNotIn(1, bot.user_states)
        text = update.message.reply_text.await_args_list[-1].args[0]
        self.assertIn("ellen_joe solo -comic -text", text)
        markup = update.message.reply_text.await_args_list[-1].kwargs["reply_markup"]
        self.assertEqual(len(markup.inline_keyboard[0]), 2)

    async def test_persistent_menu_button_opens_advanced_inline_menu(self):
        bot.user_states[1] = "waiting_search"
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            message=SimpleNamespace(text=bot.PERSISTENT_MENU, reply_text=AsyncMock()),
        )

        with (
            patch.object(bot, "build_main_menu_text", AsyncMock(return_value="menu")),
            patch.object(
                bot,
                "get_user_settings",
                AsyncMock(return_value={"interface_mode": "advanced"}),
            ),
        ):
            await bot.message_handler(update, SimpleNamespace())

        self.assertNotIn(1, bot.user_states)
        markup = update.message.reply_text.await_args.kwargs["reply_markup"]
        self.assertIsInstance(markup, bot.InlineKeyboardMarkup)

    def test_subscription_keyboard_uses_post_id_callback(self):
        keyboard = bot.get_subscription_image_keyboard(123, "tag")
        callback_data = keyboard.inline_keyboard[0][0].callback_data

        self.assertEqual(callback_data, "sub_fav_123")

    async def test_subscription_favorite_uses_cache_queries_from_database(self):
        post = {"id": 123, "file_url": "https://example.test/123.jpg"}
        update, query = make_callback_update("sub_fav_123")

        with (
            patch.object(bot, "get_known_post", AsyncMock(return_value=post)),
            patch.object(bot, "add_favorite", AsyncMock(return_value=True)) as add_favorite,
            patch.object(
                bot,
                "get_subscription_queries_for_post",
                AsyncMock(return_value=["tag-a", "tag-b"]),
            ) as get_queries,
            patch.object(bot, "add_subscription_post", AsyncMock(return_value=True)) as add_sub_post,
        ):
            await bot.button_handler(update, SimpleNamespace())

        add_favorite.assert_awaited_once_with(1, post)
        get_queries.assert_awaited_once_with(1, 123)
        self.assertEqual(add_sub_post.await_count, 2)
        add_sub_post.assert_any_await(1, "tag-a", post)
        add_sub_post.assert_any_await(1, "tag-b", post)
        query.message.reply_text.assert_awaited_once()

    async def test_favorites_menu_shows_gallery_list_and_find(self):
        message = SimpleNamespace(edit_text=AsyncMock(), reply_text=AsyncMock())

        with patch.object(bot, "count_favorites", AsyncMock(return_value=42)):
            await bot.show_favorites(message, 1, edit=True)

        message.edit_text.assert_awaited_once()
        text = message.edit_text.await_args.args[0]
        keyboard = message.edit_text.await_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("42", text)
        self.assertIn("fav_gallery", callbacks)
        self.assertIn("fav_list", callbacks)
        self.assertIn("fav_find", callbacks)
        self.assertIn("fav_export", callbacks)
        self.assertIn("fav_collections", callbacks)

    async def test_stats_button_sends_activity_summary(self):
        update, query = make_callback_update("stats")
        stats = {
            "viewed_total": 12,
            "viewed_week": 3,
            "viewed_month": 8,
            "favorites_total": 5,
            "favorites_week": 1,
            "favorites_month": 4,
            "searches_total": 9,
            "searches_week": 2,
            "searches_month": 7,
            "subscriptions_active": 2,
            "top_queries": [("blue_hair", 4)],
            "top_tags": [("solo", 3)],
        }

        with patch.object(
            bot, "get_user_activity_stats", AsyncMock(return_value=stats)
        ) as get_stats:
            await bot.button_handler(update, SimpleNamespace())

        get_stats.assert_awaited_once_with(1)
        query.message.reply_text.assert_awaited_once()
        text = query.message.reply_text.await_args.args[0]
        self.assertIn("Ваша статистика", text)
        self.assertIn("Просмотрено: 12", text)
        self.assertIn("blue_hair", text)
        markup = query.message.reply_text.await_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]
        self.assertEqual(callbacks, ["stats_clear_confirm", "my_data"])
        self.assertEqual(
            query.message.reply_text.await_args.kwargs["parse_mode"], "Markdown"
        )

    async def test_collection_picker_callback_is_not_consumed_by_generic_favorite(self):
        update, _query = make_callback_update("fav_col_pick_123")

        with (
            patch.object(bot, "show_collection_picker", AsyncMock()) as picker,
            patch.object(bot, "add_favorite", AsyncMock()) as add_favorite,
        ):
            await bot.button_handler(update, SimpleNamespace())

        picker.assert_awaited_once_with(update.callback_query.message, 1, 123)
        add_favorite.assert_not_awaited()

    async def test_gallery_settings_cycle_persists_sort(self):
        update, query = make_callback_update("gallery_cycle_sort")
        settings = bot.normalize_feature_settings({"gallery_sort": "random"})

        with (
            patch.object(bot, "get_user_settings", AsyncMock(return_value=settings)),
            patch.object(bot, "save_user_settings", AsyncMock()) as save_settings,
        ):
            await bot.button_handler(update, SimpleNamespace())

        self.assertEqual(save_settings.await_args.args[1]["gallery_sort"], "new")
        query.edit_message_text.assert_awaited_once()

    async def test_search_gallery_uses_gif_preview_without_splitting_album(self):
        message = SimpleNamespace(
            reply_media_group=AsyncMock(),
            reply_text=AsyncMock(),
        )
        posts = [
            {
                "id": 10,
                "file_url": "https://example.test/10.gif",
                "sample_url": "https://example.test/10-sample.jpg",
            }
        ] + [
            {"id": post_id, "file_url": f"https://example.test/{post_id}.jpg"}
            for post_id in range(9, 0, -1)
        ]
        settings = bot.normalize_feature_settings(
            {"gallery_sort": "new", "gallery_size": 10, "quality_mode": "original"}
        )

        with (
            patch.object(bot, "get_user_settings", AsyncMock(return_value=settings)),
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=[])),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(bot.api, "search", AsyncMock(return_value=posts)),
            patch.object(bot, "mark_post_sent", AsyncMock()),
            patch.object(bot, "save_user_query", AsyncMock()),
            patch.object(bot, "store_callback_payload", return_value="token"),
            patch.object(bot, "remember_and_cache_post", AsyncMock()) as cache_post,
            patch.object(bot, "send_post_media", AsyncMock()) as send_single,
        ):
            delivered = await bot.send_search_gallery(message, 1, "test")

        self.assertTrue(delivered)
        message.reply_media_group.assert_awaited_once()
        media = message.reply_media_group.await_args.kwargs["media"]
        self.assertEqual(len(media), 10)
        self.assertEqual(media[0].media, "https://example.test/10-sample.jpg")
        self.assertEqual(cache_post.await_count, 10)
        cached_posts = [call.args[0] for call in cache_post.await_args_list]
        self.assertEqual({post["id"] for post in cached_posts}, set(range(1, 11)))
        self.assertEqual(cached_posts[0]["file_url"], "https://example.test/10.gif")
        send_single.assert_not_awaited()

    async def test_search_gallery_retries_failed_album_item_with_sample(self):
        message = SimpleNamespace(
            reply_media_group=AsyncMock(side_effect=[
                bot.BadRequest(
                    'Failed to send message #6 with the error message "webpage_curl_failed"'
                ),
                None,
            ]),
            reply_text=AsyncMock(),
        )
        posts = [
            {
                "id": post_id,
                "file_url": f"https://example.test/{post_id}-original.jpg",
                "sample_url": f"https://example.test/{post_id}-sample.jpg",
            }
            for post_id in range(10, 0, -1)
        ]
        settings = bot.normalize_feature_settings(
            {"gallery_sort": "new", "gallery_size": 10, "quality_mode": "original"}
        )

        with (
            patch.object(bot, "get_user_settings", AsyncMock(return_value=settings)),
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=[])),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(bot.api, "search", AsyncMock(return_value=posts)),
            patch.object(bot, "mark_post_sent", AsyncMock()) as mark_sent,
            patch.object(bot, "save_user_query", AsyncMock()),
            patch.object(bot, "store_callback_payload", return_value="token"),
            patch.object(bot, "remember_and_cache_post", AsyncMock()),
            patch.object(bot, "send_post_media", AsyncMock()) as send_single,
        ):
            delivered = await bot.send_search_gallery(message, 1, "test")

        self.assertTrue(delivered)
        self.assertEqual(message.reply_media_group.await_count, 2)
        first_media = message.reply_media_group.await_args_list[0].kwargs["media"]
        retried_media = message.reply_media_group.await_args_list[1].kwargs["media"]
        self.assertEqual(first_media[5].media, "https://example.test/5-original.jpg")
        self.assertEqual(retried_media[5].media, "https://example.test/5-sample.jpg")
        self.assertEqual(len(retried_media), 10)
        self.assertEqual(mark_sent.await_count, 10)
        send_single.assert_not_awaited()

    async def test_search_gallery_removes_only_failed_album_item_without_fallback(self):
        message = SimpleNamespace(
            reply_media_group=AsyncMock(side_effect=[
                bot.BadRequest(
                    'Failed to send message #5 with the error message "webpage_curl_failed"'
                ),
                None,
            ]),
            reply_text=AsyncMock(),
        )
        posts = [
            {"id": post_id, "file_url": f"https://example.test/{post_id}.jpg"}
            for post_id in range(10, 0, -1)
        ]
        settings = bot.normalize_feature_settings(
            {"gallery_sort": "new", "gallery_size": 10, "quality_mode": "original"}
        )

        with (
            patch.object(bot, "get_user_settings", AsyncMock(return_value=settings)),
            patch.object(bot, "get_user_blacklist", AsyncMock(return_value=[])),
            patch.object(bot, "get_sent_post_ids", AsyncMock(return_value=set())),
            patch.object(bot.api, "search", AsyncMock(return_value=posts)),
            patch.object(bot, "mark_post_sent", AsyncMock()) as mark_sent,
            patch.object(bot, "save_user_query", AsyncMock()),
            patch.object(bot, "store_callback_payload", return_value="token"),
            patch.object(bot, "remember_and_cache_post", AsyncMock()),
            patch.object(bot, "send_post_media", AsyncMock()) as send_single,
        ):
            delivered = await bot.send_search_gallery(message, 1, "test")

        self.assertTrue(delivered)
        self.assertEqual(message.reply_media_group.await_count, 2)
        retried_media = message.reply_media_group.await_args_list[1].kwargs["media"]
        self.assertEqual(len(retried_media), 9)
        self.assertNotIn(
            "https://example.test/6.jpg",
            [item.media for item in retried_media],
        )
        self.assertEqual(mark_sent.await_count, 9)
        send_single.assert_not_awaited()

    async def test_export_button_schedules_zip_export(self):
        update, _query = make_callback_update("fav_export")
        context = SimpleNamespace()
        export_job = object()

        with (
            patch.object(bot, "start_favorites_zip_export", new=lambda message, user_id: export_job),
            patch.object(bot, "schedule_background_task") as schedule_task,
        ):
            await bot.button_handler(update, context)

        schedule_task.assert_called_once_with(context, export_job)

    async def test_export_fetches_missing_original_url_by_id(self):
        fresh_post = {"id": 123, "file_url": "https://example.test/original.jpg"}

        with (
            patch.object(bot.api, "get_post_by_id", AsyncMock(return_value=fresh_post)) as get_post,
            patch.object(bot, "remember_and_cache_post", AsyncMock()) as cache_post,
        ):
            post = await bot.ensure_favorite_original_url({"id": 123, "file_url": ""})

        self.assertEqual(post, fresh_post)
        get_post.assert_awaited_once_with(123)
        cache_post.assert_awaited_once_with(fresh_post)

    def test_export_uses_only_static_original_images(self):
        self.assertTrue(bot.is_exportable_image_url("https://example.test/1.jpg"))
        self.assertTrue(bot.is_exportable_image_url("https://example.test/2.PNG?download=1"))
        self.assertFalse(bot.is_exportable_image_url("https://example.test/3.gif"))
        self.assertFalse(bot.is_exportable_image_url("https://example.test/4.mp4"))

    async def test_zip_export_sends_downloaded_static_images_and_skips_others(self):
        class FakeClientSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        message = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock())
        favorites = [
            {"id": 1, "file_url": "https://example.test/1.jpg"},
            {"id": 2, "file_url": "https://example.test/2.gif"},
            {"id": 3, "file_url": "https://example.test/3.png"},
        ]

        async def fake_download(_session, favorite):
            if favorite["id"] == 2:
                return None
            return f"{favorite['id']}.jpg", b"image-data"

        with (
            patch.object(bot, "count_favorites", AsyncMock(return_value=3)),
            patch.object(bot, "get_favorites", AsyncMock(return_value=favorites)),
            patch.object(bot, "download_original_favorite_image", AsyncMock(side_effect=fake_download)),
            patch.object(bot.aiohttp, "ClientSession", return_value=FakeClientSession()),
        ):
            await bot.send_favorites_zip_export(message, 1)

        message.reply_document.assert_awaited_once()
        final_text = message.reply_text.await_args_list[-1].args[0]
        self.assertIn("2", final_text)
        self.assertIn("1", final_text)

    async def test_zip_export_rejects_parallel_export_for_same_user(self):
        bot.favorites_export_users.add(1)
        message = SimpleNamespace(reply_text=AsyncMock())

        await bot.start_favorites_zip_export(message, 1)

        message.reply_text.assert_awaited_once_with("📦 Архив избранного уже собирается.")

    async def test_zip_export_uses_cooldown_after_success(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        bot.favorites_export_last_finished_at[1] = bot.time.monotonic()

        await bot.start_favorites_zip_export(message, 1)

        text = message.reply_text.await_args.args[0]
        self.assertIn("Экспорт уже недавно запускался", text)

    async def test_favorites_gallery_sends_ten_post_pages_as_new_albums(self):
        message = SimpleNamespace(
            reply_media_group=AsyncMock(),
            reply_text=AsyncMock(),
        )
        posts = [
            {
                "id": post_id,
                "file_url": f"https://example.test/{post_id}.jpg",
                "tags": "tag",
                "rating": "s",
                "score": 1,
            }
            for post_id in range(1, 11)
        ]

        with (
            patch.object(bot, "count_favorites", AsyncMock(return_value=23)),
            patch.object(bot, "get_favorites", AsyncMock(return_value=posts)) as get_favorites,
            patch.object(bot, "get_user_settings", AsyncMock(return_value={"show_tags_button": True})),
            patch.object(bot, "send_post_media", AsyncMock()) as send_post_media,
        ):
            delivered = await bot.send_favorites_gallery(message, 1, page=0)

        self.assertTrue(delivered)
        get_favorites.assert_awaited_once_with(1, limit=10, offset=0)
        message.reply_media_group.assert_awaited_once()
        media = message.reply_media_group.await_args.kwargs["media"]
        self.assertEqual(len(media), 10)
        self.assertIn("страница 1/3", media[0].caption)
        navigation = message.reply_text.await_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data
            for row in navigation.inline_keyboard
            for button in row
        ]
        self.assertIn("fav_page_1", callbacks)
        self.assertNotIn("fav_page_-1", callbacks)
        send_post_media.assert_not_awaited()

    async def test_favorites_gallery_next_page_sends_new_album(self):
        update, query = make_callback_update("fav_page_2")

        with patch.object(
            bot, "send_favorites_gallery", AsyncMock(return_value=True)
        ) as send_gallery:
            await bot.button_handler(update, SimpleNamespace())

        send_gallery.assert_awaited_once_with(query.message, 1, 2)
        query.edit_message_text.assert_not_awaited()

    async def test_favorites_gallery_keeps_gif_without_preview_as_single_item(self):
        message = SimpleNamespace(
            reply_media_group=AsyncMock(),
            reply_text=AsyncMock(),
        )
        posts = [
            {"id": 1, "file_url": "https://example.test/1.gif"},
            *[
                {"id": post_id, "file_url": f"https://example.test/{post_id}.jpg"}
                for post_id in range(2, 11)
            ],
        ]

        with (
            patch.object(bot, "count_favorites", AsyncMock(return_value=10)),
            patch.object(bot, "get_favorites", AsyncMock(return_value=posts)),
            patch.object(bot, "get_user_settings", AsyncMock(return_value={})),
            patch.object(bot, "send_post_media", AsyncMock(return_value=True)) as send_single,
        ):
            delivered = await bot.send_favorites_gallery(message, 1)

        self.assertTrue(delivered)
        album = message.reply_media_group.await_args.kwargs["media"]
        self.assertEqual(len(album), 9)
        send_single.assert_awaited_once()
        self.assertEqual(send_single.await_args.args[1]["id"], 1)
        self.assertIn("Показано: 10", message.reply_text.await_args.args[0])

    async def test_pause_and_resume_subscription_settings_flow(self):
        update, query = make_callback_update("settings_pause_subscriptions")
        bot.user_states.pop(1, None)

        await bot.button_handler(update, SimpleNamespace())

        self.assertEqual(bot.user_states[1], "waiting_pause_subscriptions")
        query.edit_message_text.assert_awaited_once()

        update, query = make_callback_update("settings_resume_subscriptions")
        with (
            patch.object(
                bot,
                "resume_all_active_subscriptions",
                AsyncMock(return_value=3),
            ) as resume_subscriptions,
            patch.object(bot, "get_subscription_pause_until", AsyncMock(return_value=None)),
            patch.object(bot, "count_subscription_digest", AsyncMock(return_value=0)),
        ):
            await bot.button_handler(update, SimpleNamespace())

        resume_subscriptions.assert_awaited_once_with(1)
        query.edit_message_text.assert_awaited_once()
        self.assertIn("3", query.edit_message_text.await_args.args[0])

    def test_subscriptions_keyboard_shows_only_relevant_pause_action(self):
        active_callbacks = {
            button.callback_data
            for row in bot.get_subscriptions_keyboard(subscriptions_paused=False).inline_keyboard
            for button in row
        }
        paused_callbacks = {
            button.callback_data
            for row in bot.get_subscriptions_keyboard(subscriptions_paused=True).inline_keyboard
            for button in row
        }

        self.assertIn("settings_pause_subscriptions", active_callbacks)
        self.assertNotIn("settings_resume_subscriptions", active_callbacks)
        self.assertIn("settings_resume_subscriptions", paused_callbacks)
        self.assertNotIn("settings_pause_subscriptions", paused_callbacks)

    def test_subscriptions_keyboard_shows_digest_action_only_when_queue_has_posts(self):
        empty_callbacks = {
            button.callback_data
            for row in bot.get_subscriptions_keyboard(has_digest_posts=False).inline_keyboard
            for button in row
        }
        queued_callbacks = {
            button.callback_data
            for row in bot.get_subscriptions_keyboard(has_digest_posts=True).inline_keyboard
            for button in row
        }

        self.assertNotIn("sub_digest_send", empty_callbacks)
        self.assertIn("sub_digest_send", queued_callbacks)

    async def test_empty_digest_button_reports_that_queue_is_empty(self):
        update, query = make_callback_update("sub_digest_send")

        with patch.object(bot, "pop_subscription_digest", AsyncMock(return_value=[])):
            await bot.button_handler(update, SimpleNamespace())

        query.message.reply_text.assert_awaited_once_with("📨 Дайджест пока пуст.")

    def test_settings_keyboard_shows_current_values_and_interface_mode(self):
        keyboard = bot.get_settings_keyboard({
            "show_caption": False,
            "spoiler_mode": "explicit",
            "gallery_size": 6,
            "quality_mode": "sample",
            "interface_mode": "advanced",
        })
        labels = [button.text for row in keyboard.inline_keyboard for button in row]

        self.assertTrue(any("6" in label for label in labels))
        self.assertTrue(any("sample" in label for label in labels))
        self.assertTrue(any("расширенный" in label for label in labels))

    async def test_cancel_input_clears_state_and_returns_to_main_menu(self):
        bot.user_states[1] = "waiting_search"
        update, query = make_callback_update("cancel_input")

        with (
            patch.object(
                bot,
                "get_user_settings",
                AsyncMock(return_value={"interface_mode": "simple"}),
            ),
            patch.object(
                bot,
                "get_subscription_pause_until",
                AsyncMock(return_value=None),
            ),
        ):
            await bot.button_handler(update, SimpleNamespace())

        self.assertNotIn(1, bot.user_states)
        query.edit_message_text.assert_awaited_once()
        self.assertIn("отменено", query.edit_message_text.await_args.args[0].lower())

    async def test_settings_reset_requires_confirmation(self):
        update, query = make_callback_update("settings_reset")
        with patch.object(bot, "save_user_settings", AsyncMock()) as save_settings:
            await bot.button_handler(update, SimpleNamespace())

        save_settings.assert_not_awaited()
        callbacks = [
            button.callback_data
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertEqual(callbacks, ["settings_reset_do", "settings"])

    async def test_favorite_tag_prompt_opens_filtered_list(self):
        bot.user_states[1] = "waiting_fav_tag"
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            message=SimpleNamespace(text="solo", reply_text=AsyncMock()),
        )

        with patch.object(bot, "show_favorite_search_results", AsyncMock()) as show_list:
            await bot.message_handler(update, SimpleNamespace())

        show_list.assert_awaited_once_with(update.message, 1, "solo")
        self.assertNotIn(1, bot.user_states)

    async def test_restart_command_rejects_non_admin(self):
        old_restart_requested = bot.restart_requested
        bot.restart_requested = False
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        context = SimpleNamespace(application=SimpleNamespace(stop_running=Mock()))

        try:
            with patch.object(bot, "ADMIN_USER_IDS", {2}):
                await bot.restart_command(update, context)
        finally:
            bot.restart_requested = old_restart_requested

        update.message.reply_text.assert_awaited_once_with("❌ Недостаточно прав.")
        context.application.stop_running.assert_not_called()

    async def test_health_command_sends_markdown_safe_status(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )

        with (
            patch.object(bot, "ADMIN_USER_IDS", {1}),
            patch.object(
                bot,
                "get_admin_database_stats",
                AsyncMock(return_value={"quick_check": "ok", "counts": {}}),
            ),
            patch.object(bot.api, "search", AsyncMock(return_value=[])),
            patch.object(
                bot.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=10 * 1024 * 1024),
            ),
            patch.object(bot.os.path, "getsize", return_value=1024),
        ):
            await bot.health_command(update, SimpleNamespace())

        update.message.reply_text.assert_awaited_once()
        text = update.message.reply_text.await_args.args[0]
        self.assertIn("DB quick check: `ok`", text)
        self.assertNotIn("DB quick_check", text)
        self.assertIn("Tag translation worker:", text)
        self.assertEqual(
            update.message.reply_text.await_args.kwargs["parse_mode"], "Markdown"
        )

    async def test_restart_command_stops_application_for_admin(self):
        old_restart_requested = bot.restart_requested
        bot.restart_requested = False
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        context = SimpleNamespace(application=SimpleNamespace(stop_running=Mock()))

        try:
            with patch.object(bot, "ADMIN_USER_IDS", {1}):
                await bot.restart_command(update, context)
                self.assertTrue(bot.restart_requested)
        finally:
            bot.restart_requested = old_restart_requested

        update.message.reply_text.assert_awaited_once_with("♻️ Перезапускаюсь...")
        context.application.stop_running.assert_called_once()

    async def test_restart_text_stops_application_for_admin(self):
        old_restart_requested = bot.restart_requested
        bot.restart_requested = False
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            message=SimpleNamespace(text="рестарт", reply_text=AsyncMock()),
        )
        context = SimpleNamespace(application=SimpleNamespace(stop_running=Mock()))

        try:
            with patch.object(bot, "ADMIN_USER_IDS", {1}):
                await bot.message_handler(update, context)
                self.assertTrue(bot.restart_requested)
        finally:
            bot.restart_requested = old_restart_requested

        update.message.reply_text.assert_awaited_once_with("♻️ Перезапускаюсь...")
        context.application.stop_running.assert_called_once()

    def test_access_allows_private_users_by_default(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=10),
            effective_chat=SimpleNamespace(id=10, type="private"),
        )

        with (
            patch.object(bot, "ADMIN_USER_IDS", set()),
            patch.object(bot, "ALLOWED_USER_IDS", set()),
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
            patch.object(bot, "ALLOW_GROUP_CHATS", False),
        ):
            self.assertTrue(bot.is_access_allowed(update))

    def test_access_blocks_unlisted_group_by_default(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=10),
            effective_chat=SimpleNamespace(id=-100, type="supergroup"),
        )

        with (
            patch.object(bot, "ADMIN_USER_IDS", set()),
            patch.object(bot, "ALLOWED_USER_IDS", set()),
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
            patch.object(bot, "ALLOW_GROUP_CHATS", False),
        ):
            self.assertFalse(bot.is_access_allowed(update))

    def test_access_allows_configured_chat(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=10),
            effective_chat=SimpleNamespace(id=-100, type="supergroup"),
        )

        with (
            patch.object(bot, "ADMIN_USER_IDS", set()),
            patch.object(bot, "ALLOWED_USER_IDS", set()),
            patch.object(bot, "ALLOWED_CHAT_IDS", {-100}),
            patch.object(bot, "ALLOW_GROUP_CHATS", False),
        ):
            self.assertTrue(bot.is_access_allowed(update))


class CallbackPayloadStateTests(unittest.TestCase):
    def test_callback_payload_survives_memory_cache_clear(self):
        tempdir = f"test_payloads_{uuid.uuid4().hex}"
        os.makedirs(tempdir)
        old_db_path = bot_state.DB_PATH
        bot_state.DB_PATH = os.path.join(tempdir, "callbacks.db")
        bot_state.callback_payloads.clear()
        try:
            data = bot_state.store_callback_payload("hist", "wide payload")
            bot_state.callback_payloads.clear()

            self.assertEqual(
                bot_state.get_callback_payload("hist", data),
                "wide payload",
            )
        finally:
            bot_state.callback_payloads.clear()
            bot_state.DB_PATH = old_db_path
            shutil.rmtree(tempdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
