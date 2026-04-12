import unittest
from unittest.mock import patch

import bot


class BotHelperTests(unittest.IsolatedAsyncioTestCase):
    def test_callback_payload_round_trip(self):
        callback_data = bot.store_callback_payload("hist", "long query with spaces")

        self.assertLessEqual(len(callback_data.encode("utf-8")), 64)
        self.assertEqual(
            bot.get_callback_payload("hist", callback_data),
            "long query with spaces",
        )

    def test_callback_payload_expires(self):
        callback_data = bot.store_callback_payload("hist", "old query")

        with patch("bot.CALLBACK_TTL_SECONDS", 0):
            self.assertEqual(bot.get_callback_payload("hist", callback_data), "")

    def test_parse_subscription_interval_clamps_to_supported_range(self):
        self.assertEqual(bot.parse_subscription_interval("0"), 1)
        self.assertEqual(bot.parse_subscription_interval("45"), 45)
        self.assertEqual(bot.parse_subscription_interval("121"), 120)
        self.assertEqual(bot.parse_subscription_interval("abc"), 10)

    async def test_build_caption_keeps_tag_underscores_and_clamps_length(self):
        settings = {
            "show_caption": True,
            "show_search_query": True,
            "show_subscription_label": False,
            "show_id": True,
            "show_score": True,
            "show_rating": True,
            "show_tags": True,
        }
        result = {
            "id": 123,
            "score": 5,
            "rating": "safe_value",
            "tags": "tag_" * 400,
        }

        caption = await bot.build_caption(settings, result, "blue_eyes")

        self.assertLessEqual(len(caption), bot.MAX_CAPTION_LENGTH)
        self.assertIn("blue_eyes", caption)
        self.assertNotIn("\\_", caption)


if __name__ == "__main__":
    unittest.main()
