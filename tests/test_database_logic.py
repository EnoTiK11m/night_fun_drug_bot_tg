import unittest

from database import get_empty_backoff_minutes


class SubscriptionBackoffTests(unittest.TestCase):
    def test_backoff_never_less_than_interval(self):
        self.assertEqual(get_empty_backoff_minutes(1, 120), 120)

    def test_backoff_grows_for_short_intervals(self):
        self.assertEqual(get_empty_backoff_minutes(1, 10), 60)
        self.assertEqual(get_empty_backoff_minutes(2, 10), 120)
        self.assertEqual(get_empty_backoff_minutes(3, 10), 240)

    def test_backoff_is_capped(self):
        self.assertEqual(get_empty_backoff_minutes(99, 10), 720)


if __name__ == "__main__":
    unittest.main()
