"""Disposable-only aiosqlite 0.22.1 close probe; never imported by the app.

Per-instance experimental adapters observe the installed worker and inherited
Connection.close, not a reimplementation of their lifecycle. No global patches,
application imports, DB_PATH, network calls, or user-supplied database paths.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from dataclasses import dataclass, field
import importlib.metadata
import json
from pathlib import Path
from queue import SimpleQueue
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import uuid

import aiosqlite
from aiosqlite import core


SCENARIOS = tuple(f'E{i}' for i in range(1, 11))
MAX_FAULT_SECONDS = 0.25
JOIN_SECONDS = 2.0


@dataclass(frozen=True)
class Faults:
    native: float = 0
    queue: float = 0
    resume: float = 0
    stop: float = 0
    delivery: float = 0
    close_error: Exception | None = None

    def __post_init__(self):
        if any(not 0 <= x <= MAX_FAULT_SECONDS for x in
               (self.native, self.queue, self.resume, self.stop, self.delivery)):
            raise ValueError('Fault delays must be bounded to 0..0.25 seconds')


@dataclass
class Probe:
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    stamps: dict = field(default_factory=dict)
    threads: dict = field(default_factory=dict)

    def mark(self, name):
        # One writer per named boundary; threads share perf_counter_ns's clock.
        self.stamps[name] = time.perf_counter_ns()
        self.threads[name] = threading.get_ident()

    def elapsed(self, start, end):
        if start not in self.stamps or end not in self.stamps:
            return None
        return (self.stamps[end] - self.stamps[start]) / 1_000_000

    def metrics(self):
        boundaries = {
            'queue_wait_ms': ('close_enqueue', 'close_worker_start'),
            'native_close_ms': ('native_close_start', 'native_close_end'),
            'sqlite_call_ms': ('sqlite_call_start', 'sqlite_call_end'),
            'worker_completion_delivery_ms': ('close_post_request', 'close_future_complete'),
            'close_resume_ms': ('close_future_complete', 'close_coroutine_resume'),
            'stop_queue_wait_ms': ('stop_enqueue', 'stop_worker_start'),
            'stop_ack_ms': ('stop_worker_start', 'stop_future_complete'),
            'stop_resume_ms': ('stop_future_complete', 'close_await_end'),
            'total_close_await_ms': ('close_await_start', 'close_await_end'),
        }
        return {key: self.elapsed(*pair) for key, pair in boundaries.items()}


class LoopLag:
    """Local experiment only: 10 ms sampling, 20 ms spikes, last 16 spikes."""
    def __init__(self, correlation_id, interval=0.01, threshold=0.02):
        if not 0.005 <= interval <= 1 or not 0.005 <= threshold <= 1:
            raise ValueError('Invalid lag sampling bounds')
        self.correlation_id = correlation_id
        self.interval, self.threshold = interval, threshold
        self.maximum_ms = 0.0
        self.spike_count = 0
        self.spikes = deque(maxlen=16)
        self.task = None

    async def _run(self):
        while True:
            expected = time.perf_counter_ns() + int(self.interval * 1e9)
            await asyncio.sleep(self.interval)
            actual = time.perf_counter_ns()
            lag = max(0, actual - expected) / 1e6
            self.maximum_ms = max(self.maximum_ms, lag)
            if lag >= self.threshold * 1000:
                self.spike_count += 1
                self.spikes.append(dict(actual_ns=actual, expected_ns=expected, lag_ms=lag))
            # Reset after each tick: never run a catch-up busy loop after a stall.

    async def start(self):
        self.task = asyncio.create_task(self._run(), name='sqlite-harness-loop-lag')
        await asyncio.sleep(self.interval * 2)

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def snapshot(self):
        return dict(correlation_id=self.correlation_id, max_lag_ms=self.maximum_ms,
                    spike_count=self.spike_count, spikes=list(self.spikes))


class _LoopProxy:
    def __init__(self, loop, phase, probe, faults):
        self.loop, self.phase, self.probe, self.faults = loop, phase, probe, faults

    def call_soon_threadsafe(self, callback, *args):
        # Timestamp is the call request boundary, not an inferred internal post.
        if self.phase == 'close' and self.faults.delivery:
            self.loop.call_soon_threadsafe(time.sleep, self.faults.delivery)
        self.probe.mark(self.phase + '_post_request')
        return self.loop.call_soon_threadsafe(callback, *args)


class _FutureProxy:
    """Worker-facing proxy only; the caller awaits its original asyncio Future."""
    def __init__(self, future, phase, probe, faults):
        self.future, self.phase, self.probe, self.faults = future, phase, probe, faults

    def get_loop(self):
        return _LoopProxy(self.future.get_loop(), self.phase, self.probe, self.faults)

    def done(self):
        return self.future.done()

    def set_result(self, result):
        if self.phase == 'close' and self.faults.resume:
            # Put a blocker before the continuation scheduled by set_result.
            # It is intentionally blocking and exists only in this harness.
            self.future.get_loop().call_soon(time.sleep, self.faults.resume)
        self.probe.mark(self.phase + '_future_complete')
        self.future.set_result(result)

    def set_exception(self, error):
        self.probe.mark(self.phase + '_future_complete')
        self.future.set_exception(error)


class _QueueProbe:
    def __init__(self, probe, faults):
        self.queue = SimpleQueue()
        self.probe, self.faults = probe, faults

    def put_nowait(self, item):
        future, function = item
        name = getattr(getattr(function, 'func', function), '__name__', '')
        phase = 'stop' if name == 'close_and_stop' else 'close' if name == 'close' else None
        if phase is None:
            self.queue.put_nowait((future, function, None))
            return
        self.probe.mark(phase + '_enqueue')
        def observed():
            if phase == 'close':
                self.probe.mark('native_close_start')
                try:
                    return function()
                finally:
                    self.probe.mark('native_close_end')
            if self.faults.stop:
                time.sleep(self.faults.stop)
            try:
                return function()
            finally:
                self.probe.mark('stop_function_end')
        proxy = _FutureProxy(future, phase, self.probe, self.faults) if future else None
        self.queue.put_nowait((proxy, observed, phase))

    def get(self):
        future, function, phase = self.queue.get()
        if phase:
            self.probe.mark(phase + '_worker_start')
        return future, function


class _SQLiteProbe(sqlite3.Connection):
    """Harness-only factory; injected native delay is explicitly synthetic."""
    probe: Probe
    faults: Faults

    def close(self):
        # Called by the *installed* aiosqlite worker, in its owning thread.
        if self.faults.native:
            time.sleep(self.faults.native)
        self.probe.mark('sqlite_call_start')
        try:
            result = super().close()
        finally:
            self.probe.mark('sqlite_call_end')
        # Close the real resource first, then test propagation of the same object.
        if self.faults.close_error is not None:
            raise self.faults.close_error
        return result


class ExperimentalConnection(aiosqlite.Connection):
    """Private internals confined here; inherited close/stop and stock worker."""
    def __init__(self, connector, probe, faults):
        super().__init__(connector, iter_chunk_size=64)
        self.probe = probe
        self._tx = _QueueProbe(probe, faults)
        def worker():
            try:
                core._connection_worker_thread(self._tx)
            finally:
                probe.mark('worker_exit')
        self._thread = threading.Thread(target=worker, name='sqlite-close-harness-worker')

    async def _execute(self, fn, *args, **kwargs):
        closing = getattr(fn, '__name__', '') == 'close'
        try:
            return await super()._execute(fn, *args, **kwargs)
        finally:
            if closing:
                self.probe.mark('close_coroutine_resume')


def verify_version():
    version = importlib.metadata.version('aiosqlite')
    if version != '0.22.1':
        raise RuntimeError('Experimental adapters require reviewed aiosqlite 0.22.1')
    return version


async def open_connection(path, *, enabled, probe, faults):
    if not enabled:
        if faults != Faults():
            raise ValueError('Faults require experimental diagnostics')
        pending = aiosqlite.connect(path, timeout=1)
    else:
        verify_version()
        def connector():
            connection = sqlite3.connect(path, timeout=1, factory=_SQLiteProbe)
            connection.probe, connection.faults = probe, faults
            return connection
        pending = ExperimentalConnection(connector, probe, faults)
    async def opening():
        return await pending
    task = asyncio.create_task(opening())
    try:
        return await asyncio.shield(task)
    except BaseException as original_error:
        # Do not abandon an in-flight native connect: a cancelled connect Future
        # can discard its SQLite result before Connection takes ownership of it.
        # Finish opening, then explicitly close in the owning worker and join.
        try:
            await asyncio.shield(task)
        except BaseException:
            pass  # Installed _connect queues stop on a genuine opening failure.
        try:
            if pending._connection is not None:
                await asyncio.shield(pending.close())
            if pending._thread.ident is not None:
                await asyncio.shield(join_worker(pending))
        except BaseException as cleanup_error:
            original_error.add_note('Harness open cleanup failed: ' + type(cleanup_error).__name__)
        raise


async def join_worker(connection):
    # Joining is cleanup *after* the close await timer, never a production hook.
    await asyncio.to_thread(connection._thread.join, JOIN_SECONDS)
    if connection._thread.is_alive():
        raise RuntimeError('Harness worker did not terminate within cleanup budget')


async def close_measured(connection, probe):
    probe.mark('close_await_start')
    try:
        await connection.close()
    finally:
        probe.mark('close_await_end')


async def prepare_schema(connection):
    # Disposable workload settings only. Production defaults are not read or
    # modified; short 1 s busy timeout bounds synthetic concurrent contention.
    await connection.execute('PRAGMA journal_mode=WAL')
    await connection.execute('PRAGMA foreign_keys=ON')
    await connection.execute('CREATE TABLE IF NOT EXISTS fixture_a (id INTEGER PRIMARY KEY, value TEXT)')
    await connection.execute('CREATE TABLE IF NOT EXISTS fixture_b (id INTEGER PRIMARY KEY, value TEXT)')
    await connection.commit()


async def workload(connection, scenario):
    if scenario == 'E1' or scenario in ('E7', 'E8', 'E9', 'E10'):
        return 0
    if scenario == 'E2':
        return (await (await connection.execute('SELECT 1')).fetchone())[0]
    await prepare_schema(connection)
    if scenario == 'E4':
        await (await connection.execute('SELECT id FROM fixture_a')).fetchall()
        rows = [(i, 'synthetic') for i in range(250)]
        await connection.executemany('INSERT OR REPLACE INTO fixture_a VALUES (?, ?)', rows)
        await connection.executemany('INSERT OR REPLACE INTO fixture_b VALUES (?, ?)', rows)
        count = (await (await connection.execute('SELECT COUNT(*) FROM fixture_a')).fetchone())[0]
        other = (await (await connection.execute('SELECT COUNT(*) FROM fixture_b')).fetchone())[0]
        if (count, other) != (250, 250):
            raise AssertionError('Synthetic cache workload state mismatch')
    elif scenario == 'E6':
        await connection.execute("INSERT INTO fixture_a SELECT COALESCE(MAX(id), 0)+1, 'synthetic' FROM fixture_a")
        count = (await (await connection.execute('SELECT COUNT(*) FROM fixture_a')).fetchone())[0]
    else:
        await connection.execute('INSERT OR REPLACE INTO fixture_a VALUES (1, ?)', ('synthetic',))
        count = (await (await connection.execute('SELECT COUNT(*) FROM fixture_a')).fetchone())[0]
    await connection.commit()
    return count


async def enqueue_blocker(connection, seconds):
    loop = asyncio.get_running_loop()
    entered, finished = loop.create_future(), loop.create_future()
    release = threading.Event()
    def blocker():
        loop.call_soon_threadsafe(core.set_result, entered, None)
        if not release.wait(1):
            raise TimeoutError('Harness queue gate was not released')
    connection._tx.put_nowait((finished, blocker))
    try:
        await entered
    except BaseException:
        release.set()
        await asyncio.gather(finished, return_exceptions=True)
        raise
    handle = loop.call_later(seconds, release.set)
    return release, handle, finished


async def _run_one(directory, scenario, index, *, enabled=True, faults=Faults(), lag=True):
    if scenario not in SCENARIOS:
        raise ValueError('Unknown bounded scenario')
    probe = Probe()
    monitor = LoopLag(probe.correlation_id) if lag else None
    connection = None
    gate = None
    primary_error = None
    operation_start = time.perf_counter_ns()
    try:
        if monitor:
            await monitor.start()
        connection = await open_connection(Path(directory)/f'case_{index}.db', enabled=enabled, probe=probe, faults=faults)
        count = await workload(connection, scenario)
        if faults.queue:
            gate = await enqueue_blocker(connection, faults.queue)
        await close_measured(connection, probe)
        operation_end = time.perf_counter_ns()
        if monitor:
            await asyncio.sleep(monitor.interval * 2)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if gate:
            gate[0].set()
            gate[1].cancel()
            await asyncio.gather(gate[2], return_exceptions=True)
        try:
            if connection:
                # Shield cleanup against cancellation of the experiment's task.
                # Inherited close's finally queues stop even on cancelled await.
                if connection._connection is not None:
                    await asyncio.shield(connection.close())
                await asyncio.shield(join_worker(connection))
        except BaseException as cleanup_error:
            if primary_error is None:
                raise
            primary_error.add_note('Harness cleanup failed: ' + type(cleanup_error).__name__)
        finally:
            if monitor:
                await monitor.stop()
    lag_snapshot = monitor.snapshot() if monitor else None
    record = dict(scenario=scenario, diagnostic_enabled=enabled, correlation_id=probe.correlation_id,
                  timestamps_ns=probe.stamps, thread_ids=probe.threads, metrics=probe.metrics(),
                  loop_lag=lag_snapshot, row_count=count, worker_alive=connection._thread.is_alive(),
                  operation_ms=(operation_end-operation_start)/1e6,
                  synthetic_faults_ms={k:getattr(faults,k)*1000 for k in ('native','queue','resume','stop','delivery')})
    # Only fixed names, numbers and technical IDs; no paths/SQL/error messages.
    return record


async def run_scenario(scenario, *, enabled=True, faults=Faults(), lag=True, concurrency=3):
    if not 1 <= concurrency <= 4:
        raise ValueError('Concurrency must be 1..4')
    with tempfile.TemporaryDirectory(prefix='sqlite_close_probe_') as directory:
        if scenario == 'E6':
            return [await _run_one(directory, scenario, 0, enabled=enabled, faults=faults, lag=lag)
                    for _ in range(3)]
        if scenario == 'E5':
            # Initialize WAL/schema once before releasing competing writers;
            # simultaneous first-time journal-mode changes are a different risk.
            initial = await open_connection(Path(directory)/'case_0.db', enabled=False,
                                            probe=Probe(), faults=Faults())
            try:
                await prepare_schema(initial)
            finally:
                await initial.close()
                await join_worker(initial)
            # Shared disposable file: bounded writers really contend. A start
            # barrier aligns tasks; this is not a pool or a production DB lock.
            barrier = asyncio.Barrier(concurrency)
            async def one():
                await barrier.wait()
                return await _run_one(directory, scenario, 0, enabled=enabled, faults=faults, lag=lag)
            tasks = [asyncio.create_task(one()) for _ in range(concurrency)]
            try:
                return await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        return [await _run_one(directory, scenario, 0, enabled=enabled, faults=faults, lag=lag)]


def summarize(values):
    ordered = sorted(values)
    return dict(samples=len(values), median_ms=statistics.median(values),
                p95_ms=ordered[(95*len(ordered)+99)//100-1] if len(ordered) >= 100 else None,
                max_ms=max(values))


async def run_experiments(iterations=3, samples=100):
    if not 1 <= iterations <= 10 or not 20 <= samples <= 200:
        raise ValueError('Iterations must be 1..10 and benchmark samples 20..200')
    verify_version()
    records = []
    faults_by_scenario = {'E7':Faults(native=.08), 'E8':Faults(queue=.08),
                          'E9':Faults(resume=.08), 'E10':Faults(stop=.08)}
    for scenario in SCENARIOS:
        for _ in range(iterations):
            records.extend(await run_scenario(scenario, faults=faults_by_scenario.get(scenario, Faults())))
    # Alternate off/on order each pair to reduce simple warm-up/order bias.
    # Same workload/temp lifecycle, no faults and lag task OFF in both modes.
    timing = {False:[], True:[]}
    close_timing = {False:[], True:[]}
    payload = {False:0, True:0}
    events = {False:0, True:0}
    for i in range(samples):
        for enabled in ((False, True) if i%2 == 0 else (True, False)):
            record = (await run_scenario('E4', enabled=enabled, lag=False))[0]
            timing[enabled].append(record['operation_ms'])
            close_timing[enabled].append(record['metrics']['total_close_await_ms'])
            if enabled:
                payload[enabled] += len(json.dumps(record, separators=(',',':'), allow_nan=False).encode())
                events[enabled] += len(record['timestamps_ns'])
    performance = {('on' if mode else 'off'):dict(operation=summarize(timing[mode]),
                   close=summarize(close_timing[mode]), phase_boundaries=events[mode],
                   diagnostic_records=samples if mode else 0,
                   diagnostic_bytes=payload[mode]) for mode in (False, True)}
    return dict(versions=dict(python=sys.version.split()[0], aiosqlite=verify_version(), sqlite=sqlite3.sqlite_version),
                clock='perf_counter_ns; process-local monotonic, shared across threads',
                records=records, performance=performance,
                notes=['Native phase includes a synthetic sleep only for E7; sqlite_call_ms excludes it.',
                       'Worker scheduling vs backlog inside queue wait cannot be split.',
                       'No experiment diagnoses the historical production incident.'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iterations', type=int, default=3)
    parser.add_argument('--benchmark-samples', type=int, default=100)
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args(argv)
    if not 5 <= args.timeout <= 120:
        parser.error('timeout must be 5..120 seconds')
    try:
        result = asyncio.run(asyncio.wait_for(run_experiments(args.iterations, args.benchmark_samples), args.timeout))
    except Exception as error:
        # No raw exception text / SQL / paths in diagnostic output.
        print(json.dumps(dict(error_type=type(error).__name__, completed=False)), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
