import unittest

from api_handler import rule34API


class FakeRule34API(rule34API):
    def __init__(self, pages):
        super().__init__()
        self.pages = pages
        self.requested_pids = []
        self.search_kwargs = []
        self.search_limits = []

    async def search(self, tags, blacklist, limit=100, pid=0, **kwargs):
        self.requested_pids.append(pid)
        self.search_limits.append(limit)
        self.search_kwargs.append(kwargs)
        return self.pages.get(pid)


class ApiPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_random_search_stops_after_interactive_page_limit(self):
        pages = {
            pid: [{"id": str(pid), "file_url": ""}]
            for pid in range(30)
        }
        pages[30] = [{"id": "777", "file_url": "https://example.test/777.jpg"}]
        api = FakeRule34API(pages)

        result = await api.get_random_image("tag", set())

        self.assertIsNone(result)
        self.assertEqual(api.requested_pids, list(range(5)))

    async def test_next_search_stops_after_interactive_page_limit(self):
        pages = {
            pid: [{"id": str(pid), "file_url": "https://example.test/old.jpg"}]
            for pid in range(30)
        }
        pages[30] = [{"id": "888", "file_url": "https://example.test/888.jpg"}]
        api = FakeRule34API(pages)
        excluded = set(range(30))

        result = await api.get_next_image(1, "tag", set(), excluded)

        self.assertIsNone(result)
        self.assertEqual(api.requested_pids, list(range(5)))
        self.assertEqual(api.user_search_states[1]["current_pid"], 5)

    async def test_random_without_tags_uses_blacklist_only_search(self):
        pages = {
            0: [{"id": "777", "file_url": "https://example.test/777.jpg"}],
        }
        api = FakeRule34API(pages)

        result = await api.get_random_image("", {"blocked"})

        self.assertEqual(result["id"], "777")
        self.assertTrue(api.search_kwargs[0]["allow_blacklist_only"])

    async def test_global_random_uses_small_random_pages(self):
        pages = {
            17: [{"id": "777", "file_url": "https://example.test/777.jpg"}],
        }
        api = FakeRule34API(pages)

        with unittest.mock.patch("api_handler.random.randint", return_value=17):
            result = await api.get_global_random_image({"blocked"})

        self.assertEqual(result["id"], "777")
        self.assertEqual(api.requested_pids, [17])
        self.assertEqual(api.search_limits[0], 50)
        self.assertEqual(api.search_kwargs[0]["timeout"], 15)
        self.assertTrue(api.search_kwargs[0]["allow_blacklist_only"])


if __name__ == "__main__":
    unittest.main()
