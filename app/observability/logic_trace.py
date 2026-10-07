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
from app.observability.errors import error_details
from app.observability.health import runtime_health

logger = logging.getLogger(__name__)
_current = ContextVar('logic_trace', default=None)
_request = ContextVar('diagnostic_request', default=None)
_writer = None
_level = 1
_secrets = ()
_dropped = 0
LEVELS = {'minimal': 0, 'normal': 1, 'verbose': 2}
_sensitive = re.compile(r'(token|api_key|api_user_id|authorization|cookie|password|private_key|credential|headers|params|locals|callback_data|callback_payload|\.env)', re.I)


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
        value = re.sub(r'(?i)((?:api_key|api_user_id|access_token|processing_token|callback_data|callback_payload|password|authorization|cookie|user_id)\s*[=:]\s*[\"\']?)[^&\s\"\']+', r'\1[REDACTED]', value)
        value = re.sub(r'(?i)(Bearer|Basic)\s+[A-Za-z0-9+/=_\-.]+', r'\1 [REDACTED]', value)
        value = re.sub(r'https?://[^\s/@]+:[^\s/@]+@', 'https://[REDACTED]@', value)
        value = re.sub(r'(?i)(socks\w*://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', value)
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
        self.errors = 0
        self.malformed = 0
        self.last_write_error = None
        self.last_write_at = None
        self.flush_ok = None
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
            def handle_error(record):
                self.errors += 1
                import sys
                self.last_write_error = type(sys.exc_info()[1]).__name__
                runtime_health.update('trace_writer', last_write_error=self.last_write_error, errors=self.errors)
            handler.handleError = handle_error
            handler.setFormatter(logging.Formatter('%(message)s'))
            while not (self.stopping.is_set() and self.queue.empty()):
                try:
                    event = self.queue.get(timeout=.1)
                except queue.Empty:
                    continue
                try:
                    line = json.dumps(event, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
                    previous_errors = self.errors
                    handler.emit(logging.LogRecord('logic_trace', logging.INFO, '', 0, line, (), None))
                    if self.errors == previous_errors:
                        self.last_write_at = time.time()
                except Exception:
                    self.malformed += 1
                    logger.warning('Logic tracing writer discarded an invalid event')
                finally:
                    self.queue.task_done()
        except Exception as exc:
            self.accepting = False
            self.errors += 1
            self.last_write_error = type(exc).__name__
            logger.warning('Logic tracing writer unavailable type=%s', type(exc).__name__)
        finally:
            if handler:
                handler.close()

    def stop(self, timeout):
        self.accepting = False
        self.stopping.set()
        self.thread.join(timeout)
        self.flush_ok = not self.thread.is_alive()
        runtime_health.update('trace_writer', **self.snapshot())
        return self.flush_ok

    def snapshot(self):
        return dict(trace_writer_ok=self.accepting and self.last_write_error is None,
                    queue_depth=self.queue.qsize(), dropped_events=_dropped, writer_errors=self.errors,
                    malformed_events=self.malformed, last_write_error=self.last_write_error,
                    last_write_at=self.last_write_at, flush_ok=self.flush_ok)


def writer_health():
    return _writer.snapshot() if _writer else dict(runtime_health.snapshot()['components'].get('trace_writer', {}),
        trace_writer_ok=False, enabled=False, dropped_events=_dropped)


def configure_trace(*, enabled=None, path=None, level=None, max_bytes=None, backup_count=None, retention_days=None, queue_size=2048, secrets=None):
    global _writer, _level, _secrets, _dropped
    import app.config as config
    if _writer is not None:
        _writer.stop(.5)
        _writer = None
    _dropped = 0
    enabled = config.LOGIC_TRACE_ENABLED if enabled is None else enabled
    _secrets = tuple(sorted({str(s) for s in (secrets if secrets is not None else (config.BOT_TOKEN, config.API_KEY, config.API_USER_ID)) if s}, key=len, reverse=True))
    if not enabled:
        return
    _level = LEVELS.get(level or config.LOGIC_TRACE_LEVEL, 1)
    _writer = TraceWriter(path or config.PROJECT_ROOT / 'logs' / 'logic_trace.jsonl',
        max(128, max_bytes or config.LOGIC_TRACE_MAX_BYTES),
        max(1, backup_count if backup_count is not None else config.LOGIC_TRACE_BACKUP_COUNT),
        max(1, retention_days or config.LOGIC_TRACE_RETENTION_DAYS), queue_size)


def enabled(level='minimal'):
    return _writer is not None and _writer.accepting and LEVELS.get(level, 1) <= _level


def current_trace():
    return _current.get() if _writer is not None else None


def diagnostic_trace():
    return _current.get()


@contextmanager
def technical_context(flow):
    """One background operation context, without verbose flow pairs."""
    if _current.get() is not None:
        yield _current.get()
        return
    ctx = TraceContext(flow=flow)
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


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
    flow_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_flow_id: str | None = None


def trace_event(event, *, trace=None, level='minimal', **fields):
    global _dropped
    ctx = trace or _current.get()
    component = fields.get('component', event.split('.')[0])
    request = _request.get()
    if event.startswith('rule34.breaker.') or event == 'rule34.http.attempt':
        runtime_health.update('rule34', **{k:v for k,v in sanitize(fields).items() if k != 'last_success_at'})
    if request and (event.endswith('.failed') or event.endswith('.invalid_json') or event.endswith('.http_error') or event.endswith('.unavailable')):
        request['outcome'] = 'failed'
    if request and event in {'rule34.request.success', 'telegram.send.success'}:
        request['outcome'] = 'success'
    if request and event == 'telegram.send.ambiguous_accepted':
        request['outcome'] = 'recovered'
    # Runtime observations also work with trace disabled. Cache cannot heal API health.
    if event == 'rule34.request.success' and fields.get('endpoint') != 'autocomplete' and fields.get('recovery_accepted', True):
        recoveries = runtime_health.success('rule34')
    elif event == 'telegram.send.success':
        recoveries = runtime_health.success('telegram', categories={'telegram_network', 'telegram_timeout_ambiguous', 'telegram_retry_after', 'dns', 'connect', 'read_timeout', 'read_error', 'tls', 'proxy'})
        if ctx and ctx.flow in {'media.delivery', 'digest', 'telegram.background', 'zip'}:
            ctx.fields['user_impact'] = 'delivered'
    elif event == 'db.operation' and not fields.get('error_type'):
        recoveries = runtime_health.success('db', fields.get('operation'))
    else:
        recoveries = []
    if event == 'telegram.send.fallback' and ctx:
        ctx.fields['fallback_used'] = fields.get('fallback_source', True)
    if event == 'cache.decision' and fields.get('decision') == 'fallback' and ctx:
        ctx.fields['fallback_used'] = 'cache'
    if event.endswith('.deferred') or event.startswith('subscription.defer.'):
        if event != 'subscription.defer.outage':
            runtime_health.increment('subscriptions', 'deferred')
    if (event.endswith('.deferred') or event.startswith('subscription.defer.')) and ctx:
        ctx.fields.update(final_outcome='deferred', user_impact='delayed')
    if event == 'subscription.delivery.success':
        runtime_health.increment('subscriptions', 'delivered', last_delivery_at=time.time())
    if event in {'subscription.delivery.success', 'search.delivery.success'} and ctx:
        ctx.fields['user_impact'] = 'delivered'
    if event == 'subscription.delivery.failed':
        runtime_health.increment('subscriptions', 'failed')
    if event == 'media.delivery.success' and ctx:
        ctx.fields['user_impact'] = 'delivered'
    if event == 'telegram.send.skipped' and ctx:
        ctx.fields.update(final_outcome='ignored', user_impact='no_delivery')
    if event in {'rule34.request.retry', 'telegram.send.retry', 'telegram.send.retry_after', 'media.source.retry'} and ctx:
        ctx.fields['retry_count'] = ctx.fields.get('retry_count', 0) + 1
    for recovery in recoveries:
        from app.observability.logging_filters import repeated_diagnostic_filter
        with repeated_diagnostic_filter.lock:
            repeated_diagnostic_filter.entries.clear()
        logger.info('%s recovered incident=%s failures=%s duration_seconds=%.1f', component,
                    recovery['incident_id'], recovery['failures'], recovery['outage_duration_seconds'])
        trace_event(component + '.recovered', trace=ctx, level='normal', operation=fields.get('operation', fields.get('endpoint', component)), outcome='recovered', **recovery)
    if not enabled(level) or ctx is None:
        return
    record = {'ts': datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
        'trace_id': ctx.trace_id, 'flow_id': ctx.flow_id, 'flow': ctx.flow, 'event': event,
        'level': fields.get('severity', 'INFO'), 'component': component,
        'operation': fields.get('operation', fields.get('endpoint', ctx.flow))}
    if request:
        record.update(request_id=request['request_id'], attempt=request['attempt'])
    if ctx.parent_flow_id:
        record['parent_flow_id'] = ctx.parent_flow_id
    if ctx.parent_trace_id:
        record['parent_trace_id'] = ctx.parent_trace_id
    if ctx.user_id is not None:
        record['user_id_hash'] = safe_hash(ctx.user_id, 'u_')
        record['user_hash'] = record['user_id_hash']
    if ctx.query:
        record['query'] = sanitize(ctx.query)[:512]
        record['query_hash'] = safe_hash(ctx.query, 'q_')
    for key in ('post_id', 'media_type', 'budget_remaining'):
        if key in ctx.fields and key not in fields:
            record[key] = sanitize(ctx.fields[key])
    record.update({k: v for k, v in sanitize(fields).items() if v is not None and k not in {'trace_id', 'flow_id', 'flow', 'ts', 'user_id_hash', 'user_hash'}})
    if 'status' in record and isinstance(record['status'], int):
        record.setdefault('http_status', record['status'])
    if 'wait_seconds' in record:
        record.setdefault('retry_in_seconds', record['wait_seconds'])
    # Hash fields are safe and should not be removed by broad credential-key matching.
    for key in ('claim_token_hash',):
        if key in fields:
            record[key] = fields[key] if isinstance(fields[key], str) and re.fullmatch(r'h_[0-9a-f]{12}', fields[key]) else '[REDACTED]'
    if not _writer.accept(record):
        _dropped += 1
        if _dropped & (_dropped - 1) == 0:
            logger.warning('Logic tracing queue dropped events count=%s', _dropped)


def trace_error(exc, stage, event='error'):
    component = event.split('.')[0] if '.' in event else ('rule34' if stage in {'rule34', 'post_by_id', 'autocomplete'} else stage)
    details = sanitize(error_details(exc, component))
    request = _request.get()
    ctx = _current.get()
    origin = ctx.fields.get('_error_origin') if ctx else None
    if request and (component == request['component'] or event == 'error' or event.endswith('.request.error')):
        component, operation = request['component'], request['operation']
        request_id, attempt = request['request_id'], request['attempt']
    elif origin and origin['root_error_type'] == details.get('root_error_type') and origin['root_error_message'] == details.get('root_error_message'):
        component, operation, request_id, attempt = (origin[k] for k in ('component', 'operation', 'request_id', 'attempt'))
    else:
        operation, request_id, attempt = stage, None, None
    aggregation = runtime_health.error(component, operation, details, request_id, attempt)
    if ctx:
        ctx.fields['_error_origin'] = dict(component=component, operation=operation, request_id=request_id, attempt=attempt,
            root_error_type=details.get('root_error_type'), root_error_message=details.get('root_error_message'))
        ctx.fields.update({k: v for k, v in details.items() if k not in {'error_message', 'root_error_message'}})
        if details.get('error_category') == 'telegram_timeout_ambiguous':
            ctx.fields['user_impact'] = 'unknown_delivery'
        elif component == 'db' and ctx.fields.get('user_impact') == 'delivered':
            ctx.fields['user_impact'] = 'no_delivery_impact'
    payload = dict(type=type(exc).__name__, message=str(exc), stage=stage, operation=operation, component=component, severity='WARNING', **details, **aggregation)
    if request_id:
        payload.update(request_id=request_id, attempt=attempt)
    if not aggregation['repeated']:
        payload['traceback'] = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if aggregation['summary_due']:
        counters = runtime_health.snapshot()['components'].get(component, {})
        trace_event(component + '.outage.summary', **dict(counters, **details, **aggregation))
        logger.warning('%s outage summary incident=%s failures=%s category=%s', component,
                       aggregation['incident_id'], aggregation['failures'], details.get('error_category'))
    trace_event(event, **payload)


def next_attempt():
    request = _request.get()
    if request:
        request['attempt'] += 1
        return request['attempt']
    return 1


@contextmanager
def request_context(component, operation):
    parent = _current.get()
    technical = None
    if parent is None:
        technical = _current.set(TraceContext(flow=component + '.background'))
    request = dict(request_id=uuid.uuid4().hex[:16], attempt=0, component=component, operation=operation)
    token = _request.set(request)
    started = time.monotonic()
    trace_event(component + '.request.started', level='normal', operation=operation)
    final = 'success'
    try:
        yield request
    except BaseException as exc:
        final = 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'suppressed' if type(exc).__name__ == 'Rule34Unavailable' and not request['attempt'] else 'failed'
        trace_error(exc, operation, component + '.request.error')
        raise
    finally:
        resolved = request.get('outcome', final) if final == 'success' else final
        ctx = _current.get()
        evidence = {k:v for k,v in ctx.fields.items() if k in {'error_category', 'error_type', 'root_error_type', 'http_status', 'root_http_status', 'retryable'}} if ctx and resolved != 'success' else {}
        impact = 'delayed' if resolved == 'suppressed' else 'unknown_delivery' if evidence.get('error_category') == 'telegram_timeout_ambiguous' or (component == 'telegram' and resolved == 'recovered') else 'no_delivery' if component == 'telegram' and resolved == 'failed' else 'delivered' if component == 'telegram' and resolved == 'success' else 'no_user_impact'
        trace_event(component + '.request.finished', level='normal', operation=operation, outcome=resolved,
                    duration_ms=(time.monotonic()-started)*1000, attempts=request['attempt'],
                    retry_count=max(0,request['attempt']-1), user_impact=impact, **evidence)
        _request.reset(token)
        if technical is not None:
            _current.reset(technical)


def traced_request(component, operation, *, inherit=False):
    def decorate(function):
        @wraps(function)
        async def run(*args, **kwargs):
            if inherit and _request.get() and _request.get()['component'] == component:
                return await function(*args, **kwargs)
            label = kwargs.get('operation_name', kwargs.get('endpoint', operation))
            with request_context(component, label):
                return await function(*args, **kwargs)
        return run
    return decorate


def outcome(value, **fields):
    ctx = _current.get()
    if ctx:
        ctx.outcome = value
        ctx.fields.update(fields)


def annotate(**fields):
    ctx = _current.get()
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
    parent = _current.get()
    if reuse and parent is not None and parent.flow == flow:
        yield parent
        return
    ctx = TraceContext(flow=flow, user_id=user_id, query=query,
        trace_id=parent.trace_id if parent else uuid.uuid4().hex[:16],
        parent_trace_id=parent.trace_id if parent else None, parent_flow_id=parent.flow_id if parent else None, fields=fields)
    token = _current.set(ctx)
    started = time.monotonic()
    trace_event(flow + '.start', **fields)
    trace_event(flow + '.started', level='normal', **fields)
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
        raise
    finally:
        if 'http_requests' in ctx.fields:
            ctx.fields['api_requests'] = ctx.fields['http_requests']
        legacy = ctx.outcome or 'success'
        canonical = ctx.fields.get('final_outcome') or {'empty': 'no_result', 'api_error': 'failed', 'telegram_error': 'failed', 'error': 'failed', 'validation_error': 'ignored', 'deadline': 'failed', 'budget_exhausted': 'no_result'}.get(legacy, legacy)
        if canonical == 'success' and ctx.fields.get('fallback_used'):
            canonical = 'fallback_success'
        if ctx.fields.get('user_impact') == 'delivered' and ctx.fields.get('fallback_used'):
            canonical = 'fallback_success'
        impact = ctx.fields.get('user_impact', 'delayed' if canonical in {'deferred', 'suppressed'} else 'no_delivery' if canonical == 'failed' else 'no_user_impact')
        duration = round((time.monotonic() - started) * 1000, 3)
        public = {k:v for k,v in ctx.fields.items() if not k.startswith('_')}
        if canonical in {'success', 'fallback_success', 'recovered'} and public.get('error_category'):
            public['previous_error_category'] = public.pop('error_category')
        trace_event(flow + '.finished', level='normal', **dict(public, outcome=canonical, user_impact=impact, duration_ms=duration, retry_count=ctx.fields.get('retry_count', 0), severity='ERROR' if canonical == 'failed' else 'INFO'))
        trace_event(flow + '.finish', outcome=legacy, duration_ms=duration, **public)
        if parent:
            for key in ('user_impact', 'fallback_used', '_error_origin', 'error_category', 'root_error_type', 'http_status', 'root_http_status'):
                if key in ctx.fields:
                    parent.fields[key] = ctx.fields[key]
            parent.fields['retry_count'] = parent.fields.get('retry_count', 0) + ctx.fields.get('retry_count', 0)
        if fields.get('is_more'):
            trace_event('search.more.finish', outcome=ctx.outcome or 'success')
        _current.reset(token)


def traced_flow(flow, *, user_arg=None, query_arg=None, metadata=None, inherit=False):
    def decorate(function):
        signature = inspect.signature(function)
        @wraps(function)
        async def wrapped(*args, **kwargs):
            if not enabled():
                with flow_context(flow if isinstance(flow, str) else 'background'):
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
                    ctx.outcome = 'empty' if (result is False or result is None) and name in {'search', 'subscription', 'rule34', 'random'} else 'failed' if result is False and name == 'media.delivery' else 'success'
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
