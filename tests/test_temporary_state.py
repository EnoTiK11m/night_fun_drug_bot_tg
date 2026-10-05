import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app.telegram.application as bot
from app.storage.database import CacheCleanupResult


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class TemporaryUserStateRegistryTests(unittest.IsolatedAsyncioTestCase):
    def make_registry(self):
        clock = FakeClock()
        registry = bot.TemporaryUserStateRegistry(clock=clock)
        mappings = [registry.create_mapping() for _ in range(5)]
        return clock, registry, mappings

    async def asyncSetUp(self):
        bot.user_operation_gate.reset_for_tests()
        bot.temporary_user_state.clear_all()

    async def asyncTearDown(self):
        bot.temporary_user_state.clear_all()
        bot.user_operation_gate.reset_for_tests()

    async def test_expired_user_state_is_removed(self):
        clock, registry, mappings = self.make_registry()
        mappings[0][1] = "waiting"
        clock.advance(61)
        self.assertEqual(registry.cleanup_expired(60), 1)
        self.assertNotIn(1, mappings[0])

    async def test_active_user_state_is_not_removed(self):
        clock, registry, mappings = self.make_registry()
        mappings[0][1] = "waiting"
        clock.advance(61)
        with registry.activity(1):
            self.assertEqual(registry.cleanup_expired(60), 0)
            self.assertIn(1, mappings[0])

    async def test_touch_extends_ttl(self):
        clock, registry, mappings = self.make_registry()
        mappings[0][1] = "waiting"
        clock.advance(50)
        self.assertEqual(mappings[0].get(1), "waiting")
        clock.advance(20)
        self.assertEqual(registry.cleanup_expired(60), 0)
        clock.advance(41)
        self.assertEqual(registry.cleanup_expired(60), 1)

    async def test_cancel_command_immediately_clears_all_user_state(self):
        for mapping in (
            bot.user_states,
            bot.search_builders,
            bot.pending_preset_queries,
            bot.pending_bulk_posts,
            bot.pending_subscription_options,
            bot.user_last_search_at,
        ):
            mapping[7] = "value"
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=7),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        with (
            patch.object(bot, "zip_export_manager", None),
            patch.object(bot, "build_main_menu_text", AsyncMock(return_value="menu")),
            patch.object(bot, "get_user_main_keyboard", AsyncMock(return_value=None)),
        ):
            await bot.cancel_command(update, SimpleNamespace())
        for mapping in (
            bot.user_states,
            bot.search_builders,
            bot.pending_preset_queries,
            bot.pending_bulk_posts,
            bot.pending_subscription_options,
        ):
            self.assertNotIn(7, mapping)
        self.assertEqual(bot.user_last_search_at[7], "value")

    async def test_snapshot_cleanup_tolerates_concurrent_mutation(self):
        clock, registry, mappings = self.make_registry()
        for user_id in range(100):
            mappings[0][user_id] = "waiting"
        clock.advance(100)

        async def mutate():
            for user_id in range(100, 200):
                mappings[0][user_id] = "new"
                if user_id % 5 == 0:
                    await asyncio.sleep(0)

        async def clean():
            for _ in range(20):
                registry.cleanup_expired(60)
                await asyncio.sleep(0)

        await asyncio.gather(mutate(), clean())
        self.assertTrue(all(user_id in mappings[0] for user_id in range(100, 200)))

    async def test_cleanup_removes_user_from_all_related_mappings(self):
        clock, registry, mappings = self.make_registry()
        for mapping in mappings:
            mapping[3] = object()
        clock.advance(61)
        self.assertEqual(registry.cleanup_expired(60), 1)
        self.assertTrue(all(3 not in mapping for mapping in mappings))


class MaintenanceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot.user_operation_gate.reset_for_tests()

    async def asyncTearDown(self):
        task = bot.maintenance_task
        if task is not None and not task.done():
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        bot.maintenance_task = None
        bot.temporary_user_state.clear_all()
        bot.user_operation_gate.reset_for_tests()

    async def test_shutdown_finishes_maintenance_task_and_clears_state(self):
        bot.temporary_user_state.clear_all()
        bot.user_states[1] = "waiting"
        bot.maintenance_task = asyncio.create_task(asyncio.Event().wait())
        old_values = (
            bot.subscription_task,
            bot.heartbeat_task,
            bot.tag_translation_task,
            bot.zip_export_manager,
        )
        bot.subscription_task = None
        bot.heartbeat_task = None
        bot.tag_translation_task = None
        bot.zip_export_manager = None
        try:
            with (
                patch.object(bot.tag_translation_service, "close", AsyncMock()),
                patch.object(bot.api, "close", AsyncMock()),
            ):
                await bot.post_shutdown(SimpleNamespace())
            self.assertIsNone(bot.maintenance_task)
            self.assertEqual(bot.user_states, {})
        finally:
            (
                bot.subscription_task,
                bot.heartbeat_task,
                bot.tag_translation_task,
                bot.zip_export_manager,
            ) = old_values

    async def test_sqlite_cleanup_error_does_not_stop_maintenance_loop(self):
        clock = FakeClock()
        calls = 0
        slept_before_cleanup = False

        async def sleep(delay):
            nonlocal slept_before_cleanup
            slept_before_cleanup = True
            clock.advance(max(delay, 0.01))
            await asyncio.sleep(0)

        async def cleanup():
            nonlocal calls
            self.assertTrue(slept_before_cleanup)
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary database error")
            return CacheCleanupResult(1, 2, 3, 4, 5.0)

        before_errors = bot.cache_cleanup_errors
        task = asyncio.create_task(
            bot.maintenance_loop(
                cache_cleanup=cleanup,
                sleep=sleep,
                clock=clock,
                cache_interval_seconds=1,
                state_interval_seconds=100,
            )
        )
        for _ in range(100):
            if calls >= 2:
                break
            await asyncio.sleep(0)
        self.assertGreaterEqual(calls, 2)
        self.assertFalse(task.done())
        self.assertEqual(bot.cache_cleanup_errors, before_errors + 1)
        self.assertEqual(bot.cache_cleanup_last_deleted, 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
