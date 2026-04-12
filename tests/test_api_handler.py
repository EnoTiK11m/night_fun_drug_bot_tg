import unittest
from unittest.mock import patch

from api_handler import rule34API


class Rule34APISearchStateTests(unittest.IsolatedAsyncioTestCase):
    def test_build_search_tags_adds_blacklist_exclusions(self):
        api = rule34API()

        tags = api._build_search_tags("blue_eyes", {"red_hair"})

        self.assertIn("blue_eyes", tags)
        self.assertIn("-red_hair", tags)

    def test_build_search_tags_returns_empty_for_empty_query(self):
        api = rule34API()

        self.assertEqual(api._build_search_tags("   ", {"red_hair"}), "")

    async def test_save_search_state_tracks_initial_post(self):
        api = rule34API()

        await api.save_search_state(
            user_id=1,
            tags="tag",
            blacklist=set(),
            current_post_id=42,
        )

        self.assertEqual(api.user_search_states[1]["used_posts"], {42})

    async def test_get_next_image_skips_posts_already_seen(self):
        api = rule34API()
        api.user_search_states[1] = {
            "tags": "tag",
            "blacklist": set(),
            "current_pid": 0,
            "used_posts": {42},
        }

        async def fake_search(tags, blacklist, limit=100, pid=0):
            return [
                {"id": 42, "file_url": "https://example.test/old.jpg"},
                {"id": 43, "file_url": "https://example.test/new.jpg"},
            ]

        api.search = fake_search

        with patch("api_handler.random.choice", side_effect=lambda posts: posts[0]):
            result = await api.get_next_image(1, "tag", set())

        self.assertEqual(result["id"], 43)
        self.assertEqual(api.user_search_states[1]["used_posts"], {42, 43})

    async def test_get_random_image_scans_until_unseen_post_or_empty_page(self):
        api = rule34API()
        calls = []

        async def fake_search(tags, blacklist, limit=100, pid=0):
            calls.append(pid)
            if pid == 0:
                return [{"id": 42, "file_url": "https://example.test/old.jpg"}]
            if pid == 1:
                return [{"id": 43, "file_url": "https://example.test/new.jpg"}]
            return []

        api.search = fake_search

        result = await api.get_random_image("tag", set(), {42})

        self.assertEqual(result["id"], 43)
        self.assertEqual(calls, [0, 1])

    async def test_get_random_image_stops_on_empty_page(self):
        api = rule34API()

        async def fake_search(tags, blacklist, limit=100, pid=0):
            return []

        api.search = fake_search

        self.assertIsNone(await api.get_random_image("tag", set(), set()))


if __name__ == "__main__":
    unittest.main()
