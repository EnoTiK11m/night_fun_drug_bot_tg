import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import app.telegram.application as bot
import app.storage.database as database


class SubscriptionQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="subscription_quotas_")
        self.old_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tempdir, "test.db")
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.old_db_path
        shutil.rmtree(self.tempdir, ignore_errors=True)

    async def _scalar(self, sql, params=()):
        async with database.connect_db() as db:
            cursor = await db.execute(sql, params)
            row = await cursor.fetchone()
        return row[0]

    async def test_total_quota_counts_inactive_subscriptions(self):
        first = await database.add_subscription(
            1, "first", total_limit=2, active_limit=2, cooldown_seconds=0
        )
        second = await database.add_subscription(
            1, "second", total_limit=2, active_limit=2, cooldown_seconds=0
        )
        await database.toggle_subscription(1, "first", active_limit=2)

        result = await database.add_subscription(
            1, "third", total_limit=2, active_limit=2, cooldown_seconds=0
        )

        self.assertEqual((first.status, second.status), ("created", "created"))
        self.assertEqual(result.status, "total_limit_reached")
        self.assertEqual((result.total_count, result.active_count), (2, 1))
        self.assertEqual(await database.get_subscription_usage(1), (2, 1))

    async def test_active_quota_rejects_new_subscription(self):
        created = await database.add_subscription(
            1, "first", total_limit=4, active_limit=1, cooldown_seconds=0
        )

        result = await database.add_subscription(
            1, "second", total_limit=4, active_limit=1, cooldown_seconds=0
        )

        self.assertEqual(created.status, "created")
        self.assertEqual(result.status, "active_limit_reached")
        self.assertEqual((result.total_count, result.active_count), (1, 1))
        self.assertEqual(await database.get_subscription_usage(1), (1, 1))

    async def test_concurrent_creates_never_exceed_quota(self):
        results = await asyncio.gather(*(
            database.add_subscription(
                1,
                f"query-{index}",
                total_limit=3,
                active_limit=3,
                cooldown_seconds=0,
            )
            for index in range(12)
        ))

        self.assertEqual(sum(result.status == "created" for result in results), 3)
        self.assertTrue(all(
            result.status in {"created", "total_limit_reached"}
            for result in results
        ))
        self.assertEqual(await database.get_subscription_usage(1), (3, 3))

    async def test_creation_cooldown_is_persisted_in_database(self):
        created = await database.add_subscription(
            1, "first", total_limit=5, active_limit=5, cooldown_seconds=120
        )
        blocked = await database.add_subscription(
            1, "second", total_limit=5, active_limit=5, cooldown_seconds=120
        )

        self.assertEqual(created.status, "created")
        self.assertEqual(blocked.status, "cooldown")
        self.assertGreater(blocked.retry_after_seconds, 0)
        self.assertLessEqual(blocked.retry_after_seconds, 120)
        self.assertEqual(
            await self._scalar(
                "SELECT COUNT(*) FROM subscription_creation_state WHERE user_id = ?",
                (1,),
            ),
            1,
        )
        self.assertEqual(await database.get_subscription_usage(1), (1, 1))

    async def test_duplicate_update_consumes_neither_quota_nor_cooldown(self):
        await database.add_subscription(
            1, "same query", 10,
            total_limit=2, active_limit=2, cooldown_seconds=300,
        )
        async with database.connect_db() as db:
            await db.execute("""
                UPDATE subscription_creation_state
                SET last_created_at = datetime('now', '-301 seconds')
                WHERE user_id = ?
            """, (1,))
            await db.commit()

        duplicate = await database.add_subscription(
            1, "  same   query  ", 30,
            total_limit=2, active_limit=2, cooldown_seconds=300,
        )
        second = await database.add_subscription(
            1, "second", 10,
            total_limit=2, active_limit=2, cooldown_seconds=300,
        )

        self.assertEqual(duplicate.status, "updated")
        self.assertEqual((duplicate.total_count, duplicate.active_count), (1, 1))
        self.assertEqual(second.status, "created")
        self.assertEqual((second.total_count, second.active_count), (2, 2))
        async with database.connect_db() as db:
            cursor = await db.execute("""
                SELECT interval_minutes FROM subscriptions
                WHERE user_id = ? AND query = ?
            """, (1, "same query"))
            row = await cursor.fetchone()
        self.assertEqual(row, (30,))

    async def test_inactive_reactivation_checks_active_quota_and_preserves_state(self):
        await database.add_subscription(
            1, "target", 10,
            total_limit=4, active_limit=2, cooldown_seconds=0,
        )
        await database.toggle_subscription(1, "target", active_limit=2)
        await database.add_subscription(
            1, "blocker", 10,
            total_limit=4, active_limit=2, cooldown_seconds=0,
        )
        async with database.connect_db() as db:
            await db.execute("""
                UPDATE subscriptions
                SET settings_json = ?, digest_mode = ?, next_check_at = ?,
                    processing_until = ?, processing_token = ?
                WHERE user_id = ? AND query = ?
            """, (
                '{"rating_filter":"s"}', "digest", "2030-01-02 03:04:05",
                "2030-01-02 03:09:05", "claim-token", 1, "target",
            ))
            await db.commit()

        at_limit = await database.add_subscription(
            1, "target", 30,
            total_limit=4, active_limit=1, cooldown_seconds=0,
        )
        self.assertEqual(at_limit.status, "active_limit_reached")
        self.assertEqual((at_limit.total_count, at_limit.active_count), (2, 1))

        await database.toggle_subscription(1, "blocker", active_limit=2)
        reactivated = await database.add_subscription(
            1, "target", 30,
            total_limit=4, active_limit=1, cooldown_seconds=999,
        )

        self.assertEqual(reactivated.status, "reactivated")
        self.assertEqual((reactivated.total_count, reactivated.active_count), (2, 1))
        async with database.connect_db() as db:
            cursor = await db.execute("""
                SELECT interval_minutes, is_active, settings_json, digest_mode,
                       next_check_at, processing_until, processing_token
                FROM subscriptions WHERE user_id = ? AND query = ?
            """, (1, "target"))
            row = await cursor.fetchone()
        self.assertEqual(row, (
            30, 1, '{"rating_filter":"s"}', "digest",
            "2030-01-02 03:04:05", "2030-01-02 03:09:05", "claim-token",
        ))

    async def test_invalid_queries_do_not_write_any_subscription_state(self):
        cases = (
            ("   ", "invalid_query"),
            ("valid\x00invalid", "invalid_query"),
            ("x" * (database.SUBSCRIPTION_QUERY_MAX_LENGTH + 1), "query_too_long"),
            (
                " ".join(
                    f"tag{index}"
                    for index in range(database.SUBSCRIPTION_QUERY_MAX_TAGS + 1)
                ),
                "too_many_tags",
            ),
        )

        for query, expected_status in cases:
            with self.subTest(expected_status=expected_status):
                result = await database.add_subscription(
                    1, query, cooldown_seconds=0
                )
                self.assertEqual(result.status, expected_status)
                self.assertFalse(result)

        self.assertEqual(await database.get_subscription_usage(1), (0, 0))
        self.assertEqual(
            await self._scalar("SELECT COUNT(*) FROM subscription_creation_state"),
            0,
        )

    async def test_results_expose_status_limits_counts_and_boolean_semantics(self):
        created = await database.add_subscription(
            1, "first", total_limit=1, active_limit=1, cooldown_seconds=0
        )
        rejected = await database.add_subscription(
            1, "second", total_limit=1, active_limit=1, cooldown_seconds=0
        )
        missing = await database.toggle_subscription(1, "missing", active_limit=7)

        self.assertIsInstance(created, database.SubscriptionAddResult)
        self.assertEqual(
            (created.status, created.total_count, created.active_count,
             created.total_limit, created.active_limit),
            ("created", 1, 1, 1, 1),
        )
        self.assertTrue(created)
        self.assertEqual(
            (rejected.status, rejected.total_count, rejected.active_count,
             rejected.total_limit, rejected.active_limit),
            ("total_limit_reached", 1, 1, 1, 1),
        )
        self.assertFalse(rejected)
        self.assertIsInstance(missing, database.SubscriptionToggleResult)
        self.assertEqual(
            (missing.status, missing.is_active, missing.active_count,
             missing.active_limit),
            ("not_found", None, 0, 7),
        )

    async def test_toggle_reactivation_enforces_active_quota(self):
        for query in ("inactive", "active"):
            await database.add_subscription(
                1, query, total_limit=3, active_limit=2, cooldown_seconds=0
            )
        deactivated = await database.toggle_subscription(
            1, "inactive", active_limit=2
        )

        blocked = await database.toggle_subscription(
            1, "inactive", active_limit=1
        )

        self.assertEqual(deactivated.status, "deactivated")
        self.assertEqual((deactivated.is_active, deactivated.active_count), (False, 1))
        self.assertEqual(blocked.status, "active_limit_reached")
        self.assertEqual(
            (blocked.is_active, blocked.active_count, blocked.active_limit),
            (False, 1, 1),
        )
        self.assertEqual(await database.get_subscription_usage(1), (2, 1))

        await database.toggle_subscription(1, "active", active_limit=1)
        reactivated = await database.toggle_subscription(
            1, "inactive", active_limit=1
        )
        self.assertEqual(
            (reactivated.status, reactivated.is_active,
             reactivated.active_count, reactivated.active_limit),
            ("reactivated", True, 1, 1),
        )
        self.assertEqual(await database.get_subscription_usage(1), (2, 1))

    async def test_subscription_menu_and_quota_errors_are_actionable(self):
        with (
            patch.object(bot, "get_subscription_pause_until", AsyncMock(return_value=None)),
            patch.object(bot, "get_subscription_usage", AsyncMock(return_value=(7, 3))),
        ):
            menu = await bot.build_subscriptions_menu_text(1)

        self.assertIn(f"Подписки: 7 из {bot.SUBSCRIPTION_MAX_TOTAL}", menu)
        self.assertIn(f"Активные: 3 из {bot.SUBSCRIPTION_MAX_ACTIVE}", menu)
        total_error = bot.build_subscription_create_error(
            database.SubscriptionAddResult(
                "total_limit_reached", total_count=20, total_limit=20
            )
        )
        active_error = bot.build_subscription_create_error(
            database.SubscriptionAddResult("active_limit_reached")
        )
        cooldown_error = bot.build_subscription_create_error(
            database.SubscriptionAddResult("cooldown", retry_after_seconds=12)
        )
        self.assertEqual(total_error, "❌ Достигнут лимит подписок: 20 из 20.")
        self.assertIn("приостановите или удалите", active_error)
        self.assertIn("12 сек", cooldown_error)

    async def test_legacy_user_over_total_limit_cannot_reactivate(self):
        async with database.connect_db() as db:
            for index in range(25):
                await db.execute("""
                    INSERT INTO subscriptions(user_id, query, is_active)
                    VALUES (?, ?, ?)
                """, (1, f"legacy-{index}", 1 if index == 0 else 0))
            await db.commit()

        via_add = await database.add_subscription(
            1, "legacy-1", total_limit=20, active_limit=20, cooldown_seconds=0
        )
        via_toggle = await database.toggle_subscription(
            1, "legacy-1", total_limit=20, active_limit=20
        )
        deactivated = await database.toggle_subscription(
            1, "legacy-0", total_limit=20, active_limit=20
        )

        self.assertEqual(via_add.status, "total_limit_reached")
        self.assertEqual(via_toggle.status, "total_limit_reached")
        self.assertEqual(deactivated.status, "deactivated")

        for index in range(19, 25):
            self.assertTrue(await database.remove_subscription(1, f"legacy-{index}"))
        reactivated = await database.toggle_subscription(
            1, "legacy-1", total_limit=20, active_limit=20
        )
        self.assertEqual(reactivated.status, "reactivated")

    async def test_legacy_double_space_key_is_reused_without_state_loss(self):
        legacy_query = "tag1  tag2"
        async with database.connect_db() as db:
            await db.execute("""
                INSERT INTO subscriptions(
                    user_id, query, interval_minutes, is_active, settings_json,
                    digest_mode, next_check_at, processing_token, processing_until
                ) VALUES (?, ?, 10, 1, ?, ?, ?, ?, ?)
            """, (
                1, legacy_query, '{"rating_filter":"s"}', "digest",
                "2030-01-02 03:04:05", "legacy-claim", "2030-01-02 03:09:05",
            ))
            await db.commit()

        self.assertEqual((await database.get_all_user_subscriptions(1))[0][0], legacy_query)
        updated = await database.add_subscription(
            1, "tag1 tag2", 30, total_limit=5, active_limit=5, cooldown_seconds=0
        )
        toggled = await database.toggle_subscription(
            1, legacy_query, total_limit=5, active_limit=5
        )

        self.assertEqual(updated.status, "updated")
        self.assertEqual(toggled.status, "deactivated")
        async with database.connect_db() as db:
            rows = await (await db.execute("""
                SELECT query, interval_minutes, settings_json, digest_mode,
                       next_check_at, processing_token, processing_until
                FROM subscriptions WHERE user_id = ?
            """, (1,))).fetchall()
        self.assertEqual(rows, [(
            legacy_query, 30, '{"rating_filter":"s"}', "digest",
            "2030-01-02 03:04:05", "legacy-claim", "2030-01-02 03:09:05",
        )])

    async def test_multiple_legacy_canonical_collisions_are_not_merged(self):
        async with database.connect_db() as db:
            await db.executemany("""
                INSERT INTO subscriptions(user_id, query, interval_minutes, is_active)
                VALUES (1, ?, 10, 1)
            """, [("tag1  tag2",), ("tag1   tag2",)])
            await db.commit()

        result = await database.add_subscription(
            1, "tag1 tag2", 30, total_limit=5, active_limit=5, cooldown_seconds=0
        )
        self.assertEqual(result.status, "ambiguous_query")
        async with database.connect_db() as db:
            rows = await (await db.execute("""
                SELECT query, interval_minutes FROM subscriptions
                WHERE user_id = 1 ORDER BY query COLLATE BINARY
            """)).fetchall()
        self.assertEqual(rows, [("tag1   tag2", 10), ("tag1  tag2", 10)])


if __name__ == "__main__":
    unittest.main()
