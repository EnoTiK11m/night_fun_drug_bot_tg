import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

import database


class CacheRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="test_cache_retention_")
        self.old_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tempdir, "cache.db")
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.old_path
        shutil.rmtree(self.tempdir, ignore_errors=True)

    async def insert_subscription_cache(
        self, post_id: int, *, user_id: int = 1, query: str = "tag", cached_at: str
    ):
        async with database.connect_db() as db:
            await db.execute(
                """
                INSERT INTO subscription_cache
                (user_id, query, post_id, file_url, cached_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, query, post_id, f"https://example.test/{post_id}.jpg", cached_at),
            )
            await db.commit()

    async def insert_post_cache(self, post_id: int, *, cached_at: str):
        async with database.connect_db() as db:
            await db.execute(
                "INSERT INTO post_cache (post_id, file_url, cached_at) VALUES (?, ?, ?)",
                (post_id, f"https://example.test/{post_id}.jpg", cached_at),
            )
            await db.commit()

    async def cleanup(self, **overrides):
        options = dict(
            subscription_ttl_minutes=60,
            subscription_max_per_query=100,
            subscription_max_rows=1000,
            post_ttl_hours=24,
            post_max_rows=1000,
            batch_size=100,
        )
        options.update(overrides)
        return await database.cleanup_expired_caches(**options)

    async def scalar(self, sql, params=()):
        async with database.connect_db() as db:
            row = await (await db.execute(sql, params)).fetchone()
            return row[0]

    async def test_fresh_subscription_cache_is_preserved(self):
        await self.insert_subscription_cache(1, cached_at="2099-01-01 00:00:00")
        result = await self.cleanup()
        self.assertEqual(result.subscription_cache_deleted, 0)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM subscription_cache"), 1)

    async def test_expired_subscription_cache_is_deleted(self):
        await self.insert_subscription_cache(1, cached_at="2000-01-01 00:00:00")
        result = await self.cleanup()
        self.assertEqual(result.subscription_cache_deleted, 1)
        self.assertEqual(result.subscription_cache_remaining, 0)

    async def test_active_subscription_claim_protects_cache_rows(self):
        async with database.connect_db() as db:
            await db.execute(
                """
                INSERT INTO subscriptions
                (user_id, query, processing_token, processing_until)
                VALUES (1, 'tag', 'active', datetime('now', '+10 minutes'))
                """
            )
            await db.commit()
        await self.insert_subscription_cache(1, cached_at="2000-01-01 00:00:00")
        result = await self.cleanup()
        self.assertEqual(result.subscription_cache_deleted, 0)

    async def test_per_query_limit_deletes_oldest_rows(self):
        for post_id, day in enumerate(range(1, 6), start=1):
            await self.insert_subscription_cache(
                post_id, cached_at=f"2099-01-{day:02d} 00:00:00"
            )
        result = await self.cleanup(subscription_max_per_query=2)
        self.assertEqual(result.subscription_cache_deleted, 3)
        async with database.connect_db() as db:
            rows = await (
                await db.execute("SELECT post_id FROM subscription_cache ORDER BY post_id")
            ).fetchall()
        self.assertEqual([row[0] for row in rows], [4, 5])

    async def test_subscription_global_limit_is_enforced_in_batches(self):
        for post_id in range(1, 7):
            await self.insert_subscription_cache(
                post_id,
                query=f"tag-{post_id}",
                cached_at=f"2099-01-{post_id:02d} 00:00:00",
            )
        first = await self.cleanup(subscription_max_rows=2, batch_size=2)
        self.assertEqual(first.subscription_cache_deleted, 2)
        self.assertEqual(first.subscription_cache_remaining, 4)
        second = await self.cleanup(subscription_max_rows=2, batch_size=2)
        self.assertEqual(second.subscription_cache_remaining, 2)

    async def test_expired_post_cache_is_deleted(self):
        await self.insert_post_cache(1, cached_at="2000-01-01 00:00:00")
        result = await self.cleanup()
        self.assertEqual(result.post_cache_deleted, 1)
        self.assertEqual(result.post_cache_remaining, 0)

    async def test_post_cache_hard_limit_is_enforced_in_batches(self):
        for post_id in range(1, 7):
            await self.insert_post_cache(
                post_id, cached_at=f"2099-01-{post_id:02d} 00:00:00"
            )
        first = await self.cleanup(post_max_rows=2, batch_size=2)
        self.assertEqual(first.post_cache_deleted, 2)
        self.assertEqual(first.post_cache_remaining, 4)
        second = await self.cleanup(post_max_rows=2, batch_size=2)
        self.assertEqual(second.post_cache_remaining, 2)

    async def test_post_cache_cleanup_does_not_delete_favorite(self):
        await self.insert_post_cache(1, cached_at="2000-01-01 00:00:00")
        async with database.connect_db() as db:
            await db.execute(
                "INSERT INTO favorites (user_id, post_id, file_url) VALUES (1, 1, 'saved.jpg')"
            )
            await db.commit()
        await self.cleanup()
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM post_cache"), 0)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM favorites"), 1)

    async def test_post_cache_cleanup_does_not_delete_read_later(self):
        await self.insert_post_cache(1, cached_at="2000-01-01 00:00:00")
        async with database.connect_db() as db:
            await db.execute(
                """
                INSERT INTO read_later (user_id, post_id, post_json, expires_at)
                VALUES (1, 1, ?, datetime('now', '+1 day'))
                """,
                (json.dumps({"id": 1, "file_url": "saved.jpg"}),),
            )
            await db.commit()
        await self.cleanup()
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM read_later"), 1)

    async def test_repeated_cleanup_is_idempotent(self):
        await self.insert_post_cache(1, cached_at="2000-01-01 00:00:00")
        first = await self.cleanup()
        second = await self.cleanup()
        self.assertEqual(first.post_cache_deleted, 1)
        self.assertEqual(second.post_cache_deleted, 0)
        self.assertEqual(second.post_cache_remaining, 0)

    async def test_two_concurrent_cleanups_do_not_corrupt_database(self):
        for post_id in range(1, 11):
            await self.insert_post_cache(post_id, cached_at="2000-01-01 00:00:00")
        results = await asyncio.gather(
            self.cleanup(batch_size=5),
            self.cleanup(batch_size=5),
        )
        self.assertEqual(sum(result.post_cache_deleted for result in results), 10)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM post_cache"), 0)
        self.assertEqual(await self.scalar("PRAGMA quick_check"), "ok")

    async def test_migration_three_is_idempotent_and_indexes_exist(self):
        await database.init_db()
        await database.init_db()
        async with database.connect_db() as db:
            versions = {
                row[0]
                for row in await (
                    await db.execute("SELECT version FROM schema_migrations")
                ).fetchall()
            }
            indexes = {
                row[0]
                for row in await (
                    await db.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                ).fetchall()
            }
        self.assertIn(3, versions)
        self.assertTrue(
            {
                "idx_subscription_cache_lookup",
                "idx_subscription_cache_cached",
                "idx_post_cache_cached",
            } <= indexes
        )

    async def test_cleanup_returns_exact_deleted_counts_and_remaining(self):
        for post_id in (1, 2):
            await self.insert_subscription_cache(
                post_id, cached_at="2000-01-01 00:00:00"
            )
            await self.insert_post_cache(post_id, cached_at="2000-01-01 00:00:00")
        result = await self.cleanup()
        self.assertEqual(result.subscription_cache_deleted, 2)
        self.assertEqual(result.post_cache_deleted, 2)
        self.assertEqual(result.subscription_cache_remaining, 0)
        self.assertEqual(result.post_cache_remaining, 0)
        self.assertGreaterEqual(result.elapsed_ms, 0)


class CacheRetentionLegacyMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_migration_preserves_cache_data(self):
        tempdir = tempfile.mkdtemp(prefix="test_cache_legacy_")
        path = os.path.join(tempdir, "legacy.db")
        connection = sqlite3.connect(path)
        connection.execute(
            """
            CREATE TABLE subscription_cache (
                user_id INTEGER, query TEXT, post_id INTEGER, file_url TEXT,
                tags TEXT DEFAULT '', rating TEXT DEFAULT '', score INTEGER DEFAULT 0,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, query, post_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE post_cache (
                post_id INTEGER PRIMARY KEY, file_url TEXT DEFAULT '', tags TEXT DEFAULT '',
                rating TEXT DEFAULT '', score INTEGER DEFAULT 0,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO subscription_cache (user_id, query, post_id) VALUES (1, 'keep', 1)"
        )
        connection.execute("INSERT INTO post_cache (post_id) VALUES (1)")
        connection.commit()
        connection.close()
        old_path = database.DB_PATH
        database.DB_PATH = path
        try:
            await database.init_db()
            await database.init_db()
            stats = await database.get_cache_storage_stats()
            self.assertEqual(stats["subscription_cache_rows"], 1)
            self.assertEqual(stats["post_cache_rows"], 1)
            async with database.connect_db() as db:
                version = await (
                    await db.execute(
                        "SELECT COUNT(*) FROM schema_migrations WHERE version = 3"
                    )
                ).fetchone()
            self.assertEqual(version[0], 1)
        finally:
            database.DB_PATH = old_path
            shutil.rmtree(tempdir, ignore_errors=True)
