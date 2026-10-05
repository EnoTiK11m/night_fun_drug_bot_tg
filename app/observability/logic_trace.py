"""Optional, bounded decision tracing. No application logging handlers are replaced."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, UTC
from functools import wraps
import hashlib
import inspect
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import re
import threading
import time
import traceback
import uuid
import math

logger = logging.getLogger(__name__)
_current = ContextVar('logic_trace', default=None)
_writer = None
_level = 1
_secrets = ()
_dropped = 0
LEVELS = {'minimal': 0, 'normal': 1, 'verbose': 2}
_sensitive = re.compile(r'(token|api_key|api_user_id|authorization|cookie|password|private_key|credential|headers|params|locals|\.env)', re.I)


def safe_hash(value, prefix='h_'):
    return prefix + hashlib.sha256(('logic-trace-v1:' + str(value)).encode()).hexdigest()[:12]


def sanitize(value, depth=0):
    if depth > 5:
        return '[bounded]'
    if isinstance(value, dict):
        return {str(k)[:80]: '[REDACTED]' if _sensitive.search(str(k)) or str(k) in {'user_id', 'chat_id'} else sanitize(v, depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, (list, tuple, set)):
        return [sanitize(v, depth + 1) for v in list(value)[:20]]
    if isinstance(value, str):
        value = mask_known_secrets(value, [(secret, '[REDACTED]') for secret in _secrets])
        value = re.sub(r'(?i)(?:https?://)?(?:api\.telegram\.org/)?bot\d+:[A-Za-z0-9_-]+', '[TELEGRAM_REDACTED]', value)
        value = re.sub(r'(?i)((?:api_key|access_token|password|authorization|cookie|user_id)\s*[=:]\s*)[^&\s\"\']+', r'\1[REDACTED]', value)
        value = re.sub(r'(?i)(Bearer|Basic)\s+[A-Za-z0-9+/=_\-.]+', r'\1 [REDACTED]', value)
        value = re.sub(r'https?://[^\s/@]+:[^\s/@]+@', 'https://[REDACTED]@', value)
        value = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[PRIVATE_KEY_REDACTED]', value, flags=re.S)
        return value[:12000]
    if isinstance(value, float) and not math.isfinite(value):
        return '[nonfinite]'
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return '[unsupported]'


class TraceWriter:
    def __init__(self, path, max_bytes, backup_count, retention_days, queue_size):
        self.path = Path(path)
        self.queue = queue.Queue(maxsize=max(1, queue_size))
        self.stopping = threading.Event()
        self.accepting = True
        self.options = (max_bytes, backup_count, retention_days)
        self.thread = threading.Thread(target=self.run, name='logic-trace-writer', daemon=True)
        self.thread.start()

    def accept(self, event):
        if not self.accepting:
            return False
        try:
            self.queue.put_nowait(event)
            return True
        except queue.Full:
            return False

    def run(self):
        handler = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            max_bytes, backup_count, retention_days = self.options
            cutoff = time.time() - retention_days * 86400
            for path in self.path.parent.glob(self.path.name + '.*'):
                if path.name.removeprefix(self.path.name + '.').isdigit() and path.stat().st_mtime < cutoff:
                    path.unlink()
            handler = RotatingFileHandler(self.path, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(message)s'))
            while not (self.stopping.is_set() and self.queue.empty()):
                try:
                    event = self.queue.get(timeout=.1)
                except queue.Empty:
                    continue
                try:
                    line = json.dumps(event, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
                    handler.emit(logging.LogRecord('logic_trace', logging.INFO, '', 0, line, (), None))
                except Exception:
                    logger.warning('Logic tracing writer discarded an invalid event')
                finally:
                    self.queue.task_done()
        except Exception as exc:
            self.accepting = False
            logger.warning('Logic tracing writer unavailable type=%s', type(exc).__name__)
        finally:
            if handler:
                handler.close()

    def stop(self, timeout):
        self.accepting = False
        self.stopping.set()
        self.thread.join(timeout)
        return not self.thread.is_alive()


def configure_trace(*, enabled=None, path=None, level=None, max_bytes=None, backup_count=None, retention_days=None, queue_size=2048, secrets=None):
    global _writer, _level, _secrets, _dropped
    import app.config as config
    if _writer is not None:
        _writer.stop(.5)
        _writer = None
    _dropped = 0
    enabled = config.LOGIC_TRACE_ENABLED if enabled is None else enabled
    if not enabled:
        return
    _level = LEVELS.get(level or config.LOGIC_TRACE_LEVEL, 1)
    _secrets = tuple(sorted({str(s) for s in (secrets if secrets is not None else (config.BOT_TOKEN, config.API_KEY, config.API_USER_ID)) if s}, key=len, reverse=True))
    _writer = TraceWriter(path or config.PROJECT_ROOT / 'logs' / 'logic_trace.jsonl',
        max(128, max_bytes or config.LOGIC_TRACE_MAX_BYTES),
        max(1, backup_count if backup_count is not None else config.LOGIC_TRACE_BACKUP_COUNT),
        max(1, retention_days or config.LOGIC_TRACE_RETENTION_DAYS), queue_size)


def enabled(level='minimal'):
    return _writer is not None and _writer.accepting and LEVELS.get(level, 1) <= _level


def current_trace():
    return _current.get() if _writer is not None else None


def dropped_events():
    return _dropped


@dataclass
class TraceContext:
    flow: str
    user_id: int | None = None
    query: str = ''
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_trace_id: str | None = None
    outcome: str | None = None
    fields: dict = field(default_factory=dict)


def trace_event(event, *, trace=None, level='minimal', **fields):
    global _dropped
    if not enabled(level):
        return
    ctx = trace or _current.get()
    if ctx is None:
        return
    record = {'ts': datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
        'trace_id': ctx.trace_id, 'flow': ctx.flow, 'event': event}
    if ctx.parent_trace_id:
        record['parent_trace_id'] = ctx.parent_trace_id
    if ctx.user_id is not None:
        record['user_id_hash'] = safe_hash(ctx.user_id, 'u_')
    if ctx.query:
        record['query'] = sanitize(ctx.query)[:512]
        record['query_hash'] = safe_hash(ctx.query, 'q_')
    for key in ('post_id', 'media_type', 'budget_remaining'):
        if key in ctx.fields and key not in fields:
            record[key] = sanitize(ctx.fields[key])
    record.update({k: v for k, v in sanitize(fields).items() if k not in {'trace_id', 'flow', 'ts', 'user_id_hash'}})
    # Hash fields are safe and should not be removed by broad credential-key matching.
    for key in ('claim_token_hash',):
        if key in fields:
            record[key] = fields[key] if isinstance(fields[key], str) and re.fullmatch(r'h_[0-9a-f]{12}', fields[key]) else '[REDACTED]'
    if not _writer.accept(record):
        _dropped += 1
        if _dropped & (_dropped - 1) == 0:
            logger.warning('Logic tracing queue dropped events count=%s', _dropped)


def trace_error(exc, stage, event='error'):
    if not enabled():
        return
    trace_event(event, type=type(exc).__name__, message=str(exc), stage=stage,
        traceback=''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


def outcome(value, **fields):
    ctx = current_trace()
    if ctx:
        ctx.outcome = value
        ctx.fields.update(fields)


def annotate(**fields):
    ctx = current_trace()
    if ctx:
        ctx.fields.update(fields)


def add_timing(stage, duration_ms):
    ctx = current_trace()
    if ctx:
        key = stage + '_ms'
        ctx.fields[key] = round(ctx.fields.get(key, 0) + duration_ms, 3)
        trace_event('timing.stage', level='verbose', stage=stage, duration_ms=duration_ms)


def increment(name):
    ctx = current_trace()
    if ctx:
        ctx.fields[name] = ctx.fields.get(name, 0) + 1


@contextmanager
def flow_context(flow, *, user_id=None, query='', reuse=True, **fields):
    if not enabled():
        yield None
        return
    parent = current_trace()
    if reuse and parent is not None and parent.flow == flow:
        yield parent
        return
    ctx = TraceContext(flow=flow, user_id=user_id, query=query,
        parent_trace_id=parent.trace_id if parent else None, fields=fields)
    token = _current.set(ctx)
    started = time.monotonic()
    trace_event(flow + '.start', **fields)
    if fields.get('is_more'):
        trace_event('search.more.start')
    try:
        yield ctx
    except asyncio.CancelledError:
        ctx.outcome = 'cancelled'
        raise
    except BaseException as exc:
        inferred = 'deadline' if isinstance(exc, TimeoutError) else 'validation_error' if isinstance(exc, ValueError) else 'api_error' if type(exc).__name__ == 'APITemporaryError' else 'telegram_error' if type(exc).__name__ in {'Forbidden', 'BadRequest', 'NetworkError', 'TimedOut', 'RetryAfter'} else 'error'
        ctx.outcome = ctx.outcome if ctx.outcome in {'budget_exhausted', 'deadline', 'cancelled'} else inferred
        trace_error(exc, stage=flow, event=flow + '.error')
        trace_error(exc, stage=flow)
        raise
    finally:
        if 'http_requests' in ctx.fields:
            ctx.fields['api_requests'] = ctx.fields['http_requests']
        trace_event(flow + '.finish', outcome=ctx.outcome or 'success', duration_ms=round((time.monotonic() - started) * 1000, 3), **ctx.fields)
        if fields.get('is_more'):
            trace_event('search.more.finish', outcome=ctx.outcome or 'success')
        _current.reset(token)


def traced_flow(flow, *, user_arg=None, query_arg=None, metadata=None, inherit=False):
    def decorate(function):
        signature = inspect.signature(function)
        @wraps(function)
        async def wrapped(*args, **kwargs):
            if not enabled():
                return await function(*args, **kwargs)
            if inherit and current_trace() is not None:
                return await function(*args, **kwargs)
            bound = signature.bind(*args, **kwargs).arguments
            name = flow(bound) if callable(flow) else flow
            extra = metadata(bound) if metadata else {}
            extra.setdefault('user_id', bound.get(user_arg))
            extra.setdefault('query', bound.get(query_arg, ''))
            if 'is_more' in bound:
                extra['is_more'] = bound['is_more']
            with flow_context(name, **extra) as ctx:
                result = await function(*args, **kwargs)
                if ctx and ctx.outcome is None:
                    ctx.outcome = 'empty' if (result is False or result is None) and name in {'search', 'subscription', 'rule34'} else 'success'
                    if hasattr(result, 'status'):
                        status = result.status
                        ctx.outcome = 'cancelled' if status == 'cancelled' else 'error' if status in {'failed', 'download_error'} else 'validation_error' if status == 'limit' else 'success'
                        ctx.fields['result_status'] = status
                    if hasattr(result, 'failed_ids'):
                        ctx.fields.update(failed_count=len(result.failed_ids), confirmed_count=len(result.delivered_ids), ambiguous_count=len(result.ambiguous_ids))
                        if result.failed_ids or result.ambiguous_ids:
                            ctx.outcome = 'telegram_error'
                return result
        return wrapped
    return decorate


def mask_known_secrets(text, pairs):
    """Shared literal masker; operational formatter retains its original placeholders."""
    for secret, replacement in pairs:
        if secret:
            text = text.replace(str(secret), replacement)
    return text


async def shutdown_trace(timeout=4):
    global _writer
    writer, _writer = _writer, None
    if writer and not await asyncio.to_thread(writer.stop, timeout):
        logger.warning('Logic tracing shutdown exceeded flush budget; queued events may be lost')
