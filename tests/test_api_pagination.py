import unittest

from api_handler import rule34API


class FakeRule34API(rule34API):
    def __init__(self, pages):
        super().__init__()
        self.pages = pages
        self.requested_pids = []

    async def search(self, tags, blacklist, limit=100, pid=0, **kwargs):
        self.requested_pids.append(pid)
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


if __name__ == "__main__":
    unittest.main()
