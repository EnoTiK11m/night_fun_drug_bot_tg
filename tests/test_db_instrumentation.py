"""Real disposable SQLite: phase attribution, lifecycle and privacy contracts."""
import asyncio
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import app.storage.database as database
from app.observability import logic_trace as trace
from app.observability import db_diagnostics as diagnostics
from app.services.search import ProgressiveSearch
from app.telegram import state


class DBInstrumentationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="db_instrumentation_")
        self.stack = ExitStack()
        self.db_path = str(Path(self.directory.name) / "fixture.db")
        self.path = Path(self.directory.name) / "trace.jsonl"
        self.stack.enter_context(patch.object(database, "DB_PATH", self.db_path))
        self.stack.enter_context(patch.object(state, "DB_PATH", self.db_path))
        await database.init_db()
        trace.configure_trace(enabled=True, path=self.path, level="verbose",
                              max_bytes=10000000, queue_size=4096, secrets=[])

    async def asyncTearDown(self):
        await state.flush_callback_payloads()
        await trace.shutdown_trace()
        self.stack.close()
        self.directory.cleanup()
        self.assertEqual(diagnostics.counter_snapshot()["db_connections_active"], 0)
        self.assertEqual(diagnostics.counter_snapshot()["db_operations_active"], 0)

    async def events(self):
        await asyncio.to_thread(trace._writer.queue.join)
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    async def summaries(self):
        return [e for e in await self.events() if e["event"] in ("db.operation", "db.slow_operation")]

    async def test_connection_phase_model_and_total(self):
        async with database.connect_db(operation="fixture.read") as db:
            self.assertEqual(await (await db.execute("SELECT 42")).fetchone(), (42,))
        e = (await self.summaries())[-1]
        for key in ("connect_ms", "init_ms", "context_body_ms", "close_ms", "total_ms"):
            self.assertGreaterEqual(e[key], 0)
        self.assertGreaterEqual(e["total_ms"], sum(e[k] for k in ("connect_ms", "init_ms", "context_body_ms", "close_ms")))

    async def test_operation_label(self):
        await database.claim_due_subscription(1, "missing")
        self.assertEqual((await self.summaries())[-1]["operation"], "subscription.claim.acquire")

    async def test_background_without_user_context(self):
        async with database.connect_db(operation="background.fixture"):
            pass
        e = (await self.summaries())[-1]
        self.assertEqual(e["flow"], "db")
        # v2 distinguishes the root operation from its SQLite connection.
        self.assertNotEqual(e["trace_id"], e["connection_id"])
        self.assertTrue(e["flow_id"])
        self.assertNotIn("user_id_hash", e)

    async def test_parent_correlation_preserved(self):
        with trace.flow_context("subscription", user_id=123, query="tag") as ctx:
            async with database.connect_db():
                pass
        e = (await self.summaries())[-1]
        self.assertEqual(e["trace_id"], ctx.trace_id)
        self.assertTrue(e["user_id_hash"].startswith("u_"))

    async def test_unique_connection_and_task_metadata(self):
        for _ in range(2):
            async with database.connect_db():
                pass
        ee = await self.summaries()
        self.assertNotEqual(ee[-1]["connection_id"], ee[-2]["connection_id"])
        self.assertTrue(ee[-1]["task_id"].startswith("task_"))
        self.assertIsInstance(ee[-1]["thread_id"], int)

    async def test_execute_and_fetchone(self):
        async with database.connect_db() as db:
            cursor = await diagnostics.query(db, "fixture.read").execute("SELECT 7")
            self.assertEqual(await cursor.fetchone(), (7,))
        q = (await self.summaries())[-1]["query_timings"]["fixture.read"]
        self.assertIn("execute_ms", q)
        self.assertIn("fetchone_ms", q)

    async def test_executemany_batch_count_without_payload(self):
        async with database.connect_db() as db:
            await db.execute("CREATE TABLE fixture(value TEXT)")
            await diagnostics.query(db, "fixture.batch").executemany("INSERT INTO fixture VALUES (?)", [("private-a",), ("private-b",)])
            await db.commit()
            self.assertEqual(await (await db.execute("SELECT COUNT(*) FROM fixture")).fetchone(), (2,))
        q = (await self.summaries())[-1]["query_timings"]["fixture.batch"]
        self.assertEqual(q["batch_count"], 2)
        self.assertEqual(q["rowcount"], 2)
        self.assertNotIn("private-a", self.path.read_text(encoding="utf-8"))

    async def test_fetchall_fetchmany_and_cursor_context(self):
        async with database.connect_db() as db:
            async with db.execute("SELECT 1 UNION ALL SELECT 2") as cursor:
                self.assertEqual(await cursor.fetchmany(1), [(1,)])
                self.assertEqual(await cursor.fetchall(), [(2,)])
        e = (await self.summaries())[-1]
        self.assertIn("fetchmany_ms", e)
        self.assertIn("fetchall_ms", e)

    async def test_cursor_async_iteration(self):
        async with database.connect_db() as db:
            cursor = await db.execute("SELECT 1 UNION ALL SELECT 2")
            self.assertEqual([row async for row in cursor], [(1,), (2,)])
        self.assertIn("fetchmany_ms", (await self.summaries())[-1])

    async def test_execute_awaitable_preserves_create_task_protocol(self):
        async with database.connect_db() as db:
            cursor = await asyncio.create_task(db.execute("SELECT 19"))
            self.assertEqual(await cursor.fetchone(), (19,))

    async def test_commit_is_separate(self):
        async with database.connect_db() as db:
            await db.execute("INSERT INTO users(user_id) VALUES (23)")
            await db.commit()
        e = (await self.summaries())[-1]
        self.assertIn("commit_ms", e)
        self.assertIn("close_ms", e)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT user_id FROM users WHERE user_id=23").fetchone(), (23,))

    async def test_explicit_begin_and_rollback_preserve_state(self):
        async with database.connect_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("INSERT INTO users(user_id) VALUES (23)")
            await db.rollback()
        e = (await self.summaries())[-1]
        self.assertIn("begin_ms", e)
        self.assertIn("rollback_ms", e)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertIsNone(conn.execute("SELECT user_id FROM users WHERE user_id=23").fetchone())

    async def test_exception_cleanup_and_error_metadata(self):
        with self.assertRaises(sqlite3.OperationalError):
            async with database.connect_db() as db:
                await db.execute("SELECT * FROM does_not_exist")
        e = (await self.summaries())[-1]
        self.assertEqual(e["error_type"], "OperationalError")
        self.assertEqual(e["db_connections_active"], 0)
        self.assertIn("close_ms", e)

    async def test_close_failure_keeps_counters_and_error_observable(self):
        with self.assertRaisesRegex(sqlite3.OperationalError, "close failure"):
            async with database.connect_db() as db:
                original_close = db.close
                async def failed_close():
                    await original_close()
                    raise sqlite3.OperationalError("close failure")
                db.close = failed_close
        e = (await self.summaries())[-1]
        self.assertEqual(e["error_type"], "OperationalError")
        self.assertEqual(e["db_connections_active"], 0)
        self.assertIn("close_ms", e)

    async def test_cancellation_cleanup(self):
        entered = asyncio.Event()
        async def work():
            async with database.connect_db():
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(work())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        e = (await self.summaries())[-1]
        self.assertEqual(e["error_type"], "CancelledError")
        self.assertEqual(e["db_connections_active"], 0)
        self.assertIn("close_ms", e)

    async def test_active_and_peak_connections(self):
        async with database.connect_db():
            async with database.connect_db():
                snapshot = diagnostics.counter_snapshot()
                self.assertEqual(snapshot["db_connections_active"], 2)
                self.assertGreaterEqual(snapshot["db_connections_peak"], 2)
        self.assertEqual(diagnostics.counter_snapshot()["db_connections_active"], 0)

    async def test_operation_counter_during_await_and_failure(self):
        d = diagnostics.Diagnostics("fixture.wait")
        started, release = asyncio.Event(), asyncio.Event()
        async def fail():
            started.set()
            await release.wait()
            raise ValueError("private-bind")
        task = asyncio.create_task(d.call("execute", fail))
        await started.wait()
        self.assertEqual(diagnostics.counter_snapshot()["db_operations_active"], 1)
        self.assertGreaterEqual(diagnostics.counter_snapshot()["db_operations_peak"], 1)
        release.set()
        with self.assertRaises(ValueError):
            await task
        self.assertEqual(diagnostics.counter_snapshot()["db_operations_active"], 0)
        self.assertNotIn("private-bind", json.dumps(await self.events()))

    async def simulate_phase(self, phase, seconds):
        clock = SimpleNamespace(value=10.0)
        original_connect = database.aiosqlite.connect
        async def connect(*args, **kwargs):
            db = await original_connect(*args, **kwargs)
            original = getattr(db, phase)
            async def delayed():
                clock.value += seconds
                return await original()
            setattr(db, phase, delayed)
            return db
        original_diagnostic = diagnostics.Diagnostics
        with patch.object(database.aiosqlite, "connect", connect), patch.object(database, "Diagnostics", side_effect=lambda operation=None: original_diagnostic(operation, clock=lambda: clock.value)):
            async with database.connect_db(operation="fixture.slow") as db:
                if phase == "commit":
                    await db.commit()
        return (await self.summaries())[-1]

    async def test_simulated_slow_close_attribution(self):
        e = await self.simulate_phase("close", 1.5)
        self.assertEqual(e["close_ms"], 1500)
        self.assertEqual(e["total_ms"], 1500)
        close = [e for e in await self.events() if e["event"] == "db.connection.close.slow"][-1]
        self.assertEqual(close["close_ms"], 1500)

    async def test_simulated_slow_commit_attribution(self):
        e = await self.simulate_phase("commit", 2.0)
        self.assertEqual(e["commit_ms"], 2000)
        self.assertEqual(e["close_ms"], 0)
        slow = [e for e in await self.events() if e["event"] == "db.phase.slow" and e["phase"] == "commit"][-1]
        self.assertTrue(slow["slow"])

    async def test_normal_level_filters_fast_phases(self):
        trace.configure_trace(enabled=True, path=self.path, level="normal", secrets=[])
        async with database.connect_db():
            pass
        # Recovery is intentionally normal even when the successful phases are verbose.
        self.assertEqual([e for e in await self.events() if e['event'] != 'db.recovered'], [])

    async def test_disabled_trace_keeps_database_behavior(self):
        await trace.shutdown_trace()
        async with database.connect_db() as db:
            self.assertEqual(await (await db.execute("SELECT 8")).fetchone(), (8,))
        trace.configure_trace(enabled=True, path=self.path, level="verbose", secrets=[])
        self.assertEqual(await self.events(), [])

    async def test_bind_values_and_raw_processing_token_absent(self):
        await database.add_subscription(1, "tag", 30)
        async with database.connect_db() as db:
            await db.execute("UPDATE subscriptions SET processing_token=?,processing_until=datetime('now','+5 minutes')", ("PRIVATE_PROCESSING_VALUE",))
            await db.commit()
        self.assertTrue(await database.renew_subscription_claim(1, "tag", "PRIVATE_PROCESSING_VALUE"))
        raw = json.dumps(await self.events())
        self.assertNotIn("PRIVATE_PROCESSING_VALUE", raw)
        self.assertNotIn("UPDATE subscriptions SET", raw)

    async def test_callback_sync_phase_timings_and_persistence(self):
        await asyncio.to_thread(state._store_callback_payload_db, "fixture", "private-token", "private-payload", time.time(), db_path=self.db_path)
        e = (await self.summaries())[-1]
        self.assertEqual(e["operation"], "callback.persist")
        for key in ("connect_ms", "init_ms", "schema_ms", "execute_ms", "commit_ms", "close_ms", "total_ms"):
            self.assertIn(key, e)
        self.assertEqual(await asyncio.to_thread(state._get_callback_payload_db, "fixture", "private-token", db_path=self.db_path), "private-payload")
        self.assertNotIn("private-payload", json.dumps(await self.events()))
        state.callback_payloads.pop(("fixture", "private-token"), None)

    async def test_callback_queue_wait_is_observed(self):
        state._database_job(state._store_callback_payload_db, "fixture", "queued", "private-payload", time.time())
        await state.flush_callback_payloads()
        e = [e for e in await self.summaries() if e["operation"] == "callback.persist"][-1]
        self.assertIn("queue_wait_ms", e)
        self.assertGreaterEqual(e["queue_wait_ms"], 0)

    async def test_cache_replace_breakdown_and_rows(self):
        posts = [{"id": n, "file_url": "https://fixture.test/private-url", "tags": "private-tags"} for n in (1, 2)]
        result = await database.replace_subscription_cache(1, "tag", posts)
        self.assertEqual(result, {"api": 2, "new": 2, "total": 2})
        e = (await self.summaries())[-1]
        self.assertEqual(e["operation"], "subscription.cache.replace")
        self.assertEqual(e["posts_count"], 2)
        self.assertEqual(e["batch_count"], 2)
        self.assertEqual(set(e["query_timings"]), {"cache.exists", "cache.subscription.upsert.batch", "cache.post.upsert.batch", "cache.count"})
        self.assertIn("commit_ms", e)
        self.assertIn("close_ms", e)
        self.assertNotIn("private-url", json.dumps(await self.events()))
        # Preserve the pre-existing iterable input contract, without consuming
        # a generator a second time just to collect diagnostic counts.
        self.assertEqual(await database.replace_subscription_cache(1, "tag", iter(posts)),
                         {"api": 2, "new": 0, "total": 2})
        self.assertEqual((await self.summaries())[-1]["posts_count"], 2)

    async def test_history_ack_breakdown_and_dedup_state(self):
        await database.add_subscription(1, "tag", 30)
        async with database.connect_db() as db:
            await db.execute("INSERT INTO query_progress(kind,user_id,query,signature) VALUES ('subscription',1,'tag','fixture')")
            await db.commit()
        await ProgressiveSearch(None).delivered(1, "tag", {"id": 42}, subscription=True)
        e = (await self.summaries())[-1]
        self.assertEqual(e["operation"], "subscription.history.ack")
        self.assertEqual(set(e["query_timings"]), {"history.begin", "subscription.global_history.insert", "subscription.global_history.retention", "progress.lookup", "progress.update", "history.insert.query"})
        self.assertIn("commit_ms", e)
        self.assertIn("close_ms", e)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT post_id FROM subscription_user_delivery_history WHERE user_id=1").fetchall(), [(42,)])
            self.assertEqual(conn.execute("SELECT high_water FROM query_progress WHERE user_id=1").fetchone(), (42,))


if __name__ == "__main__":
    unittest.main()
