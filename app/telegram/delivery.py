import asyncio
import logging
from app.observability.logic_trace import trace_event, trace_error, current_trace
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable, Literal

from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import BaseRateLimiter

from app.config import (
    TELEGRAM_GLOBAL_REQUESTS_PER_SECOND,
    TELEGRAM_MAX_RETRY_AFTER_ATTEMPTS,
    TELEGRAM_PER_CHAT_REQUESTS_PER_SECOND,
    TELEGRAM_RATE_LIMIT_BURST,
    TELEGRAM_RATE_LIMIT_STATE_TTL_SECONDS,
)

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGES_PER_CHAT_MINUTE = 45  # Backward-compatible public constant.
MAX_RETRY_AFTER_SECONDS = 300.0
MAX_CHAT_BUCKETS = 4096
SAFE_TIMEOUT_RETRIES = 1
AmbiguousBadRequestPolicy = Literal[
    "edit_message_text",
    "edit_message_reply_markup",
    "delete_message",
    "answer_callback_query",
]
WaitStrategy = Callable[[asyncio.Condition, float | None], Awaitable[None]]

_limiter_active: ContextVar[bool] = ContextVar("telegram_limiter_active", default=False)


class TelegramRateLimiterClosed(RuntimeError):
    """Raised when a Telegram request is submitted after limiter shutdown."""


class TelegramRateLimiterLifecycleError(RuntimeError):
    """Raised when limiter lifecycle methods are used from an invalid event loop."""


@dataclass(slots=True)
class TelegramDeliveryMetrics:
    telegram_requests_total: int = 0
    telegram_rate_limit_waits: int = 0
    telegram_retry_after_count: int = 0
    telegram_retry_attempts: int = 0
    telegram_ambiguous_timeouts: int = 0
    telegram_request_failures: int = 0

    def reset(self) -> None:
        for field_name in self.__dataclass_fields__:
            setattr(self, field_name, 0)


@dataclass(slots=True)
class _TokenBucket:
    tokens: float
    updated_at: float
    last_seen: float
    cooldown_until: float = 0.0


@dataclass(eq=False, slots=True)
class _Waiter:
    chat_id: int
    wait_counted: bool = False


def retry_after_seconds(value: RetryAfter | timedelta | float | int | Any) -> float:
    raw_value = getattr(value, "retry_after", value)
    try:
        if isinstance(raw_value, timedelta):
            seconds = raw_value.total_seconds()
        elif hasattr(raw_value, "total_seconds"):
            seconds = raw_value.total_seconds()
        else:
            seconds = float(raw_value)
    except (TypeError, ValueError, OverflowError):
        seconds = 1.0
    return min(MAX_RETRY_AFTER_SECONDS, max(0.0, float(seconds)))


def is_ambiguous_bad_request_success(
    error: BadRequest,
    policy: AmbiguousBadRequestPolicy | None,
) -> bool:
    message = str(error).casefold()
    if policy in {"edit_message_text", "edit_message_reply_markup"}:
        return "message is not modified" in message
    if policy == "delete_message":
        return "message to delete not found" in message
    if policy == "answer_callback_query":
        return "query is too old" in message or "query id is invalid" in message
    return False


async def _default_wait_strategy(
    condition: asyncio.Condition,
    delay: float | None,
) -> None:
    if delay is None:
        await condition.wait()
        return
    try:
        async with asyncio.timeout(max(0.001, delay)):
            await condition.wait()
    except TimeoutError:
        pass


class TelegramRateLimiter(BaseRateLimiter[None]):
    """FIFO-aware process-wide and per-chat token bucket for all PTB requests."""

    def __init__(
        self,
        *,
        global_requests_per_second: float = TELEGRAM_GLOBAL_REQUESTS_PER_SECOND,
        per_chat_requests_per_second: float = TELEGRAM_PER_CHAT_REQUESTS_PER_SECOND,
        burst: int = TELEGRAM_RATE_LIMIT_BURST,
        state_ttl_seconds: float = TELEGRAM_RATE_LIMIT_STATE_TTL_SECONDS,
        max_retry_after_attempts: int = TELEGRAM_MAX_RETRY_AFTER_ATTEMPTS,
        max_registry_size: int = MAX_CHAT_BUCKETS,
        clock: Callable[[], float] = time.monotonic,
        wait_strategy: WaitStrategy = _default_wait_strategy,
        per_user_seconds: float | None = None,
        global_per_second: float | None = None,
    ) -> None:
        if global_per_second is not None:
            global_requests_per_second = global_per_second
        if per_user_seconds is not None:
            per_chat_requests_per_second = (
                1_000_000.0 if per_user_seconds <= 0 else 1.0 / per_user_seconds
            )
        self.global_rate = max(0.01, float(global_requests_per_second))
        self.per_chat_rate = max(0.01, float(per_chat_requests_per_second))
        self.burst = max(1, int(burst))
        self.state_ttl_seconds = max(0.01, float(state_ttl_seconds))
        self.max_retry_after_attempts = max(0, int(max_retry_after_attempts))
        self.max_registry_size = max(1, int(max_registry_size))
        self._clock = clock
        self._wait_strategy = wait_strategy
        self._condition = asyncio.Condition()
        self._loop = None
        self._waiters: deque[_Waiter] = deque()
        self._buckets: dict[int, _TokenBucket] = {}
        now = self._clock()
        self._global_bucket = _TokenBucket(float(self.burst), now, now)
        self._global_cooldown_until = 0.0
        self._running = True
        self.metrics = TelegramDeliveryMetrics()
        self.per_user_seconds = 1.0 / self.per_chat_rate
        self.global_interval = 1.0 / self.global_rate

    @property
    def registry_size(self) -> int:
        return len(self._buckets)

    @property
    def waiter_count(self) -> int:
        return len(self._waiters)

    async def initialize(self) -> None:
        current_loop = asyncio.get_running_loop()
        if self._running:
            if self._waiters:
                raise TelegramRateLimiterLifecycleError(
                    "Cannot initialize Telegram limiter with active waiters"
                )
            if self._loop is None:
                self._loop = current_loop
                return
            if self._loop is not current_loop:
                raise TelegramRateLimiterLifecycleError(
                    "Telegram limiter is already running on another event loop"
                )
            return
        self._condition = asyncio.Condition()
        self._loop = current_loop
        self._waiters = deque()
        self._buckets = {}
        now = self._clock()
        self._global_bucket = _TokenBucket(float(self.burst), now, now)
        self._global_cooldown_until = 0.0
        self._running = True

    async def start(self) -> None:
        await self.initialize()

    async def shutdown(self) -> None:
        condition = self._condition
        async with condition:
            self._running = False
            self._waiters.clear()
            self._buckets.clear()
            self._global_cooldown_until = 0.0
            condition.notify_all()

    def _refill(self, bucket: _TokenBucket, rate: float, now: float) -> None:
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(float(self.burst), bucket.tokens + elapsed * rate)
        bucket.updated_at = now
        bucket.last_seen = now

    def _bucket(self, chat_id: int, now: float) -> _TokenBucket:
        bucket = self._buckets.get(chat_id)
        if bucket is None:
            bucket = _TokenBucket(float(self.burst), now, now)
            self._buckets[chat_id] = bucket
        self._refill(bucket, self.per_chat_rate, now)
        return bucket

    @staticmethod
    def _token_delay(bucket: _TokenBucket, rate: float) -> float:
        if bucket.tokens >= 1.0:
            return 0.0
        return (1.0 - bucket.tokens) / rate

    def _chat_delay(self, chat_id: int, now: float) -> float:
        bucket = self._bucket(chat_id, now)
        return max(
            0.0,
            bucket.cooldown_until - now,
            self._token_delay(bucket, self.per_chat_rate),
        )

    def _cleanup_registry(self, now: float) -> None:
        active_chat_ids = {waiter.chat_id for waiter in self._waiters}
        expired = [
            chat_id
            for chat_id, bucket in tuple(self._buckets.items())
            if chat_id not in active_chat_ids
            and now - bucket.last_seen >= self.state_ttl_seconds
        ]
        for chat_id in expired:
            self._buckets.pop(chat_id, None)
        overflow = len(self._buckets) - self.max_registry_size
        if overflow > 0:
            inactive = sorted(
                (
                    (bucket.last_seen, chat_id)
                    for chat_id, bucket in self._buckets.items()
                    if chat_id not in active_chat_ids
                )
            )
            for _last_seen, chat_id in inactive[:overflow]:
                self._buckets.pop(chat_id, None)

    def _remove_waiter(self, waiter: _Waiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            pass

    async def wait_for_slot(self, chat_id: int) -> bool:
        normalized_chat_id = int(chat_id or 0)
        waiter = _Waiter(normalized_chat_id)
        current_loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = current_loop
        elif self._loop is not current_loop:
            raise TelegramRateLimiterLifecycleError(
                "Telegram limiter is running on another event loop; "
                "call shutdown there before initialize/start in this loop"
            )
        condition = self._condition
        async with condition:
            if not self._running:
                raise TelegramRateLimiterClosed("Telegram rate limiter is shut down")
            self._waiters.append(waiter)
            condition.notify_all()

        try:
            while True:
                async with condition:
                    if not self._running:
                        self._remove_waiter(waiter)
                        condition.notify_all()
                        raise TelegramRateLimiterClosed("Telegram rate limiter is shut down")

                    now = self._clock()
                    self._cleanup_registry(now)
                    self._refill(self._global_bucket, self.global_rate, now)
                    global_delay = max(
                        0.0,
                        self._global_cooldown_until - now,
                        self._token_delay(self._global_bucket, self.global_rate),
                    )
                    selected = None
                    minimum_chat_delay = None
                    if global_delay <= 0.0:
                        for candidate in self._waiters:
                            candidate_delay = self._chat_delay(candidate.chat_id, now)
                            if candidate_delay <= 0.0:
                                selected = candidate
                                break
                            if minimum_chat_delay is None or candidate_delay < minimum_chat_delay:
                                minimum_chat_delay = candidate_delay

                    if selected is waiter:
                        chat_bucket = self._bucket(normalized_chat_id, now)
                        self._global_bucket.tokens -= 1.0
                        chat_bucket.tokens -= 1.0
                        self._remove_waiter(waiter)
                        self._cleanup_registry(now)
                        condition.notify_all()
                        return True

                    delay = global_delay if global_delay > 0 else minimum_chat_delay
                    if (
                        (selected is not None or (delay is not None and delay > 0))
                        and not waiter.wait_counted
                    ):
                        waiter.wait_counted = True
                        self.metrics.telegram_rate_limit_waits += 1
                    await self._wait_strategy(condition, delay)
        except BaseException:
            async with condition:
                self._remove_waiter(waiter)
                condition.notify_all()
            raise

    def apply_retry_after(self, chat_id: int, error: RetryAfter | Any) -> float:
        wait_seconds = retry_after_seconds(error)
        now = self._clock()
        until = now + wait_seconds
        self._global_cooldown_until = max(self._global_cooldown_until, until)
        bucket = self._bucket(int(chat_id or 0), now)
        bucket.cooldown_until = max(bucket.cooldown_until, until)
        self._cleanup_registry(now)
        return wait_seconds

    async def _execute_retry_after(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        operation_name: str,
        chat_id: int,
        max_retry_after_attempts: int | None = None,
    ) -> Any:
        if _limiter_active.get():
            return await operation()
        retry_limit = (
            self.max_retry_after_attempts
            if max_retry_after_attempts is None
            else max(0, int(max_retry_after_attempts))
        )
        retry_after_attempts = 0
        attempt = 0
        while True:
            queued_at = time.monotonic()
            await self.wait_for_slot(chat_id)
            attempt += 1
            trace_event("telegram.queue.admitted", level="verbose", queue_wait_ms=(time.monotonic()-queued_at)*1000)
            self.metrics.telegram_requests_total += 1
            token = _limiter_active.set(True)
            try:
                started = time.monotonic()
                trace_event("telegram.send.start", operation=operation_name, attempt=attempt)
                result = await operation()
                trace_event("telegram.send.success", operation=operation_name, attempt=attempt, duration_ms=(time.monotonic()-started)*1000)
                return result
            except RetryAfter as exc:
                trace_event("telegram.send.retry_after", operation=operation_name, attempt=attempt, wait_seconds=retry_after_seconds(exc))
                self.metrics.telegram_retry_after_count += 1
                wait_seconds = self.apply_retry_after(chat_id, exc)
                if retry_after_attempts >= retry_limit:
                    raise
                retry_after_attempts += 1
                self.metrics.telegram_retry_attempts += 1
                logger.warning(
                    "Telegram RetryAfter operation=%s chat=%s attempt=%s wait=%.1fs",
                    operation_name,
                    chat_id,
                    attempt,
                    wait_seconds,
                )
            except Exception as trace_exc:
                trace_error(trace_exc, stage=operation_name, event="telegram.send.failed")
                raise
            finally:
                _limiter_active.reset(token)

    async def execute(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        operation_name: str,
        chat_id: int,
        safe_to_retry_timeout: bool = False,
        ambiguous_bad_request_policy: AmbiguousBadRequestPolicy | None = None,
        max_retry_after_attempts: int | None = None,
    ) -> Any:
        timeout_attempts = 0
        ambiguous_timeout = False
        while True:
            try:
                return await self._execute_retry_after(
                    operation,
                    operation_name=operation_name,
                    chat_id=chat_id,
                    max_retry_after_attempts=max_retry_after_attempts,
                )
            except TimedOut as trace_exc:
                trace_error(trace_exc, stage=operation_name, event="telegram.send.timeout")
                ambiguous_timeout = True
                if safe_to_retry_timeout and timeout_attempts < SAFE_TIMEOUT_RETRIES:
                    timeout_attempts += 1
                    self.metrics.telegram_retry_attempts += 1
                    logger.warning(
                        "Telegram timeout retry operation=%s chat=%s attempt=%s",
                        operation_name,
                        chat_id,
                        timeout_attempts,
                    )
                    continue
                self.metrics.telegram_ambiguous_timeouts += 1
                self.metrics.telegram_request_failures += 1
                raise
            except BadRequest as exc:
                trace_error(exc, stage=operation_name, event="telegram.send.bad_request")
                if ambiguous_timeout and is_ambiguous_bad_request_success(
                    exc, ambiguous_bad_request_policy
                ):
                    return True
                self.metrics.telegram_request_failures += 1
                raise
            except asyncio.CancelledError:
                raise
            except Exception as trace_exc:
                error_kind = "forbidden" if type(trace_exc).__name__ == "Forbidden" else "network_error" if isinstance(trace_exc, NetworkError) else "failed"
                trace_error(trace_exc, stage=operation_name, event="telegram.send." + error_kind)
                self.metrics.telegram_request_failures += 1
                raise

    async def process_request(
        self,
        callback,
        args: Any,
        kwargs: dict[str, Any],
        endpoint: str,
        data: dict[str, Any],
        rate_limit_args: None,
    ):
        if _limiter_active.get():
            return await callback(*args, **kwargs)
        chat_id = data.get("chat_id")
        if not isinstance(chat_id, int):
            chat_id = 0
        return await self._execute_retry_after(
            lambda: callback(*args, **kwargs),
            operation_name=endpoint,
            chat_id=chat_id,
        )

    def snapshot_metrics(self) -> dict[str, int]:
        return {
            "telegram_requests_total": self.metrics.telegram_requests_total,
            "telegram_rate_limit_waits": self.metrics.telegram_rate_limit_waits,
            "telegram_retry_after_count": self.metrics.telegram_retry_after_count,
            "telegram_retry_attempts": self.metrics.telegram_retry_attempts,
            "telegram_ambiguous_timeouts": self.metrics.telegram_ambiguous_timeouts,
            "telegram_request_failures": self.metrics.telegram_request_failures,
            "telegram_limiter_registry_size": self.registry_size,
        }

    def reset(self) -> None:
        """Reset transient state for isolated tests without binding to an event loop."""
        self._condition = asyncio.Condition()
        self._loop = None
        self._waiters.clear()
        self._buckets.clear()
        now = self._clock()
        self._global_bucket = _TokenBucket(float(self.burst), now, now)
        self._global_cooldown_until = 0.0
        self._running = True
        self.metrics.reset()


async def execute_telegram_request(
    operation: Callable[[], Awaitable[Any]],
    *,
    operation_name: str,
    chat_id: int,
    safe_to_retry_timeout: bool = False,
    ambiguous_bad_request_policy: AmbiguousBadRequestPolicy | None = None,
    max_retry_after_attempts: int | None = None,
    limiter: TelegramRateLimiter | None = None,
) -> Any:
    active_limiter = telegram_rate_limiter if limiter is None else limiter
    return await active_limiter.execute(
        operation,
        operation_name=operation_name,
        chat_id=chat_id,
        safe_to_retry_timeout=safe_to_retry_timeout,
        ambiguous_bad_request_policy=ambiguous_bad_request_policy,
        max_retry_after_attempts=max_retry_after_attempts,
    )


telegram_rate_limiter = TelegramRateLimiter()
