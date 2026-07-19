import os
import shutil
import unittest
import uuid
import sqlite3

import database
from bot_features import (
    filter_and_sort_posts,
    media_group_compatible_url,
    normalize_feature_settings,
    post_matches_preferences,
    prepare_gallery_album_posts,
    prepare_post_quality,
)


class FeatureDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = f"test_features_{uuid.uuid4().hex}"
        os.makedirs(self.tempdir)
        self.old_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tempdir, "test.db")
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.old_db_path
        shutil.rmtree(self.tempdir, ignore_errors=True)

    async def test_temporary_blacklist_expires(self):
        await database.add_temporary_blacklist_tag(1, "animated", 60)
        self.assertIn("animated", await database.get_user_blacklist(1))

        async with database.connect_db() as db:
            await db.execute(
                "UPDATE blacklist SET expires_at = datetime('now', '-1 minute') WHERE user_id = 1"
            )
            await db.commit()

        self.assertNotIn("animated", await database.get_user_blacklist(1))

    async def test_temporary_blacklist_does_not_weaken_permanent_entry(self):
        await database.add_to_blacklist(1, "always_hidden")

        changed = await database.add_temporary_blacklist_tag(1, "always_hidden", 10)

        self.assertFalse(changed)
        entries = await database.get_blacklist_entries(1)
        self.assertIsNone(entries[0]["expires_at"])

    async def test_removing_preset_keeps_preexisting_manual_tag(self):
        await database.add_to_blacklist(1, "animated")
        added = await database.apply_blacklist_preset(1, "animated")
        self.assertGreaterEqual(added, 1)

        await database.remove_blacklist_preset(1, "animated")

        self.assertIn("animated", await database.get_user_blacklist(1))

    async def test_collection_crud_note_and_favorite_cascade(self):
        post = {
            "id": 42,
            "file_url": "https://example.test/42.jpg",
            "tags": "alpha beta",
        }
        self.assertTrue(await database.add_favorite(1, post))
        collection_id = await database.create_favorite_collection(1, "Wallpapers")
        self.assertIsNotNone(collection_id)
        self.assertTrue(await database.add_favorite_to_collection(1, collection_id, 42))
        self.assertTrue(await database.set_favorite_note(1, 42, "best one"))
        self.assertEqual(await database.count_collection_favorites(1, collection_id), 1)
        self.assertEqual(await database.get_favorite_note(1, 42), "best one")

        await database.remove_favorite(1, 42)

        self.assertEqual(await database.count_collection_favorites(1, collection_id), 0)
        self.assertEqual(await database.get_favorite_note(1, 42), "")

    async def test_activity_stats_and_clear_preserve_favorites(self):
        await database.save_user_query(1, "alpha")
        await database.mark_post_sent(1, 10)
        await database.add_favorite(1, {"id": 10, "file_url": "x.jpg", "tags": "alpha"})

        stats = await database.get_user_activity_stats(1)
        self.assertEqual(stats["viewed_total"], 1)
        self.assertEqual(stats["searches_total"], 1)
        self.assertEqual(stats["favorites_total"], 1)

        await database.clear_user_activity_stats(1)
        stats = await database.get_user_activity_stats(1)
        self.assertEqual(stats["viewed_total"], 0)
        self.assertEqual(stats["searches_total"], 0)
        self.assertEqual(stats["favorites_total"], 1)

    async def test_failed_delivery_queue_upserts_and_deletes(self):
        post = {"id": 7, "file_url": "https://example.test/7.jpg"}
        await database.save_delivery_failure(1, post, "caption", "timeout")
        await database.save_delivery_failure(1, post, "caption", "timeout again")

        failures = await database.get_delivery_failures()
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["attempts"], 2)
        self.assertEqual(failures[0]["post"]["id"], 7)

        await database.delete_delivery_failure(failures[0]["id"])
        self.assertEqual(await database.get_delivery_failures(), [])

    async def test_search_preset_crud_preserves_settings(self):
        preset_id = await database.create_search_preset(
            1, "Landscape", "character -comic", {"orientation": "landscape"}
        )

        preset = await database.get_search_preset(1, preset_id)

        self.assertEqual(preset["query"], "character -comic")
        self.assertEqual(preset["settings"]["orientation"], "landscape")
        self.assertTrue(await database.delete_search_preset(1, preset_id))

    async def test_subscription_options_are_isolated_per_query(self):
        await database.add_subscription(1, "alpha", 10)
        await database.update_subscription_options(1, "alpha", {
            "rating_filter": "s", "digest_mode": "digest", "extra_blacklist": "comic"
        })

        options = await database.get_subscription_options(1, "alpha")

        self.assertEqual(options["rating_filter"], "s")
        self.assertEqual(options["digest_mode"], "digest")
        self.assertEqual(options["extra_blacklist"], "comic")

    async def test_subscription_cache_preserves_dimensions_for_filters(self):
        await database.replace_subscription_cache(1, "alpha", [{
            "id": 9,
            "file_url": "https://example.test/9.jpg",
            "width": 1920,
            "height": 1080,
        }])

        posts, _cached_at = await database.get_subscription_cache(1, "alpha")

        self.assertEqual(posts[0]["width"], 1920)
        self.assertEqual(posts[0]["height"], 1080)

    async def test_read_later_and_digest_queue_round_trip(self):
        post = {"id": 77, "file_url": "https://example.test/77.jpg", "tags": "alpha"}
        self.assertTrue(await database.add_read_later(1, post, 30))
        self.assertEqual((await database.get_read_later(1))[0]["id"], 77)
        self.assertTrue(await database.remove_read_later(1, 77))

        self.assertTrue(await database.enqueue_subscription_digest(1, "alpha", post))
        self.assertEqual(await database.count_subscription_digest(1), 1)
        digest = await database.pop_subscription_digest(1, 10)
        self.assertEqual(digest[0]["subscription_query"], "alpha")
        self.assertEqual(await database.pop_subscription_digest(1, 10), [])
        self.assertEqual(await database.count_subscription_digest(1), 0)

    async def test_favorite_profile_search_and_storage_stats(self):
        post = {
            "id": 88, "file_url": "https://example.test/88.jpg",
            "tags": "character_name blue_hair solo",
        }
        await database.add_favorite(1, post)
        await database.set_favorite_note(1, 88, "best wallpaper")

        profile = await database.get_favorite_tag_profile(1)
        results = await database.search_favorites(1, "wallpaper blue_hair")
        stats = await database.get_user_storage_stats(1)

        self.assertIn(("blue_hair", 1), profile)
        self.assertEqual(results[0]["id"], 88)
        self.assertEqual(stats["favorites"], 1)

    async def test_tag_translation_queue_round_trip_and_seed(self):
        await database.cache_post({
            "id": 99,
            "file_url": "https://example.test/99.jpg",
            "tags": "blue_hair green_eyes",
        })
        await database.add_to_blacklist(1, "gore")

        inserted = await database.seed_tag_translation_queue()
        pending = await database.get_pending_tag_translations(10)

        self.assertGreaterEqual(inserted, 3)
        self.assertTrue({"blue_hair", "green_eyes", "gore"}.issubset(set(pending)))

        await database.save_tag_translations_bulk({
            "blue_hair": "голубые волосы",
            "green_eyes": "зелёные глаза",
        })
        translations = await database.get_tag_translations(
            ["blue_hair", "green_eyes", "missing"]
        )
        self.assertEqual(translations["blue_hair"], "голубые волосы")
        self.assertEqual(translations["green_eyes"], "зелёные глаза")

        await database.mark_tag_translations_failed(["gore"])
        states = await database.get_tag_translation_states(
            ["blue_hair", "green_eyes", "gore"]
        )
        self.assertEqual(states["blue_hair"], "ready")
        self.assertEqual(states["gore"], "failed")


class MediaPreferenceTests(unittest.TestCase):
    def test_filter_sort_and_orientation(self):
        posts = [
            {"id": 1, "file_url": "1.jpg", "width": 800, "height": 1200, "rating": "s", "score": 1},
            {"id": 2, "file_url": "2.jpg", "width": 1600, "height": 900, "rating": "s", "score": 50},
            {"id": 3, "file_url": "3.mp4", "width": 1920, "height": 1080, "rating": "e", "score": 100},
        ]
        settings = normalize_feature_settings({
            "gallery_sort": "popular",
            "rating_filter": "s",
            "media_type": "images",
            "orientation": "landscape",
            "min_width": 1000,
        })

        result = filter_and_sort_posts(posts, settings)

        self.assertEqual([post["id"] for post in result], [2])
        self.assertTrue(post_matches_preferences(posts[1], settings))

    def test_quality_order_and_limits_are_normalized(self):
        post = {
            "id": 1,
            "file_url": "original.jpg",
            "sample_url": "sample.jpg",
            "preview_url": "preview.jpg",
            "file_size": 20 * 1024 * 1024,
        }
        settings = normalize_feature_settings({
            "quality_mode": "auto", "max_file_mb": 5, "gallery_size": 99
        })

        prepared = prepare_post_quality(post, settings)

        self.assertEqual(prepared["file_url"], "sample.jpg")
        self.assertEqual(settings["gallery_size"], 10)
        self.assertEqual(settings["spoiler_mode"], "off")

    def test_gallery_replaces_gif_with_static_preview_and_fills_album(self):
        posts = [
            {
                "id": 1,
                "file_url": "https://example.test/animated.gif",
                "sample_url": "https://example.test/animated-sample.jpg",
            }
        ] + [
            {"id": post_id, "file_url": f"https://example.test/{post_id}.jpg"}
            for post_id in range(2, 12)
        ]

        prepared = prepare_gallery_album_posts(posts, {"quality_mode": "original"}, 10)

        self.assertEqual(len(prepared), 10)
        self.assertEqual(prepared[0]["file_url"], "https://example.test/animated-sample.jpg")
        self.assertTrue(all(media_group_compatible_url(post["file_url"]) for post in prepared))

    def test_gallery_skips_gif_without_preview_and_keeps_scanning(self):
        posts = [
            {"id": 1, "file_url": "https://example.test/animated.gif"},
            {"id": 2, "file_url": "https://example.test/2.jpg"},
            {"id": 3, "file_url": "https://example.test/3.jpg"},
        ]

        prepared = prepare_gallery_album_posts(posts, {"quality_mode": "original"}, 2)

        self.assertEqual([post["id"] for post in prepared], [2, 3])


class LegacyMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_adds_subscription_feature_columns(self):
        tempdir = f"test_sub_migration_{uuid.uuid4().hex}"
        os.makedirs(tempdir)
        path = os.path.join(tempdir, "legacy.db")
        connection = sqlite3.connect(path)
        connection.execute("""
            CREATE TABLE subscriptions (
                user_id INTEGER, query TEXT, interval_minutes INTEGER DEFAULT 10,
                is_active BOOLEAN DEFAULT 1, last_sent TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, query)
            )
        """)
        connection.execute("""
            CREATE TABLE subscription_cache (
                user_id INTEGER, query TEXT, post_id INTEGER, file_url TEXT,
                tags TEXT DEFAULT '', rating TEXT DEFAULT '', score INTEGER DEFAULT 0,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, query, post_id)
            )
        """)
        connection.execute("INSERT INTO subscriptions (user_id, query) VALUES (1, 'keep')")
        connection.commit()
        connection.close()
        old_path = database.DB_PATH
        database.DB_PATH = path
        try:
            await database.init_db()
            async with database.connect_db() as db:
                sub_columns = {
                    row[1] for row in await (await db.execute("PRAGMA table_info(subscriptions)" )).fetchall()
                }
                cache_columns = {
                    row[1] for row in await (await db.execute("PRAGMA table_info(subscription_cache)" )).fetchall()
                }
            self.assertTrue({"settings_json", "digest_mode"}.issubset(sub_columns))
            self.assertTrue({"width", "height"}.issubset(cache_columns))
            self.assertEqual((await database.get_all_user_subscriptions(1))[0][0], "keep")
        finally:
            database.DB_PATH = old_path
            shutil.rmtree(tempdir, ignore_errors=True)

    async def test_init_migrates_legacy_blacklist_without_data_loss(self):
        tempdir = f"test_migration_{uuid.uuid4().hex}"
        os.makedirs(tempdir)
        path = os.path.join(tempdir, "legacy.db")
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE blacklist (user_id INTEGER, tag TEXT, PRIMARY KEY(user_id, tag))"
        )
        connection.execute("INSERT INTO blacklist VALUES (1, 'keep_me')")
        connection.commit()
        connection.close()
        old_path = database.DB_PATH
        database.DB_PATH = path
        try:
            await database.init_db()
            entries = await database.get_blacklist_entries(1)
            async with database.connect_db() as db:
                cursor = await db.execute("PRAGMA table_info(blacklist)")
                columns = {row[1] for row in await cursor.fetchall()}
            self.assertEqual(entries[0]["tag"], "keep_me")
            self.assertIn("expires_at", columns)
            self.assertIn("source", columns)
        finally:
            database.DB_PATH = old_path
            shutil.rmtree(tempdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
