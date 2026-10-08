"""Real temp SQLite + installed worker: phase attribution and resource ownership."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts import diagnose_sqlite_close as harness


class SQLiteCloseHarnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.initial_threads = set(threading.enumerate())

    async def asyncTearDown(self):
        workers = [t for t in threading.enumerate() if t not in self.initial_threads
                   and ('harness-worker' in t.name or '(_connection_worker_thread)' in t.name)]
        self.assertEqual(workers, [], 'A disposable SQLite worker outlived its test')

    async def case(self, scenario='E1', **kwargs):
        return (await asyncio.wait_for(harness.run_scenario(scenario, **kwargs), 5))[0]

    async def test_phase_order_actual_worker_ids_and_total_without_double_count(self):
        record = await self.case()
        stamps, threads = record['timestamps_ns'], record['thread_ids']
        phases = ['close_await_start','close_enqueue','close_worker_start','native_close_start',
                  'sqlite_call_start','sqlite_call_end','native_close_end','close_post_request',
                  'close_future_complete','close_coroutine_resume','stop_enqueue','stop_worker_start',
                  'stop_function_end','stop_post_request','stop_future_complete','close_await_end']
        self.assertEqual([stamps[k] for k in phases], sorted(stamps[k] for k in phases))
        self.assertEqual(threads['sqlite_call_start'], threads['stop_worker_start'])
        self.assertNotEqual(threads['sqlite_call_start'], threads['close_future_complete'])
        self.assertEqual(threads['close_enqueue'], threads['close_coroutine_resume'])
        metrics = record['metrics']
        self.assertAlmostEqual(metrics['total_close_await_ms'],
                               (stamps['close_await_end']-stamps['close_await_start'])/1e6)
        # Disjoint observed intervals leave small unassigned instrumentation gaps.
        disjoint = ['queue_wait_ms','native_close_ms','worker_completion_delivery_ms',
                    'close_resume_ms','stop_queue_wait_ms','stop_ack_ms','stop_resume_ms']
        self.assertLessEqual(sum(metrics[k] for k in disjoint), metrics['total_close_await_ms'])
        self.assertLess(stamps['worker_exit'], stamps['close_await_end'] + 2_000_000_000)
        self.assertFalse(record['worker_alive'])

    async def test_native_injection_is_not_reported_as_queue_or_loop_delay(self):
        record = await self.case('E7', faults=harness.Faults(native=.08))
        metrics = record['metrics']
        self.assertGreaterEqual(metrics['native_close_ms'], 70)
        self.assertLess(metrics['sqlite_call_ms'], metrics['native_close_ms']/2)
        self.assertLess(metrics['queue_wait_ms'], metrics['native_close_ms']/2)
        self.assertLess(record['loop_lag']['max_lag_ms'], metrics['native_close_ms']*.8)
        self.assertGreaterEqual(metrics['total_close_await_ms'], metrics['native_close_ms'])

    async def test_queue_injection_does_not_become_native_close(self):
        metrics = (await self.case('E8', faults=harness.Faults(queue=.08)))['metrics']
        self.assertGreaterEqual(metrics['queue_wait_ms'], 70)
        self.assertLess(metrics['native_close_ms'], metrics['queue_wait_ms']/2)

    async def test_loop_resume_delay_has_fast_native_close_and_lag_spike(self):
        record = await self.case('E9', faults=harness.Faults(resume=.08))
        metrics = record['metrics']
        self.assertGreaterEqual(metrics['close_resume_ms'], 70)
        self.assertLess(metrics['native_close_ms'], metrics['close_resume_ms']/2)
        self.assertGreaterEqual(record['loop_lag']['max_lag_ms'], 40)
        self.assertGreaterEqual(record['loop_lag']['spike_count'], 1)

    async def test_completion_delivery_is_separate_from_coroutine_resume(self):
        metrics = (await self.case(faults=harness.Faults(delivery=.08)))['metrics']
        self.assertGreaterEqual(metrics['worker_completion_delivery_ms'], 70)
        self.assertLess(metrics['close_resume_ms'], metrics['worker_completion_delivery_ms']/2)
        self.assertLess(metrics['native_close_ms'], metrics['worker_completion_delivery_ms']/2)

    async def test_stop_ack_delay_separate_from_native_close(self):
        metrics = (await self.case('E10', faults=harness.Faults(stop=.08)))['metrics']
        self.assertGreaterEqual(metrics['stop_ack_ms'], 70)
        self.assertLess(metrics['native_close_ms'], metrics['stop_ack_ms']/2)
        self.assertGreaterEqual(metrics['total_close_await_ms'], metrics['stop_ack_ms'])

    async def test_mixed_faults_remain_separate_without_double_count(self):
        metrics = (await self.case(faults=harness.Faults(queue=.04, native=.04, resume=.04, stop=.04)))['metrics']
        for phase in ('queue_wait_ms','native_close_ms','close_resume_ms','stop_ack_ms'):
            self.assertGreaterEqual(metrics[phase], 30, phase)
        self.assertLess(metrics['sqlite_call_ms'], metrics['native_close_ms']/2)
        disjoint = ['queue_wait_ms','native_close_ms','worker_completion_delivery_ms',
                    'close_resume_ms','stop_queue_wait_ms','stop_ack_ms','stop_resume_ms']
        self.assertLessEqual(sum(metrics[k] for k in disjoint), metrics['total_close_await_ms'])

    async def test_real_cache_batches_concurrency_and_reopen_preserve_state(self):
        self.assertEqual((await self.case('E4'))['row_count'], 250)
        concurrent = await harness.run_scenario('E5', concurrency=4, lag=False)
        self.assertEqual([r['row_count'] for r in concurrent], [1]*4)
        self.assertEqual(len({r['thread_ids']['sqlite_call_start'] for r in concurrent}), 4)
        reopened = await harness.run_scenario('E6', lag=False)
        self.assertEqual([r['row_count'] for r in reopened], [1,2,3])
        self.assertTrue(all(not r['worker_alive'] for r in concurrent + reopened))

    async def test_original_exception_identity_is_preserved_and_worker_stops(self):
        error = sqlite3.OperationalError('SECRET /private/path')
        with self.assertRaises(sqlite3.OperationalError) as caught:
            await self.case(faults=harness.Faults(close_error=error))
        self.assertIs(caught.exception, error)

    async def test_cancel_during_close_stops_worker_and_lag_task(self):
        started = asyncio.Event()
        original = harness.close_measured
        async def measured(connection, probe):
            asyncio.get_running_loop().call_later(.02, started.set)
            return await original(connection, probe)
        with patch.object(harness, 'close_measured', measured):
            task = asyncio.create_task(harness.run_scenario('E7', faults=harness.Faults(native=.15)))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(any(t.get_name()=='sqlite-harness-loop-lag' and not t.done()
                             for t in asyncio.all_tasks()))

    async def test_cancel_while_native_opening_explicitly_closes_result(self):
        loop = asyncio.get_running_loop()
        original = harness.sqlite3.connect
        closed = []
        class Connection(sqlite3.Connection):
            def close(self):
                closed.append(threading.get_ident())
                return super().close()
        for enabled in (False, True):
            entered, release = asyncio.Event(), threading.Event()
            def delayed(*args, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                release.wait(1)
                kwargs['factory'] = Connection
                return original(*args, **kwargs)
            with tempfile.TemporaryDirectory(prefix='cancel_open_') as directory, \
                 patch.object(harness.sqlite3, 'connect', delayed):
                task = asyncio.create_task(harness.open_connection(Path(directory)/'fixture.db', enabled=enabled,
                                           probe=harness.Probe(), faults=harness.Faults()))
                await entered.wait()
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(len(closed), 2)
        self.assertTrue(all(thread != threading.get_ident() for thread in closed))

    async def test_disabled_and_enabled_use_same_sql_settings_and_committed_state(self):
        traces, states = [], []
        with tempfile.TemporaryDirectory(prefix='close_settings_') as directory:
            for enabled in (False, True):
                probe = harness.Probe()
                connection = await harness.open_connection(Path(directory)/f'{enabled}.db', enabled=enabled,
                                                          probe=probe, faults=harness.Faults())
                statements = []
                try:
                    await connection.set_trace_callback(statements.append)
                    self.assertEqual(await harness.workload(connection, 'E4'), 250)
                    state = []
                    for setting in ('journal_mode','synchronous','busy_timeout','wal_autocheckpoint','foreign_keys'):
                        state.append((await (await connection.execute('PRAGMA '+setting)).fetchone())[0])
                    states.append(state)
                    self.assertFalse(connection.in_transaction)
                finally:
                    await harness.close_measured(connection, probe)
                    await harness.join_worker(connection)
                traces.append(statements)
                with sqlite3.connect(Path(directory)/f'{enabled}.db') as raw:
                    self.assertEqual(raw.execute('SELECT COUNT(*) FROM fixture_b').fetchone()[0], 250)
                raw.close()  # sqlite3 context manager commits but doesn't close.
        self.assertEqual(traces[0], traces[1])
        self.assertEqual(states[0], states[1])
        self.assertEqual(states[0], ['wal',2,1000,1000,1])

    async def test_unknown_version_fails_closed_without_worker_or_global_patch(self):
        original_close = harness.aiosqlite.Connection.close
        original_worker = harness.core._connection_worker_thread
        with patch.object(harness.importlib.metadata, 'version', return_value='0.future'):
            with self.assertRaisesRegex(RuntimeError, 'require reviewed'):
                await self.case()
        self.assertIs(harness.aiosqlite.Connection.close, original_close)
        self.assertIs(harness.core._connection_worker_thread, original_worker)

    async def test_json_output_is_bounded_and_excludes_database_paths_and_sql(self):
        record = await self.case('E4')
        encoded = json.dumps(record, allow_nan=False)
        self.assertEqual(json.loads(encoded)['row_count'], 250)
        for text in ('SELECT','INSERT','"synthetic"','DB_PATH','sqlite_close_probe_', 'SECRET'):
            self.assertNotIn(text, encoded)
        self.assertLess(len(encoded), 10000)

    async def test_lag_task_stops_when_owner_is_cancelled(self):
        monitor = harness.LoopLag('technical-id')
        async def owner():
            try:
                await monitor.start()
                await asyncio.Event().wait()
            finally:
                await monitor.stop()
        task = asyncio.create_task(owner())
        await asyncio.sleep(.04)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(monitor.task.done())

    async def test_parameter_bounds_reject_unbounded_experiments(self):
        with self.assertRaises(ValueError):
            harness.Faults(native=1)
        with self.assertRaises(ValueError):
            await harness.run_scenario('E5', concurrency=100)
        with self.assertRaises(ValueError):
            await harness.run_experiments(iterations=10000)
        self.assertIsNone(harness.summarize([1,2,3])['p95_ms'])


class StandaloneSafetyTests(unittest.TestCase):
    def test_cli_never_imports_app_or_uses_production_db_path(self):
        with tempfile.TemporaryDirectory(prefix='close_cli_guard_') as directory:
            forbidden = Path(directory)/'PRODUCTION_MUST_NOT_OPEN.db'
            code = '''
import json, pathlib, runpy, sys
def audit(event, args):
    if event == 'sqlite3.connect':
        path = pathlib.Path(args[0])
        assert path.parent.name.startswith('sqlite_close_probe_'), 'Unexpected DB access'
sys.addaudithook(audit)
namespace = runpy.run_path('scripts/diagnose_sqlite_close.py')
result = namespace['main'](['--iterations','1','--benchmark-samples','20'])
assert not any(n == 'app' or n.startswith(('app.', 'telegram')) for n in sys.modules)
raise SystemExit(result)
'''
            completed = subprocess.run([sys.executable,'-c',code], cwd=Path(__file__).resolve().parents[1],
                                       env=dict(os.environ, DB_PATH=str(forbidden)), capture_output=True,
                                       text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(forbidden.exists())
            report = json.loads(completed.stdout)
            self.assertEqual({r['scenario'] for r in report['records']}, set(harness.SCENARIOS))
            self.assertTrue(all(not r['worker_alive'] for r in report['records']))
            self.assertEqual(report['performance']['off']['diagnostic_records'], 0)
            self.assertEqual(report['performance']['on']['diagnostic_records'], 20)
