"""Bounded exception evidence. Classification never changes retry policy."""
import asyncio
import re
import sqlite3


def error_details(exc, component="app"):
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen and len(chain) < 8:
        seen.add(id(exc))
        chain.append(exc)
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    if not chain:
        return {}
    high, root = chain[0], chain[-1]
    status = None
    for item in reversed(chain):
        candidate = getattr(item, "status", None) or getattr(item, "status_code", None)
        match = re.search(r"\bHTTP\s+(\d{3})\b", str(item), re.I)
        if isinstance(candidate, int) or match:
            status = candidate if isinstance(candidate, int) else int(match[1])
            break
        if type(item).__name__ == "Rule34Unavailable":
            status = 403
    name, message = type(root).__name__, str(root).lower()
    # Domain semantics belong to the chain, not just its transport root. In
    # particular PTB TimedOut -> httpx.ReadTimeout must survive generic flows.
    telegram_name = next((type(item).__name__ for item in chain if type(item).__name__ in
                          {'RetryAfter', 'TimedOut', 'BadRequest', 'Forbidden', 'NetworkError'}), None)
    category = "invalid_state"
    if telegram_name:
        category = {"RetryAfter": "telegram_retry_after", "TimedOut": "telegram_timeout_ambiguous",
                    "BadRequest": "telegram_bad_request", "Forbidden": "telegram_forbidden"}.get(telegram_name, "telegram_network")
    elif status:
        category = "http_5xx" if status >= 500 else f"http_{status}"
    elif component == "telegram":
        category = "telegram_network"
    elif isinstance(root, sqlite3.IntegrityError):
        category = "db_integrity"
    elif isinstance(root, sqlite3.Error):
        category = "db_locked" if "locked" in message or "busy" in message else "db_failure"
    elif isinstance(root, asyncio.CancelledError):
        category = "cancellation"
    elif isinstance(root, TimeoutError):
        category = "deadline" if "deadline" in str(high).lower() else "read_timeout"
    elif "dns" in name.lower() or "gaierror" in name.lower():
        category = "dns"
    elif "proxy" in name.lower():
        category = "proxy"
    elif "ssl" in name.lower() or "certificate" in name.lower():
        category = "tls"
    elif "connector" in name.lower() or "connection" in name.lower():
        category = "connect"
    elif "payload" in name.lower():
        category = "read_error"
    elif "json" in name.lower() or "invalid json" in message or "unexpected response" in message or "invalid response" in message or "returned error" in message:
        category = "invalid_response"
    elif name == "DeliveryPreconditionFailed":
        category = "delivery_precondition"
    result = dict(error_type=type(high).__name__, error_message=str(high)[:2000],
                  root_error_type=name, root_error_message=str(root)[:2000], error_category=category,
                  retryable=category in {"http_403", "http_429", "http_5xx", "dns", "connect", "read_timeout", "telegram_network", "telegram_retry_after", "db_locked"})
    if status:
        result.update(http_status=status, root_http_status=status)
    if getattr(high, "retry_after_seconds", None) is not None:
        result["cooldown_remaining_seconds"] = high.retry_after_seconds
    if getattr(high, 'timed_out', False):
        result['error_category'] = 'deadline'
    if isinstance(getattr(high, 'returncode', None), int):
        result['returncode'] = high.returncode
    if isinstance(getattr(high, 'stage', None), str):
        result['error_stage'] = high.stage[:80]
    if category == 'telegram_bad_request':
        # Exact, observed PTB reasons only; never publish arbitrary message/URL.
        result['safe_reason'] = {
            'photo_invalid_dimensions': 'unsupported_dimensions',
            'invalid dimensions': 'unsupported_dimensions',
            'wrong type of the web page content': 'wrong_content_type',
            'failed to get http url content': 'telegram_fetch_failed',
        }.get(str(next(item for item in chain if type(item).__name__ == 'BadRequest')).strip().lower(), 'other_bad_request')
    return result
