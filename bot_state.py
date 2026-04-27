import hashlib
import logging
import os
import sqlite3
import time

from config import DB_PATH

CALLBACK_TTL_SECONDS = 24 * 60 * 60
CALLBACK_DB_CLEANUP_INTERVAL_SECONDS = 10 * 60
RECENT_POST_TTL_SECONDS = 6 * 60 * 60
RECENT_POST_MAX_ITEMS = 2000
CALLBACK_PAYLOAD_TABLE = "callback_payloads"

logger = logging.getLogger(__name__)

callback_payloads = {}
recent_posts = {}
last_callback_db_cleanup = 0.0


def _log_payload_db_error(operation: str, exc: sqlite3.Error):
    message = str(exc).lower()
    if "database is locked" in message:
        logger.debug("Skipped callback payload %s: database is locked", operation)
    else:
        logger.warning("Failed to %s callback payloads: %s", operation, exc)


def _connect_payload_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=0.05)
    conn.execute("PRAGMA busy_timeout=50")
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


def _store_callback_payload_db(action: str, token: str, payload: str, created_at: float):
    conn = None
    try:
        conn = _connect_payload_db()
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


def _get_callback_payload_db(action: str, token: str) -> str:
    conn = None
    try:
        conn = _connect_payload_db()
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


def _cleanup_callback_payloads_db(now: float):
    conn = None
    try:
        conn = _connect_payload_db()
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


def store_callback_payload(action: str, payload: str) -> str:
    """Store large callback payloads behind compact Telegram callback_data."""
    cleanup_callback_payloads()
    token = hashlib.blake2s(
        f"{action}:{payload}".encode("utf-8"), digest_size=8
    ).hexdigest()
    callback_payloads[(action, token)] = (payload, time.monotonic())
    _store_callback_payload_db(action, token, payload, time.time())
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
    return _get_callback_payload_db(action, token)


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

    if now - last_callback_db_cleanup >= CALLBACK_DB_CLEANUP_INTERVAL_SECONDS:
        last_callback_db_cleanup = now
        _cleanup_callback_payloads_db(time.time())


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
