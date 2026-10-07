"""Read-only local snapshots. No upstream probes or application DB helpers."""
import asyncio
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import time

from app.observability.health import runtime_health
from app.observability.logic_trace import sanitize, writer_health, trace_event, TraceContext
from app.observability.db_diagnostics import counter_snapshot

_startup_trace = TraceContext(flow='startup')


def stage(name, **fields):
    with runtime_health.lock:
        runtime_health.process.update(stage=name, **sanitize(fields))
    trace_event('process.stage', trace=_startup_trace, level='normal', stage=name, **fields)


def cache_version(root):
    """Read local Git metadata once at startup, without spawning Git."""
    revision = 'unknown'
    try:
        git = Path(root) / '.git'
        if git.is_file():
            git = (Path(root) / git.read_text(encoding='utf-8').strip().removeprefix('gitdir: ')).resolve()
        head = (git / 'HEAD').read_text(encoding='utf-8').strip()
        if head.startswith('ref: '):
            ref = head[5:]
            if (git / ref).exists():
                head = (git / ref).read_text(encoding='utf-8').strip()
            else:
                head = next(line.split()[0] for line in (git / 'packed-refs').read_text(encoding='utf-8').splitlines()
                            if line.endswith(' ' + ref))
        if len(head) == 40 and all(c in '0123456789abcdef' for c in head):
            revision = head[:12]
    except (OSError, StopIteration):
        pass
    with runtime_health.lock:
        runtime_health.process.update(pid=os.getpid(), version=revision, version_scope='HEAD; local changes may exist')
    return revision


def read_subscriptions(path, *, timeout=.1):
    started = time.monotonic()
    connection = None
    try:
        # URI mode=ro also prevents accidental creation of a missing DB.
        connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=timeout)
        connection.set_progress_handler(lambda: int(time.monotonic()-started > .25), 1000)
        row = connection.execute("""SELECT
            COALESCE(SUM(is_active = 1),0),
            COALESCE(SUM(is_active = 1 AND datetime(COALESCE(next_check_at,last_sent)) <= datetime('now')
                AND (processing_until IS NULL OR datetime(processing_until) <= datetime('now'))),0),
            COALESCE(SUM(processing_until IS NOT NULL AND datetime(processing_until) > datetime('now')),0)
            , COALESCE(SUM(processing_until IS NOT NULL AND datetime(processing_until) <= datetime('now')),0)
            FROM subscriptions""").fetchone()
        return dict(active=row[0], due=row[1], claims=row[2], expired_claims=row[3], snapshot_at=time.time())
    except sqlite3.Error as exc:
        return dict(snapshot_error=type(exc).__name__, snapshot_at=time.time())
    finally:
        if connection is not None:
            connection.close()


async def subscription_snapshot(path):
    try:
        result = await asyncio.wait_for(asyncio.to_thread(read_subscriptions, path), 1)
    except TimeoutError:
        result = dict(snapshot_error='TimeoutError', snapshot_at=time.time())
    runtime_health.update('subscriptions', **result)
    return result


def age(timestamp, now):
    return 'unknown' if timestamp is None else f'{max(0,int(now-timestamp))}s'


def format_snapshot(snapshot, *, timezone=None, now=None):
    now = time.time() if now is None else now
    process, components = snapshot['process'], snapshot['components']
    telegram, rule34 = components.get('telegram', {}), components.get('rule34', {})
    subscriptions, db = components.get('subscriptions', {}), components.get('db', {})
    writer = snapshot.get('trace_writer', {})
    timezone = timezone or datetime.now().astimezone().tzinfo
    lines = [f"Diag {datetime.fromtimestamp(now,timezone).strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"Process pid={process.get('pid',os.getpid())} version={process.get('version','unknown')} uptime={age(process.get('started_at'),now)} stage={process.get('stage')} lock={process.get('lock_held','unknown')}",
        f"Telegram success_age={age(telegram.get('last_success_at'),now)} error_age={age(telegram.get('last_error_at'),now)} category={telegram.get('error_category','none')} degraded={telegram.get('degraded',False)} cooldown={round(telegram.get('cooldown_remaining_seconds',0))}s retry_after={telegram.get('telegram_retry_after_count',0)} queue={telegram.get('queue_depth',0)} ambiguous={telegram.get('telegram_ambiguous_timeouts',0)} retries={telegram.get('telegram_retry_attempts',0)}",
        f"Rule34 success_age={age(rule34.get('last_success_at'),now)} error_age={age(rule34.get('last_error_at'),now)} breaker={rule34.get('breaker_open',False)} status={rule34.get('last_http_status','unknown')} category={rule34.get('category','unknown')} cooldown={round(rule34.get('cooldown_remaining_seconds',0))}s outage={round(rule34.get('outage_seconds',0))}s backoff={rule34.get('backoff_seconds',0)}s attempts={rule34.get('physical_attempts',0)} failures={rule34.get('physical_failures',0)} suppressed={rule34.get('suppressed_requests',0)} probes={rule34.get('probes',0)} recoveries={rule34.get('recoveries',0)}",
        f"Subscriptions active={subscriptions.get('active','unknown')} due={subscriptions.get('due','unknown')} claims={subscriptions.get('claims','unknown')}/{subscriptions.get('expired_claims','unknown')} last_delivery_age={age(subscriptions.get('last_delivery_at'),now)} failed={subscriptions.get('failed',0)} delivered={subscriptions.get('delivered',0)} deferred={subscriptions.get('deferred',0)} snapshot_error={subscriptions.get('snapshot_error','none')}",
        f"SQLite connections={db.get('db_connections_active',0)}/{db.get('db_connections_peak',0)} operations={db.get('db_operations_active',0)}/{db.get('db_operations_peak',0)} last={db.get('last_operation','unknown')} duration={round(db.get('last_duration_ms',0))}ms slow={db.get('slow_operations',0)} last_slow={db.get('last_slow_operation','unknown')}:{round(db.get('last_slow_duration_ms',0))}ms category={db.get('error_category','none')}",
        f"Trace ok={writer.get('trace_writer_ok',False)} queue={writer.get('queue_depth',0)} drops={writer.get('dropped_events',0)} errors={writer.get('writer_errors',0)} malformed={writer.get('malformed_events',0)} last_error={writer.get('last_write_error') or 'none'}",
        f"App active_incidents={snapshot['active_incidents']} gate={process.get('gate_registry',0)} waiters={process.get('gate_waiters',0)} stale={process.get('stale_results',0)} duplicate={process.get('duplicate_callbacks',0)}"]
    for incident in snapshot.get('incidents', []):
        lines.append(f"{incident['incident_id']} {incident['component']}/{incident['operation']} {incident.get('error_category')} reason={incident.get('safe_reason','-')} root={incident.get('root_error_type')} status={incident.get('http_status','-')} failures={incident['failures']} first_age={age(incident['started_at'],now)} last_age={age(incident['last_failure_at'],now)} state={incident.get('outcome','active')}")
    return sanitize('\n'.join(lines))[:3900]


def local_snapshot(*, errors=False):
    runtime_health.update('db', **counter_snapshot())
    snapshot = runtime_health.snapshot(errors)
    snapshot['trace_writer'] = writer_health()
    return snapshot
