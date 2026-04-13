"""
Per-key token-bucket rate limiter.

In-process, thread-safe, zero external deps. One bucket per `key` (typically
client IP). Tokens refill continuously at `rate_per_minute`; capacity is
`rate_per_minute` by default so a client can burst up to one minute's worth
of requests before throttling kicks in.

Setting `rate_per_minute <= 0` disables the limiter — `allow()` always
returns (True, 0.0). This lets tests and local dev run without throttling.

Productization §3.3 will swap this for Redis when multi-replica deployments
land. The `TokenBucket.allow()` interface stays the same; only the storage
backend changes.
"""

from __future__ import annotations

import time
from threading import Lock


class TokenBucket:
    def __init__(self, rate_per_minute: int, capacity: int | None = None) -> None:
        self.rate_per_minute = rate_per_minute
        self.capacity = float(capacity if capacity is not None else max(rate_per_minute, 1))
        self._tokens: dict[str, float] = {}
        self._last_refill: dict[str, float] = {}
        self._lock = Lock()

    @property
    def enabled(self) -> bool:
        return self.rate_per_minute > 0

    def allow(self, key: str, *, now: float | None = None) -> tuple[bool, float]:
        """Try to consume one token from `key`'s bucket.

        Returns (allowed, retry_after_seconds). When `allowed` is False the
        caller should respond 429 with `Retry-After: retry_after_seconds`.

        The `now` parameter is for deterministic tests.
        """
        if self.rate_per_minute <= 0:
            return True, 0.0
        now = now if now is not None else time.monotonic()
        rate_per_second = self.rate_per_minute / 60.0

        with self._lock:
            tokens = self._tokens.get(key, self.capacity)
            last = self._last_refill.get(key, now)
            elapsed = max(0.0, now - last)
            tokens = min(self.capacity, tokens + elapsed * rate_per_second)

            if tokens >= 1.0:
                tokens -= 1.0
                self._tokens[key] = tokens
                self._last_refill[key] = now
                return True, 0.0

            # Not enough tokens. Record current fractional tokens and
            # compute retry_after to the nearest whole token.
            self._tokens[key] = tokens
            self._last_refill[key] = now
            retry_after = (1.0 - tokens) / rate_per_second
            return False, retry_after

    def reset(self, key: str | None = None) -> None:
        """Test helper: drop state for `key`, or everything if None."""
        with self._lock:
            if key is None:
                self._tokens.clear()
                self._last_refill.clear()
            else:
                self._tokens.pop(key, None)
                self._last_refill.pop(key, None)
