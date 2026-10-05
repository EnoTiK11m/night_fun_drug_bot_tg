from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import app.telegram.application as bot
import app.telegram.state as bot_state
import app.storage.database as database


def post(post_id=1, **overrides):
    value = {
        "id": post_id,
        "file_url": f"https://example.test/{post_id}.jpg",
        "sample_url": "https://example.test/sample.jpg",
        "preview_url": "https://example.test/preview.jpg",
        "rating": "s",
        "width": 1920,
        "height": 1080,
        "tags": "fixture",
    }
    value.update(overrides)
    return value


class SearchFilterInvariantTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_rejected(self, mode, settings, rejected):
        message = SimpleNamespace(
            reply_text=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock()))
        )
        delivery = AsyncMock(return_value=True)
        with ExitStack() as stack:
            replacements = {
                "callback_issuer_for": Mock(return_value=Mock()),
                "get_user_blacklist": AsyncMock(return_value=set()),
                "get_user_settings": AsyncMock(return_value=settings),
                "reserve_search_cooldown": AsyncMock(return_value=False),
                "get_sent_post_ids": AsyncMock(return_value=set()),
                "remember_and_cache_post": AsyncMock(),
                "save_user_query": AsyncMock(),
                "mark_post_sent": AsyncMock(),
                "send_post_media": delivery,
            }
            for name, replacement in replacements.items():
                stack.enter_context(patch.object(bot, name, replacement))
            for name in ("get_random_image", "get_next_image", "get_global_random_image"):
                stack.enter_context(
                    patch.object(bot.api, name, AsyncMock(return_value=rejected))
                )
            stack.enter_context(patch.object(bot.api, "search", AsyncMock(side_effect=[[rejected], []])))
            stack.enter_context(patch.object(bot.api, "save_search_state", AsyncMock()))
            if mode == "random":
                delivered = await bot.send_random_image(message, 1)
            else:
                delivered = await bot.send_image(
                    message, 1, "fixture", is_more=mode == "more"
                )
        self.assertFalse(delivered)
        delivery.assert_not_awaited()

    async def test_regular_search_rejects_fifth_rating_mismatch(self):
        await self._assert_rejected("search", {"rating_filter": "s"}, post(rating="e"))

    async def test_more_rejects_fifth_media_type_mismatch(self):
        await self._assert_rejected("more", {"media_type": "videos"}, post())

    async def test_random_rejects_fifth_dimension_mismatch(self):
        await self._assert_rejected(
            "random", {"min_width": 1280, "min_height": 720}, post(width=640, height=480)
        )

    async def test_quality_preference_reaches_delivery(self):
        lower_delivery = AsyncMock(return_value=True)
        value = post()
        with patch.object(bot, "send_post_media_with_retries", lower_delivery):
            self.assertTrue(
                await bot.send_post_media(
                    AsyncMock(), value, settings={"quality_mode": "preview"}
                )
            )
        prepared = lower_delivery.await_args.args[1]
        self.assertEqual(prepared["file_url"], value["preview_url"])


class SubscriptionOptionProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot.user_operation_gate.reset_for_tests()
        bot.temporary_user_state.clear_all()
        bot.issued_one_shot_callbacks.clear()
        self.tmp = tempfile.TemporaryDirectory(prefix="subscription_callback_")
        self.patches = ExitStack()
        db_path = str(Path(self.tmp.name) / "test.db")
        for module in (database, bot_state):
            self.patches.enter_context(patch.object(module, "DB_PATH", db_path))
        self.patches.enter_context(patch.object(bot_state, "callback_payloads", {}))
        self.patches.enter_context(
            patch.object(bot_state, "last_callback_db_cleanup", time.monotonic())
        )
        await database.init_db()

    async def asyncTearDown(self):
        bot.temporary_user_state.clear_all()
        bot.issued_one_shot_callbacks.clear()
        bot.user_operation_gate.reset_for_tests()
        self.patches.close()
        self.tmp.cleanup()

    async def test_all_option_buttons_round_trip_through_handler(self):
        for index, action in enumerate(bot.SUBSCRIPTION_OPTION_ACTIONS, start=1):
            with self.subTest(action=action):
                query_text = f"fixture_{action}"
                await database.add_subscription(index, query_text, cooldown_seconds=0)
                message = SimpleNamespace(reply_text=AsyncMock())
                await bot.show_subscription_options(message, index, query_text)
                keyboard = message.reply_text.await_args.kwargs["reply_markup"]
                callback_data = next(
                    row[0].callback_data
                    for row in keyboard.inline_keyboard
                    if row[0].callback_data.startswith(f"subopt_{action}_")
                )
                query = SimpleNamespace(
                    data=callback_data,
                    from_user=SimpleNamespace(id=index),
                    message=message,
                    edit_message_text=AsyncMock(),
                )
                with patch.object(bot, "safe_query_answer", AsyncMock()):
                    await bot.button_handler(
                        SimpleNamespace(callback_query=query), SimpleNamespace()
                    )
                self.assertNotIn("sub_options_", callback_data.removeprefix(f"subopt_{action}_"))
                if action != "blacklist":
                    options = await database.get_subscription_options(index, query_text)
                    expected = {
                        "rating": ("rating_filter", "s"),
                        "type": ("media_type", "images"),
                        "orientation": ("orientation", "portrait"),
                        "resolution": ("min_width", 1280),
                        "quality": ("quality_mode", "preview"),
                        "digest": ("digest_mode", "digest"),
                    }
                    key, value = expected[action]
                    self.assertEqual(options.get(key), value)


class DockerContextTests(unittest.TestCase):
    def test_database_backups_and_sidecars_are_excluded(self):
        rules = {
            line.strip()
            for line in (Path(__file__).resolve().parents[1] / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertTrue(
            {"backups/", "*.db", "*.db-*", "*.sqlite", "*.sqlite-*", "*.sqlite3", "*.sqlite3-*"}.issubset(rules)
        )


if __name__ == "__main__":
    unittest.main()
