import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from telegram.error import Forbidden
from contextlib import asynccontextmanager
import app.telegram.application as bot
import app.telegram.keyboards as bot_keyboards
import app.telegram.state as bot_state
import app.integrations.rule34.client as api_handler
import tempfile
import time
import threading
from test_database_integration import TempDatabaseTestCase
import app.storage.database as database
from app.telegram.formatting import build_caption, parse_pause_minutes, split_markdown_lines, md_code


class FormattingRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_and_subscription_lines_split_without_broken_spans(self):
        text = '\n'.join(f'• `{md_code("`" + "x" * 300)}`' for _ in range(100))
        chunks = split_markdown_lines(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual('\n'.join(chunks), text)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 3500)
            self.assertEqual(chunk.count('`') % 2, 0)
    async def test_idle_post_and_api_state_are_cleaned_without_access(self):
        with patch.object(bot_state, 'recent_posts', {1: ({'id': 1}, 0)}):
            bot_state.cleanup_recent_posts(now=100000)
            self.assertEqual(bot_state.recent_posts, {})
        api = api_handler.rule34API()
        api.user_search_states[1] = {'last_used': 0, 'used_posts': {1}}
        api.cleanup_search_states(now=100000)
        self.assertEqual(api.user_search_states, {})

    async def test_callback_database_write_does_not_block_event_loop_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bot_state, 'DB_PATH', directory + '/callbacks.db'):
            original = bot_state._store_callback_payload_db
            release = threading.Event()
            def slow_write(*args, **kwargs):
                release.wait(timeout=2)
                original(*args, **kwargs)
            with patch.object(bot_state, '_store_callback_payload_db', side_effect=slow_write):
                try:
                    async with asyncio.timeout(.5):
                        data = bot_state.store_callback_payload('hist', 'restart value')
                        await asyncio.sleep(.01)
                        self.assertFalse(release.is_set())
                finally:
                    release.set()
                await bot_state.flush_callback_payloads()
            bot_state.callback_payloads.clear()
            self.assertEqual(await bot_state.get_callback_payload_async('hist', data), 'restart value')

    async def test_long_caption_preserves_complete_code_spans(self):
        caption = await build_caption({}, {'id': 42, 'tags': 'tag ' * 1000}, '`\\' * 2000)
        self.assertLessEqual(len(caption), 1024)
        self.assertEqual(caption.count('`') % 2, 0)
        self.assertIn('42', caption)

    async def test_nonfinite_duration_is_rejected_as_validation_error(self):
        for value in ('nan', 'inf', '-inf', '1e309'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_pause_minutes(value)

    async def test_subscription_favorite_button_retains_query(self):
        issuer = Mock()
        issuer.payload = Mock(return_value='sub_fav_token')
        button = bot_keyboards.get_favorite_button(42, 'specific query', side_effect_callback=issuer)
        issuer.payload.assert_called_once_with('sub_fav', '42\nspecific query')
        self.assertEqual(button.callback_data, 'sub_fav_token')

    async def test_retry_forbidden_does_not_abort_second_entry(self):
        failures = [{'user_id': i, 'post_id': i, 'post': {'id': i}, 'caption': ''} for i in (1, 2)]
        update = SimpleNamespace(effective_user=SimpleNamespace(id=99), message=SimpleNamespace(reply_text=AsyncMock()))
        with (
            patch.object(bot, 'ADMIN_USER_IDS', {99}),
            patch.object(bot, 'is_recipient_allowed', return_value=True),
            patch.object(bot, 'claim_delivery_failures', AsyncMock(return_value=('claim', failures))),
            patch.object(bot, 'release_delivery_failure_claim', AsyncMock()),
            patch.object(bot, 'send_post_media_to_chat', AsyncMock(side_effect=[Forbidden('blocked'), True])) as send,
            patch.object(bot, 'delete_delivery_failure_for_post', AsyncMock(return_value=True)) as delete,
            patch.object(bot, 'get_subscription_image_keyboard', return_value=None),
            patch.object(database, 'mark_delivery_failure_permanent', AsyncMock(), create=True) as permanent,
        ):
            await bot.retry_failed_command(update, SimpleNamespace(bot=object()))
        self.assertEqual(send.await_count, 2)
        delete.assert_awaited_once_with(2, 2, 'claim')
        permanent.assert_awaited_once()


class SettingsRegressionTests(TempDatabaseTestCase):
    async def test_subscription_cache_fallback_loads_a_bounded_window(self):
        async with database.connect_db() as db:
            await db.executemany('INSERT INTO subscription_cache(user_id,query,post_id,file_url) VALUES (1,?,?,?)', [('tag', i, 'https://example.test/a.jpg') for i in range(1000)])
            await db.commit()
        posts, _ = await database.get_subscription_cache(1, 'tag')
        self.assertLessEqual(len(posts), database.SUBSCRIPTION_CACHE_MAX_PER_QUERY)
    async def test_favorite_source_survives_ram_and_query_cache_eviction(self):
        from test_user_state_races import callback_update
        await database.add_subscription(1, 'source tag', 60)
        post = {'id': 42, 'file_url': 'https://example.test/a.jpg'}
        with patch.object(bot_state, 'DB_PATH', database.DB_PATH):
            button = bot_keyboards.get_favorite_button(42, 'source tag', side_effect_callback=bot.subscription_callback_issuer_for(1))
            await bot_state.flush_callback_payloads()
            bot_state.callback_payloads.clear()
            bot_state.recent_posts.clear()
            async with database.connect_db() as db:
                await db.execute('DELETE FROM subscription_cache')
                await db.execute('DELETE FROM post_cache')
                await db.commit()
            update, query = callback_update(button.callback_data)
            with patch.object(bot, 'get_known_post', AsyncMock(return_value=post)), patch.object(bot, 'safe_query_answer', AsyncMock()):
                await bot.button_handler(update, SimpleNamespace(bot=object()))
            await bot_state.flush_callback_payloads()
        async with database.connect_db() as db:
            rows = await (await db.execute('SELECT query,post_id FROM subscription_posts WHERE user_id=1')).fetchall()
        self.assertEqual(rows, [('source tag', 42)])
    async def test_migration_six_upgrades_version_five_and_preserves_existing_data(self):
        await database.add_subscription(1, 'tag', 60)
        async with database.connect_db() as db:
            await db.execute('DELETE FROM schema_migrations WHERE version=6')
            await db.execute('DROP TABLE query_progress')
            await db.execute('DROP TABLE subscription_delivery_history')
            await db.execute('ALTER TABLE delivery_failures DROP COLUMN is_permanent')
            await db.commit()
        await database.init_db()
        await database.init_db()
        self.assertEqual(await database.get_user_subscriptions(1), [('tag', 3600)])
        async with database.connect_db() as db:
            self.assertEqual((await (await db.execute('SELECT count(*) FROM schema_migrations WHERE version=6')).fetchone())[0], 1)

    async def test_recorded_migration_with_missing_history_is_rejected(self):
        async with database.connect_db() as db:
            await db.execute('DROP TABLE subscription_delivery_history')
            await db.commit()
        with self.assertRaises(RuntimeError):
            await database.init_db()
    async def test_first_settings_read_cannot_overwrite_concurrent_writer(self):
        connect = database.connect_db
        injected = False
        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor
            async def fetchone(self):
                nonlocal injected
                row = await self.cursor.fetchone()
                if row is None and not injected:
                    injected = True
                    await database.save_user_settings(1, {'show_tags': False, 'quality_mode': 'original'})
                return row
        class Connection:
            def __init__(self, db):
                self.db = db
            def __getattr__(self, key):
                return getattr(self.db, key)
            async def execute(self, sql, *args):
                cursor = await self.db.execute(sql, *args)
                return Cursor(cursor) if sql.strip().startswith('SELECT * FROM user_settings') and not injected else cursor
        @asynccontextmanager
        async def interleave():
            async with connect() as db:
                yield Connection(db)
        with patch.object(database, 'connect_db', interleave):
            settings = await database.get_user_settings(1)
        self.assertFalse(settings['show_tags'])
        self.assertEqual(settings['quality_mode'], 'original')

    async def test_subscription_changed_during_rate_wait_blocks_real_request(self):
        await database.add_subscription(1, 'tag', 60)
        post = {'id': 42, 'file_url': 'https://example.test/a.jpg'}
        telegram_bot = SimpleNamespace(send_photo=AsyncMock())
        async def pause_in_wait(chat_id):
            await database.pause_all_active_subscriptions(1, 60)
            return True
        with (
            patch.object(bot, 'is_recipient_allowed', return_value=True),
            patch.object(bot.search_service, 'select', AsyncMock(return_value=post)),
            patch.object(bot_state, 'DB_PATH', database.DB_PATH),
            patch.object(bot.telegram_rate_limiter, 'wait_for_slot', side_effect=pause_in_wait),
        ):
            sent = await bot.process_one_subscription(SimpleNamespace(bot=telegram_bot), (1, 'tag', 60, 0))
            await bot_state.flush_callback_payloads()
        self.assertFalse(sent)
        telegram_bot.send_photo.assert_not_awaited()
    async def test_subscription_independent_patches_survive_concurrency(self):
        await database.add_subscription(1, 'tag', 60)
        await asyncio.gather(
            database.update_subscription_options(1, 'tag', {'rating_filter': 's'}),
            database.update_subscription_options(1, 'tag', {'media_type': 'videos'}),
        )
        options = await database.get_subscription_options(1, 'tag')
        self.assertEqual(options['rating_filter'], 's')
        self.assertEqual(options['media_type'], 'videos')

    async def test_subscription_patch_rejects_unknown_fields(self):
        await database.add_subscription(1, 'tag', 60)
        with self.assertRaises(ValueError):
            await database.update_subscription_options(1, 'tag', {'processing_token': 'evil'})

    async def test_wrong_shape_settings_are_defaults(self):
        await database.get_user_settings(1)
        await database.add_subscription(1, 'tag', 60)
        for raw in ('null', '[]', '123', '"text"'):
            async with database.connect_db() as db:
                await db.execute('UPDATE user_settings SET settings_json=?', (raw,))
                await db.execute('UPDATE subscriptions SET settings_json=?', (raw,))
                await db.commit()
            self.assertEqual((await database.get_user_settings(1))['quality_mode'], 'auto')
            self.assertEqual(await database.get_subscription_options(1, 'tag'), {'digest_mode': 'instant'})
            await database.save_user_settings(1, {'media_type': 'videos'})
            self.assertEqual((await database.get_user_settings(1))['media_type'], 'videos')

    async def test_delete_subscription_clears_associated_cache_and_saved_posts(self):
        await database.add_subscription(1, 'tag', 60)
        post = {'id': 42, 'file_url': 'https://example.test/42.jpg', 'tags': 'tag'}
        await database.add_favorite(1, post)
        await database.add_subscription_post(1, 'tag', post)
        await database.replace_subscription_cache(1, 'tag', [post])
        await database.remove_subscription(1, 'tag')
        async with database.connect_db() as db:
            for table in ('subscription_posts', 'subscription_cache', 'subscription_digest_queue'):
                row = await (await db.execute(f'SELECT count(*) FROM {table} WHERE user_id=1 AND query="tag"')).fetchone()
                self.assertEqual(row[0], 0, table)
            row = await (await db.execute('SELECT count(*) FROM favorites WHERE user_id=1')).fetchone()
            self.assertEqual(row[0], 1)
