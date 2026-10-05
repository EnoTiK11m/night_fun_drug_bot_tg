import hashlib
import logging
from app.observability.logic_trace import trace_event
import os
import secrets
import sqlite3
import time
import asyncio

from app.config import DB_PATH

CALLBACK_TTL_SECONDS = 24 * 60 * 60
CALLBACK_DB_CLEANUP_INTERVAL_SECONDS = 10 * 60
RECENT_POST_TTL_SECONDS = 6 * 60 * 60
RECENT_POST_MAX_ITEMS = 2000
CALLBACK_PAYLOAD_TABLE = "callback_payloads"

logger = logging.getLogger(__name__)

callback_payloads = {}
recent_posts = {}
last_callback_db_cleanup = 0.0
CALLBACK_RAM_MAX_ITEMS = 8192
CALLBACK_PENDING_MAX_ITEMS = 256
_pending_payload_tasks = set()
_worker_loop = None
_worker_lock = None


def _database_job(function, *args):
    """Synchronous callers remain supported; runtime SQLite runs in a worker."""
    global _worker_loop, _worker_lock
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        function(*args)
        return
    if len(_pending_payload_tasks) >= CALLBACK_PENDING_MAX_ITEMS:
        logger.warning('Callback persistence queue full; retaining RAM fallback')
        return
    if _worker_loop is not loop:
        _worker_loop, _worker_lock = loop, asyncio.Lock()
    lock = _worker_lock
    path = DB_PATH
    async def job():
        async with lock:
            await asyncio.to_thread(function, *args, db_path=path)
    task = loop.create_task(job())
    _pending_payload_tasks.add(task)
    task.add_done_callback(_pending_payload_tasks.discard)


async def flush_callback_payloads():
    tasks = tuple(_pending_payload_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def get_callback_payload_by_token_async(action, token):
    cleanup_callback_payloads()
    stored = callback_payloads.get((action, token))
    if stored:
        trace_event("callback.payload.loaded", level="normal", action=action, payload_size=len(stored[0]), source="ram")
        return stored[0]
    await flush_callback_payloads()
    payload = await asyncio.to_thread(_get_callback_payload_db, action, token, db_path=DB_PATH)
    trace_event("callback.payload.loaded" if payload else "callback.payload.missing", level="normal", action=action, payload_size=len(payload), source="sqlite")
    return payload


async def get_callback_payload_async(action, data):
    return await get_callback_payload_by_token_async(action, data.removeprefix(f'{action}_'))


def _log_payload_db_error(operation: str, exc: sqlite3.Error):
    trace_event("db.callback.persistence.failure", type=type(exc).__name__, operation=operation, message=str(exc))
    message = str(exc).lower()
    if "database is locked" in message:
        logger.debug("Skipped callback payload %s: database is locked", operation)
    else:
        logger.warning("Failed to %s callback payloads: %s", operation, exc)


def _connect_payload_db(db_path=None):
    db_path = DB_PATH if db_path is None else db_path
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    # Callback payloads back buttons that may outlive the current process.  A
    # 50 ms writer timeout made otherwise valid buttons memory-only during
    # ordinary WAL contention, so use the same bounded wait policy as the
    # asynchronous database layer.
    conn = sqlite3.connect(db_path, timeout=1.0)
    conn.execute("PRAGMA busy_timeout=1000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_payload_table(conn):
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {CALLBACK_PAYLOAD_TABLE} (
            action TEXT NOT NULL,
            token TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (action, token)
        )
    """)


def _store_callback_payload_db(action: str, token: str, payload: str, created_at: float, *, db_path=None):
    conn = None
    try:
        conn = _connect_payload_db(db_path)
        _ensure_payload_table(conn)
        conn.execute(f"""
            INSERT OR REPLACE INTO {CALLBACK_PAYLOAD_TABLE}
            (action, token, payload, created_at)
            VALUES (?, ?, ?, ?)
        """, (action, token, payload, created_at))
        conn.commit()
    except sqlite3.Error as exc:
        _log_payload_db_error("persist", exc)
    finally:
        if conn is not None:
            conn.close()


def _get_callback_payload_db(action: str, token: str, *, db_path=None) -> str:
    conn = None
    try:
        conn = _connect_payload_db(db_path)
        _ensure_payload_table(conn)
        cursor = conn.execute(f"""
            SELECT payload, created_at
            FROM {CALLBACK_PAYLOAD_TABLE}
            WHERE action = ? AND token = ?
        """, (action, token))
        row = cursor.fetchone()
        if not row:
            return ""

        payload, created_at = row
        if time.time() - float(created_at) > CALLBACK_TTL_SECONDS:
            conn.execute(f"""
                DELETE FROM {CALLBACK_PAYLOAD_TABLE}
                WHERE action = ? AND token = ?
            """, (action, token))
            conn.commit()
            return ""

        callback_payloads[(action, token)] = (payload, time.monotonic())
        return payload
    except sqlite3.Error as exc:
        _log_payload_db_error("read", exc)
        return ""
    finally:
        if conn is not None:
            conn.close()


def _cleanup_callback_payloads_db(now: float, *, db_path=None):
    conn = None
    try:
        conn = _connect_payload_db(db_path)
        _ensure_payload_table(conn)
        conn.execute(f"""
            DELETE FROM {CALLBACK_PAYLOAD_TABLE}
            WHERE ? - created_at > ?
        """, (now, CALLBACK_TTL_SECONDS))
        conn.commit()
    except sqlite3.Error as exc:
        _log_payload_db_error("cleanup", exc)
    finally:
        if conn is not None:
            conn.close()


def store_callback_payload(
    action: str,
    payload: str,
    *,
    one_shot: bool = False,
    token_prefix: str = "",
) -> str:
    """Store large callback payloads behind compact Telegram callback_data."""
    cleanup_callback_payloads()
    nonce = secrets.token_hex(8) if one_shot else ""
    token_material = (
        f"{action}:{payload}:{nonce}" if one_shot else f"{action}:{payload}"
    )
    token_hash = hashlib.blake2s(
        token_material.encode("utf-8"), digest_size=8
    ).hexdigest()
    token = f"{token_prefix}-{token_hash}" if token_prefix else token_hash
    callback_payloads[(action, token)] = (payload, time.monotonic())
    while len(callback_payloads) > CALLBACK_RAM_MAX_ITEMS:
        callback_payloads.pop(next(iter(callback_payloads)), None)
    _database_job(_store_callback_payload_db, action, token, payload, time.time())
    return f"{action}_{token}"


def get_callback_payload(action: str, data: str) -> str:
    cleanup_callback_payloads()
    token = data.replace(f"{action}_", "", 1)
    return get_callback_payload_by_token(action, token)


def get_callback_payload_by_token(action: str, token: str) -> str:
    cleanup_callback_payloads()
    stored = callback_payloads.get((action, token))
    if stored:
        return stored[0]
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _get_callback_payload_db(action, token)
    return ""


def cleanup_callback_payloads():
    global last_callback_db_cleanup
    now = time.monotonic()
    expired = [
        key
        for key, (_, created_at) in callback_payloads.items()
        if now - created_at > CALLBACK_TTL_SECONDS
    ]
    for key in expired:
        callback_payloads.pop(key, None)
    while len(callback_payloads) > CALLBACK_RAM_MAX_ITEMS:
        callback_payloads.pop(next(iter(callback_payloads)), None)

    if now - last_callback_db_cleanup >= CALLBACK_DB_CLEANUP_INTERVAL_SECONDS:
        last_callback_db_cleanup = now
        _database_job(_cleanup_callback_payloads_db, time.time())


def cleanup_recent_posts(now=None):
    now = time.monotonic() if now is None else now
    for key, (_, created) in list(recent_posts.items()):
        if now - created > RECENT_POST_TTL_SECONDS:
            recent_posts.pop(key, None)
    while len(recent_posts) > RECENT_POST_MAX_ITEMS:
        recent_posts.pop(min(recent_posts, key=lambda key: recent_posts[key][1]), None)


def safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def remember_post(post: dict):
    post_id = safe_int(post.get("id"))
    if post_id is None:
        return

    now = time.monotonic()
    recent_posts[post_id] = (dict(post), now)

    if len(recent_posts) > RECENT_POST_MAX_ITEMS:
        oldest = sorted(recent_posts.items(), key=lambda item: item[1][1])
        for old_post_id, _ in oldest[: len(recent_posts) - RECENT_POST_MAX_ITEMS]:
            recent_posts.pop(old_post_id, None)


def get_remembered_post(post_id: int) -> dict | None:
    item = recent_posts.get(post_id)
    if not item:
        return None

    post, created_at = item
    if time.monotonic() - created_at > RECENT_POST_TTL_SECONDS:
        recent_posts.pop(post_id, None)
        return None

    return dict(post)


def minimal_post(post_id: int) -> dict:
    return {
        "id": post_id,
        "file_url": "",
        "sample_url": "",
        "preview_url": "",
        "tags": "",
        "rating": "",
        "score": 0,
    }
