"""SQLite observations only: no SQL, parameters or payloads are recorded."""
import asyncio
from collections.abc import Coroutine
from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps
import threading
import time
import uuid

from app.observability import logic_trace

_operation = ContextVar("db_operation", default="unlabelled")
_counter_lock = threading.Lock()
_counters = dict(db_connections_active=0, db_connections_peak=0,
                 db_operations_active=0, db_operations_peak=0)


def counter_snapshot():
    with _counter_lock:
        return dict(_counters)


def count(kind, delta):
    # This protects four integers only, never a database call or await.
    with _counter_lock:
        active, peak = f"db_{kind}_active", f"db_{kind}_peak"
        _counters[active] += delta
        _counters[peak] = max(_counters[peak], _counters[active])


@contextmanager
def operation_scope(label):
    token = _operation.set(label)
    try:
        yield
    finally:
        _operation.reset(token)


def db_operation(label):
    def decorate(function):
        @wraps(function)
        async def run(*args, **kwargs):
            with operation_scope(label(*args, **kwargs) if callable(label) else label):
                return await function(*args, **kwargs)
        return run
    return decorate


def query(db, label):
    """A fixed label for the immediately following execute/executemany call.

    Consume it synchronously when the awaitable is constructed, before yielding.
    Raw fixture connections are supported without changing their API.
    """
    if hasattr(db, "_db_diagnostics"):
        db._db_diagnostics.next_query = label
    return db


class AwaitableCursor(Coroutine):
    """Preserve both await db.execute(...) and async with db.execute(...)."""
    def __init__(self, coroutine):
        self.coroutine = coroutine

    def __await__(self):
        return self.coroutine.__await__()

    def send(self, value):
        return self.coroutine.send(value)

    def throw(self, *args):
        return self.coroutine.throw(*args)

    def close(self):
        return self.coroutine.close()

    async def __aenter__(self):
        self.cursor = await self.coroutine
        return self.cursor

    async def __aexit__(self, *exc):
        if hasattr(self.cursor, "fetchone"):
            await self.cursor.close()


class Diagnostics:
    def __init__(self, operation=None, *, clock=None):
        self.clock = clock or time.monotonic
        self.operation = operation or _operation.get()
        self.connection_id = uuid.uuid4().hex[:16]
        self.trace = logic_trace.current_trace() or logic_trace.TraceContext(
            flow="db", trace_id=self.connection_id)
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        # Task names can be supplied by callers and contain arbitrary values.
        self.task_id = f"task_{id(task):x}" if task else None
        self.thread_id = threading.get_ident()
        self.started = self.clock()
        self.timings = dict.fromkeys(("connect_ms", "init_ms", "context_body_ms", "close_ms"), 0.0)
        self.queries = {}
        self.next_query = None
        self.metadata = {}
        self.error_type = None

    def emit(self, event, level="verbose", **fields):
        if not logic_trace.enabled(level):
            return
        logic_trace.trace_event(event, trace=self.trace, level=level,
            connection_id=self.connection_id, operation=self.operation,
            task_id=self.task_id, thread_id=self.thread_id,
            **counter_snapshot(), **fields)

    def record(self, phase, started, label=None, error=None, **fields):
        duration = (self.clock() - started) * 1000
        if error:
            self.error_type = error
        key = phase + "_ms"
        self.timings[key] = self.timings.get(key, 0.0) + duration
        if label:
            values = self.queries.setdefault(label, {}) if label in self.queries or len(self.queries) < 20 else None
            if values is not None:
                values[key] = values.get(key, 0.0) + duration
                values.update(fields)
        self.emit("db.phase.slow" if duration > 250 else "db.phase",
                  level="normal" if duration > 250 or error else "verbose",
                  phase=phase, query_label=label, duration_ms=duration,
                  slow=duration > 1000, error_type=error, **fields)
        return duration

    async def call(self, phase, function, *args, label=None, **fields):
        started = self.clock()
        error = None
        count("operations", 1)
        try:
            return await function(*args)
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            count("operations", -1)
            self.record(phase, started, label, error, **fields)

    def instrument(self, db):
        db._db_diagnostics = self
        for method in ("execute", "executemany", "executescript", "execute_fetchall", "execute_insert"):
            original = getattr(db, method, None)
            if original is None:
                continue
            def execute(sql, *args, _method=method, _original=original, **kwargs):
                label, self.next_query = self.next_query, None
                # Never include SQL or bind values. BEGIN is a phase, not a lock timer.
                phase = "begin" if str(sql).lstrip()[:5].upper() == "BEGIN" else _method
                fields = {}
                if _method == "executemany" and args and isinstance(args[0], (list, tuple)):
                    fields["batch_count"] = len(args[0])
                async def run():
                    result = await self.call(phase, lambda: _original(sql, *args, **kwargs), label=label, **fields)
                    if hasattr(result, "fetchone"):
                        self.instrument_cursor(result, label)
                        if label in self.queries:
                            self.queries[label]["rowcount"] = result.rowcount
                    return result
                return AwaitableCursor(run())
            setattr(db, method, execute)
        for method in ("commit", "rollback"):
            original = getattr(db, method)
            async def transaction(_method=method, _original=original):
                return await self.call(_method, _original)
            setattr(db, method, transaction)
        return db

    def instrument_cursor(self, cursor, label):
        for method in ("fetchone", "fetchall", "fetchmany"):
            original = getattr(cursor, method)
            async def fetch(*args, _method=method, _original=original, **kwargs):
                return await self.call(_method, lambda: _original(*args, **kwargs), label=label)
            setattr(cursor, method, fetch)

    def sync_call(self, phase, function, *args, label=None):
        started = self.clock()
        error = None
        count("operations", 1)
        try:
            return function(*args)
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            count("operations", -1)
            self.record(phase, started, label, error)

    def finished(self, **fields):
        total = (self.clock() - self.started) * 1000
        self.emit("db.slow_operation" if total > 250 else "db.operation",
            level="normal" if total > 250 or self.error_type else "verbose", duration_ms=total, error_type=self.error_type,
            total_ms=total, **self.timings, query_timings=self.queries, **self.metadata, **fields)
        if self.timings.get("close_ms", 0) > 1000:
            self.emit("db.connection.close.slow", level="normal", total_ms=total,
                      close_ms=self.timings["close_ms"])
        return total


_callback = ContextVar("callback_db_diagnostic", default=None)


def callback_phase(phase, function, *args, **kwargs):
    diagnostic = _callback.get()
    if diagnostic is None:
        return function(*args, **kwargs)
    return diagnostic.sync_call(phase, lambda: function(*args, **kwargs),
                                label="callback." + phase)


def callback_connection_opened():
    count("connections", 1)


def callback_close(conn):
    try:
        return callback_phase("close", conn.close)
    finally:
        count("connections", -1)


def callback_operation(label):
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            diagnostic = _callback.get() or Diagnostics(label)
            diagnostic.thread_id = threading.get_ident()
            token = _callback.set(diagnostic)
            try:
                return function(*args, **kwargs)
            finally:
                diagnostic.finished()
                _callback.reset(token)
        return run
    return decorate


def callback_job(diagnostic, queued_at, function, *args, **kwargs):
    diagnostic.thread_id = threading.get_ident()
    diagnostic.record("queue_wait", queued_at)
    token = _callback.set(diagnostic)
    try:
        return function(*args, **kwargs)
    finally:
        _callback.reset(token)


def metadata(db, **counts):
    """Accept only fixed numeric diagnostic counts, never arbitrary payloads."""
    if hasattr(db, "_db_diagnostics"):
        allowed = {"posts_count", "batch_count"}
        db._db_diagnostics.metadata.update({k: v for k, v in counts.items()
                                           if k in allowed and isinstance(v, int)})
