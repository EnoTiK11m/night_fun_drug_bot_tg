import asyncio
import logging
import time
from datetime import timedelta

from telegram.error import RetryAfter

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGES_PER_CHAT_MINUTE = 45


def retry_after_seconds(error: RetryAfter) -> float:
    value = error.retry_after
    if isinstance(value, timedelta):
        return max(0.0, value.total_seconds())
    return max(0.0, float(value))


class TelegramRateLimiter:
    """In-process per-user/global pacing plus Telegram RetryAfter cooldowns."""

    def __init__(
        self,
        per_user_seconds: float = 60 / TELEGRAM_MESSAGES_PER_CHAT_MINUTE,
        global_per_second: int = 25,
    ):
        self.per_user_seconds = max(0.0, per_user_seconds)
        self.global_interval = 1.0 / max(1, global_per_second)
        self._lock = asyncio.Lock()
        self._next_user_send: dict[int, float] = {}
        self._next_global_send = 0.0
        self._cooldowns: dict[int, float] = {}

    async def wait_for_slot(self, user_id: int) -> bool:
        while True:
            now = time.monotonic()
            cooldown_until = self._cooldowns.get(user_id, 0.0)
            if cooldown_until > now:
                logger.info(
                    "Telegram send delayed user=%s cooldown_remaining=%.1fs",
                    user_id,
                    cooldown_until - now,
                )
                await asyncio.sleep(cooldown_until - now)
                continue

            async with self._lock:
                now = time.monotonic()
                cooldown_until = self._cooldowns.get(user_id, 0.0)
                if cooldown_until > now:
                    cooldown_delay = cooldown_until - now
                    send_at = None
                else:
                    cooldown_delay = 0.0
                    send_at = max(
                        now,
                        self._next_global_send,
                        self._next_user_send.get(user_id, 0.0),
                    )
                    self._next_global_send = send_at + self.global_interval
                    self._next_user_send[user_id] = send_at + self.per_user_seconds

            if send_at is None:
                await asyncio.sleep(cooldown_delay)
                continue

            if send_at > now:
                await asyncio.sleep(send_at - now)

            now = time.monotonic()
            if self._cooldowns.get(user_id, 0.0) > now:
                continue
            return True

    def apply_retry_after(self, user_id: int, error: RetryAfter) -> float:
        wait_seconds = retry_after_seconds(error) + 5.0
        self._cooldowns[user_id] = max(
            self._cooldowns.get(user_id, 0.0),
            time.monotonic() + wait_seconds,
        )
        logger.warning(
            "Telegram RetryAfter user=%s wait=%.1fs; cooldown applied",
            user_id,
            wait_seconds,
        )
        return wait_seconds

    def reset(self) -> None:
        """Reset transient state (primarily useful for isolated tests)."""
        self._next_user_send.clear()
        self._next_global_send = 0.0
        self._cooldowns.clear()


telegram_rate_limiter = TelegramRateLimiter()
