import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


def make_callback_update(data: str, user_id: int = 1):
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(reply_text=AsyncMock()),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(callback_query=query), query


class FavoritesFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.recent_posts.clear()

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


if __name__ == "__main__":
    unittest.main()
