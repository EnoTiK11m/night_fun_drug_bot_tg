import os
import shutil
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    return SimpleNamespace(callback_query=query), query


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

        with patch.object(bot, "add_subscription", AsyncMock(return_value=True)) as add_subscription:
            await bot.button_handler(update, SimpleNamespace())

        add_subscription.assert_awaited_once_with(1, "tag", 10)
        query.edit_message_text.assert_not_awaited()
        query.message.reply_text.assert_awaited_once()

    async def test_post_tags_button_replies_with_full_tags(self):
        post = {"id": "123", "tags": "alpha beta gamma"}
        update, query = make_callback_update("post_tags_123")

        with patch.object(bot, "get_known_post", AsyncMock(return_value=post)):
            await bot.button_handler(update, SimpleNamespace())

        query.message.reply_text.assert_awaited_once()
        text = query.message.reply_text.await_args.args[0]
        self.assertIn("• `alpha`", text)
        self.assertIn("• `beta`", text)

    def test_tags_button_can_be_hidden_from_image_keyboard(self):
        visible_keyboard = bot.get_image_keyboard(123, show_tags_button=True)
        hidden_keyboard = bot.get_image_keyboard(123, show_tags_button=False)

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

        self.assertEqual(rows[0][0].text, "🔔 Подписаться")
        self.assertEqual(rows[1][0].callback_data, "more")
        self.assertEqual(rows[1][1].callback_data, "search")
        self.assertEqual(rows[2][0].callback_data, "fav_123")
        self.assertEqual(rows[3][0].callback_data, "post_tags_123")
        self.assertIn("id=123", rows[4][0].url)

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

    async def test_favorites_gallery_loads_single_post_by_index(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        post = {
            "id": 20,
            "file_url": "https://example.test/20.jpg",
            "tags": "tag",
            "rating": "s",
            "score": 1,
        }

        with (
            patch.object(bot, "count_favorites", AsyncMock(return_value=3)),
            patch.object(bot, "get_favorite_by_index", AsyncMock(return_value=post)) as get_by_index,
            patch.object(bot, "get_user_settings", AsyncMock(return_value={"show_tags_button": True})),
            patch.object(bot, "send_post_media", AsyncMock()) as send_post_media,
            patch.object(bot, "get_favorites", AsyncMock()) as get_favorites,
        ):
            await bot.send_favorites_gallery(message, 1, index=99)

        get_by_index.assert_awaited_once_with(1, 2)
        get_favorites.assert_not_awaited()
        send_post_media.assert_awaited_once()

    async def test_pause_and_resume_subscription_settings_flow(self):
        update, query = make_callback_update("settings_pause_subscriptions")
        bot.user_states.pop(1, None)

        await bot.button_handler(update, SimpleNamespace())

        self.assertEqual(bot.user_states[1], "waiting_pause_subscriptions")
        query.edit_message_text.assert_awaited_once()

        update, query = make_callback_update("settings_resume_subscriptions")
        with patch.object(
            bot,
            "resume_all_active_subscriptions",
            AsyncMock(return_value=3),
        ) as resume_subscriptions:
            await bot.button_handler(update, SimpleNamespace())

        resume_subscriptions.assert_awaited_once_with(1)
        query.edit_message_text.assert_awaited_once()
        self.assertIn("3", query.edit_message_text.await_args.args[0])

    async def test_favorite_tag_prompt_opens_filtered_list(self):
        bot.user_states[1] = "waiting_fav_tag"
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            message=SimpleNamespace(text="solo", reply_text=AsyncMock()),
        )

        with patch.object(bot, "show_favorites_list", AsyncMock()) as show_list:
            await bot.message_handler(update, SimpleNamespace())

        show_list.assert_awaited_once_with(
            update.message,
            1,
            edit=False,
            page=0,
            tag_filter="solo",
        )
        self.assertNotIn(1, bot.user_states)


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
