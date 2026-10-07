"""Event-loop-owned API health; only HTTP 403 opens the shared circuit.

Transitions contain no awaits. Admission is checked again inside the limiter,
before quota is charged, so queued requests cannot slip through an opened circuit.
"""
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import logging
import math
import random
import re
import time

from app.observability.logic_trace import trace_event
from app.services.media_preferences import runtime_metrics

logger = logging.getLogger(__name__)
BACKOFF_SECONDS = (60, 120, 300, 600, 900)
BODY_SAMPLE_LIMIT = 8192


class APITemporaryError(Exception):
    """The API could not complete this operation; retry later."""


class Rule34Unavailable(APITemporaryError):
    def __init__(self, remaining_seconds):
        self.retry_after_seconds = max(1, math.ceil(remaining_seconds))
        super().__init__("Rule34 API temporarily unavailable (HTTP 403 circuit open)")


def unavailable_text(exc):
    if isinstance(exc, Rule34Unavailable):
        minutes = max(1, math.ceil(exc.retry_after_seconds / 60))
        return f"⚠️ Rule34 API временно недоступен. Следующая проверка через ~{minutes} мин."
    return "⚠️ Rule34 временно недоступен. Попробуйте позже."


def forbidden_diagnostics(headers, body, *, secrets=()):
    """No body snippets or arbitrary header values, even if upstream echoes secrets."""
    body = body[:BODY_SAMPLE_LIMIT]
    secret_values = tuple(str(secret).casefold() for secret in secrets if secret)
    headers = {k.lower(): str(v) for k, v in headers.items()
               if not any(secret in str(v).casefold() for secret in secret_values)}
    safe = {}
    content_type = headers.get('content-type', '').split(';', 1)[0].strip().lower()
    safe['content_type'] = content_type if content_type in {
        'application/json', 'text/html', 'text/plain', 'application/octet-stream',
    } else 'unknown'
    for name, pattern in {
        'content-length': r'[0-9]{1,12}',
        'retry-after': r'[0-9]{1,8}',
        'cf-ray': r'[0-9a-fA-F]{16}-[A-Z]{3}',
    }.items():
        value = headers.get(name, '')
        if re.fullmatch(pattern, value):
            safe[name.replace('-', '_')] = value
    server = headers.get('server', '').lower()
    safe['server'] = server if server in {'cloudflare', 'nginx', 'apache', 'cloudfront'} else 'unknown'
    for name, values in {
        'cf-mitigated': {'challenge'},
        'cf-cache-status': {'HIT', 'MISS', 'DYNAMIC', 'BYPASS', 'EXPIRED', 'STALE', 'REVALIDATED', 'UPDATING'},
    }.items():
        if headers.get(name) in values:
            safe[name.replace('-', '_')] = headers[name]
    text = body.decode('utf-8', errors='replace').lower()
    classification = 'unknown_text'
    if not body.strip():
        classification = 'empty'
    elif server == 'cloudflare' and (
        headers.get('cf-mitigated') == 'challenge'
        or ('challenge-platform' in text and ('cf-' in text or 'cloudflare' in text))
    ):
        classification = 'cloudflare_challenge'
    else:
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            data = None
        if isinstance(data, dict) and (data.get('success') is False or 'error' in data):
            classification = 'json_error'
            message = ' '.join(str(data.get(k, '')) for k in ('error', 'message', 'code')).lower()
            if any(hint in message for hint in ('invalid api key', 'invalid credentials', 'authentication failed')):
                classification = 'auth_like'
        elif '<html' in text or '<!doctype html' in text:
            classification = 'html_forbidden'
    category = {
        'cloudflare_challenge': 'http_403_cloudflare_challenge',
        'auth_like': 'http_403_auth_like',
    }.get(classification, 'http_403_unknown')
    return dict(status=403, category=category, body_classification=classification,
                body_sample_bytes=len(body), body_sample_limit=BODY_SAMPLE_LIMIT,
                body_sha256_prefix=hashlib.sha256(body).hexdigest()[:16], **safe)


@dataclass
class Admission:
    generation: int
    probe: bool = False
    dispatched: bool = False
    completed: bool = False


class Rule34Outage:
    def __init__(self, *, clock=time.monotonic, wall_clock=lambda: datetime.now(UTC),
                 jitter=lambda: random.uniform(0, 15), metrics=None):
        self.clock, self.wall_clock, self.jitter = clock, wall_clock, jitter
        self.metrics = runtime_metrics if metrics is None else metrics
        self.opened_at = None
        self.failure_started_at = None
        self.health_alerted = False
        self.cooldown_until = 0.0
        self.backoff_seconds = 0
        self.backoff_index = -1
        self.category = None
        self.last_http_status = None
        self.last_success_at = None
        self.consecutive_failures = 0
        self.logical_failures = 0
        self.physical_attempts = 0
        self.physical_failures = 0
        self.suppressed_requests = 0
        self.probes = 0
        self.recoveries = 0
        self.generation = 0
        self.probe = None
        self.notifier = None
        self.last_alert_at = -math.inf
        self.pending_recovery = None

    @property
    def is_open(self):
        return self.opened_at is not None

    def remaining(self):
        return max(0.0, self.cooldown_until - self.clock())

    def fields(self, kind=None):
        return dict(status=self.last_http_status, category=self.category,
                    backoff_seconds=self.backoff_seconds,
                    remaining_seconds=round(self.remaining(), 3),
                    outage_seconds=round(self.clock() - self.failure_started_at, 3) if self.failure_started_at is not None else 0,
                    consecutive_failures=self.consecutive_failures,
                    logical_failures=self.logical_failures,
                    physical_attempts=self.physical_attempts,
                    physical_failures=self.physical_failures,
                    suppressed_requests=self.suppressed_requests,
                    last_success_at=self.last_success_at, request_kind=kind)

    def reject(self, kind):
        self.suppressed_requests += 1
        self.metrics.increment('rule34_suppressed_requests')
        self.logical_failures += 1
        trace_event('rule34.breaker.suppressed', level='normal', **self.fields(kind))
        raise Rule34Unavailable(self.remaining() or self.backoff_seconds)

    def admit(self, kind, *, recovery_endpoint=True):
        if self.is_open:
            if self.remaining() > 0 or self.probe is not None or not recovery_endpoint:
                self.reject(kind)
            ticket = Admission(self.generation, probe=True)
            self.probe = ticket
            self.probes += 1
            self.metrics.increment('rule34_probes')
            trace_event('rule34.breaker.probe', level='normal', **self.fields(kind))
            return ticket
        return Admission(self.generation)

    def validate(self, ticket, kind):
        if ticket.generation != self.generation or (self.is_open and self.probe is not ticket):
            self.reject(kind)

    def dispatched(self, ticket, kind, endpoint):
        ticket.dispatched = True
        self.physical_attempts += 1
        self.metrics.increment('rule34_http_attempts')
        trace_event('rule34.http.attempt', level='normal', endpoint=endpoint, **self.fields(kind))

    def failure(self, ticket, *, status=None):
        if ticket.completed:
            return
        ticket.completed = True
        self.physical_failures += 1
        self.metrics.increment('rule34_physical_failures')
        self.consecutive_failures += 1
        self.last_http_status = status
        if self.failure_started_at is None:
            self.failure_started_at = self.clock()
        if not self.is_open:
            self.category = ('http_5xx' if status is not None and status >= 500
                             else f'http_{status}' if status is not None
                             else 'network_or_timeout')

    def forbidden(self, ticket, kind, category):
        self.failure(ticket, status=403)
        self.logical_failures += 1
        # In-flight results from an older generation do not extend a newer circuit.
        if ticket.generation != self.generation:
            return
        event = 'rule34.breaker.extended' if self.is_open else 'rule34.breaker.opened'
        if not self.is_open:
            self.opened_at = self.clock()
        self.backoff_index = min(self.backoff_index + 1, len(BACKOFF_SECONDS) - 1)
        self.backoff_seconds = BACKOFF_SECONDS[self.backoff_index]
        self.cooldown_until = self.clock() + self.backoff_seconds
        self.category = category
        self.generation += 1
        self.probe = None
        trace_event(event, level='normal', **self.fields(kind))

    def success(self, ticket, kind, *, recovery_endpoint=True):
        ticket.completed = True
        # An unauthenticated autocomplete response cannot prove post API recovery.
        if not recovery_endpoint or ticket.generation != self.generation:
            return
        if self.is_open and self.probe is not ticket:
            return
        self.last_success_at = self.wall_clock().isoformat()
        self.last_http_status = 200
        if self.is_open or self.health_alerted:
            self.pending_recovery = self.fields(kind)
            self.recoveries += 1
            self.metrics.increment('rule34_recoveries')
        if self.is_open:
            trace_event('rule34.breaker.closed', level='normal', **self.pending_recovery)
            self.generation += 1
        self.opened_at = None
        self.failure_started_at = None
        self.health_alerted = False
        self.cooldown_until = 0
        self.backoff_seconds = 0
        self.backoff_index = -1
        self.category = None
        self.consecutive_failures = self.logical_failures = 0
        self.physical_attempts = self.physical_failures = 0
        self.probe = None

    def release(self, ticket):
        if self.probe is ticket:
            # Cancelled/failed probes must not leave half-open stuck or cause a hot loop.
            self.probe = None
            self.cooldown_until = self.clock() + self.backoff_seconds
            self.generation += 1

    def subscription_delay(self):
        if not self.is_open:
            return None
        return math.ceil((self.remaining() or self.backoff_seconds) + self.jitter()) + 1

    async def notify(self):
        if self.notifier is None:
            return
        messages = []
        if self.pending_recovery is not None:
            recovered, self.pending_recovery = self.pending_recovery, None
            messages.append(f"✅ Rule34 API восстановился после {math.ceil(recovered['outage_seconds'] / 60)} мин.")
        # Keep reporting sustained non-403 failures, without opening their circuit
        # or changing retries / the limiter's independent 429 cooldown.
        if (self.is_open or self.consecutive_failures >= 5) and self.clock() - self.last_alert_at >= 900:
            self.last_alert_at = self.clock()
            self.health_alerted = True
            fields = self.fields()
            status_text = f"HTTP {fields['status']}" if fields['status'] is not None else fields['category']
            messages.append(
                f"🚨 Rule34 outage: {status_text}, {fields['category']}, "
                f"{math.ceil(fields['outage_seconds'] / 60)} мин; "
                f"logical failures={fields['logical_failures']}, physical failures={fields['physical_failures']}, "
                f"backoff={fields['backoff_seconds']}s, remaining={math.ceil(fields['remaining_seconds'])}s; "
                f"last API success={fields['last_success_at'] or 'не наблюдался'}"
            )
        for message in messages:
            try:
                async with asyncio.timeout(3):
                    await self.notifier(message)
            except Exception as exc:
                logger.warning('Rule34 health notification failed type=%s', type(exc).__name__)


rule34_outage = Rule34Outage()
