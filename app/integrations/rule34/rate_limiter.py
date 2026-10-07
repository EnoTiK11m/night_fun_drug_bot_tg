"""One rolling quota for the authenticated Rule34 account in this process."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import logging
import math
import time

from app.config import RULE34_API_REQUESTS_PER_WINDOW, RULE34_API_WINDOW_SECONDS
from app.services.media_preferences import runtime_metrics
from app.observability.logic_trace import trace_event

logger = logging.getLogger(__name__)


class QuotaQueueFull(Exception):
    pass


@dataclass(eq=False)
class Waiter:
    kind: str
    semaphore: object = None


class Rule34Limiter:
    def __init__(self, limit=RULE34_API_REQUESTS_PER_WINDOW,
                 window_seconds=RULE34_API_WINDOW_SECONDS, *, clock=time.monotonic,
                 wait_strategy=None, queue_limit=256, interactive_burst=5, metrics=None):
        if not 1 <= limit <= 60 or not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError('Invalid rolling quota')
        self.limit, self.window_seconds = limit, window_seconds
        self.clock, self.wait_strategy = clock, wait_strategy
        self.queue_limit, self.interactive_burst = queue_limit, interactive_burst
        self.metrics = runtime_metrics if metrics is None else metrics
        self.timestamps = deque()
        self.queues = {'interactive': deque(), 'background': deque()}
        self.condition = asyncio.Condition()
        self.cooldown_until = 0
        self.interactive_streak = 0
        self.closed = False

    def fields(self, kind, wait_ms=0):
        return dict(request_kind=kind, used=len(self.timestamps), limit=self.limit,
                    window_seconds=self.window_seconds, wait_ms=round(wait_ms, 3),
                    queue_depth=sum(map(len, self.queues.values())),
                    interactive_waiters=len(self.queues['interactive']),
                    background_waiters=len(self.queues['background']))

    def gauges(self):
        for kind in self.queues:
            self.metrics.counters['rule34_' + kind + '_waiters'] = len(self.queues[kind])

    def selected(self):
        # FIFO within each class. A background turn after five contended grants.
        interactive, background = self.queues.values()
        if background and (not interactive or self.interactive_streak >= self.interactive_burst):
            return background[0]
        return interactive[0] if interactive else None

    async def _wait(self, delay):
        if self.wait_strategy:
            await self.wait_strategy(self.condition, delay)
        elif delay is None:
            await self.condition.wait()
        else:
            try:
                async with asyncio.timeout(delay):
                    await self.condition.wait()
            except TimeoutError:
                pass

    async def acquire(self, kind='interactive', semaphore=None, *, admit=None):
        if kind not in self.queues:
            raise ValueError('Unknown request kind')
        started, waited = self.clock(), False
        waiter = Waiter(kind, semaphore)
        async with self.condition:
            if self.closed:
                raise asyncio.CancelledError('Rule34 limiter stopped')
            if sum(map(len, self.queues.values())) >= self.queue_limit:
                raise QuotaQueueFull('Rule34 quota queue full')
            self.queues[kind].append(waiter)
            self.gauges()
            self.condition.notify_all()
            trace_event('rule34.limit.acquire', level='normal', **self.fields(kind))
            try:
                while True:
                    if self.closed:
                        raise asyncio.CancelledError('Rule34 limiter stopped')
                    now = self.clock()
                    while self.timestamps and now - self.timestamps[0] >= self.window_seconds:
                        self.timestamps.popleft()
                    delay = max(0, self.cooldown_until - now,
                                self.timestamps[0] + self.window_seconds - now if len(self.timestamps) >= self.limit else 0)
                    if self.selected() is waiter and delay <= 0 and (semaphore is None or not semaphore.locked()):
                        # No suspension between semaphore admission, timestamp and dispatch.
                        if admit is not None:
                            admit()
                        if semaphore is not None:
                            await semaphore.acquire()
                        self.timestamps.append(self.clock())
                        self.queues[kind].popleft()
                        self.interactive_streak = self.interactive_streak + 1 if kind == 'interactive' and self.queues['background'] else 0
                        wait_ms = (self.clock() - started) * 1000
                        self.metrics.increment('rule34_requests_total')
                        self.gauges()
                        trace_event('rule34.limit.granted', level='normal', **self.fields(kind, wait_ms))
                        self.condition.notify_all()
                        return
                    if not waited:
                        waited = True
                        self.metrics.increment('rule34_rate_limit_waits')
                        reason = 'cooldown' if self.cooldown_until > now else 'quota' if len(self.timestamps) >= self.limit else 'concurrency' if semaphore is not None and semaphore.locked() else 'priority'
                        trace_event('rule34.limit.wait', level='normal', reason=reason, retry_in_ms=delay * 1000, **self.fields(kind))
                    # Semaphore release and cancellation wake queues. No polling loop.
                    await self._wait(delay if delay > 0 else None)
            finally:
                if waited:
                    self.metrics.increment('rule34_rate_limit_wait_ms', (self.clock() - started) * 1000)
                if waiter in self.queues[kind]:
                    self.queues[kind].remove(waiter)
                    self.gauges()
                    self.condition.notify_all()

    @asynccontextmanager
    async def slot(self, kind, semaphore=None, *, admit=None):
        await self.acquire(kind, semaphore, admit=admit)
        try:
            yield
        finally:
            if semaphore is not None:
                semaphore.release()
            await self.wake()

    async def wake(self):
        async with self.condition:
            self.condition.notify_all()

    async def cooldown(self, retry_after=None, kind='interactive'):
        try:
            seconds = float(retry_after)
        except (TypeError, ValueError):
            try:
                date = parsedate_to_datetime(retry_after)
                seconds = (date - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                seconds = 5
        if not math.isfinite(seconds) or seconds < 0:
            seconds = 5
        seconds = max(1, seconds)
        async with self.condition:
            self.cooldown_until = max(self.cooldown_until, self.clock() + seconds)
            self.metrics.increment('rule34_http_429_total')
            trace_event('rule34.limit.429', level='normal', retry_after_seconds=seconds, **self.fields(kind))
            trace_event('rule34.limit.cooldown', level='normal', cooldown_seconds=seconds, **self.fields(kind))
            self.condition.notify_all()
        logger.warning('Rule34 HTTP 429; shared cooldown %.3f seconds', seconds)

    async def stop(self):
        async with self.condition:
            self.closed = True
            self.condition.notify_all()


rule34_limiter = Rule34Limiter()
