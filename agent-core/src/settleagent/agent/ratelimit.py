"""Protect the LLM provider (and the wallet) from bursts: concurrency cap + token-bucket rate."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from settleagent.agent.llm import LlmClient, LlmReply


class RateLimitedLlm:
    """Wraps any ``LlmClient``.

    * ``max_concurrent`` — at most this many calls in flight at once;
    * ``requests_per_minute`` — sustained rate; ``burst`` calls may go back-to-back before it bites.
      ``requests_per_minute <= 0`` disables the rate limit (the concurrency cap still applies).

    Waiting callers are served in order. The clock and sleep are injectable for tests.
    """

    def __init__(
        self,
        inner: LlmClient,
        *,
        max_concurrent: int = 4,
        requests_per_minute: float = 300.0,
        burst: int = 20,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_concurrent < 1 or burst < 1:
            raise ValueError("max_concurrent and burst must be >= 1")
        self._inner = inner
        self._slots = asyncio.Semaphore(max_concurrent)
        self._rate = requests_per_minute / 60.0
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._last = clock()
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self.peak_in_flight = 0
        self._in_flight = 0
        self.throttled_seconds = 0.0

    async def _take_token(self) -> None:
        if self._rate <= 0:
            return
        async with self._lock:  # held while waiting: callers are served first-come first-served
            while True:
                now = self._clock()
                self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
                self.throttled_seconds += wait
                await self._sleep(wait)

    async def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> LlmReply:
        async with self._slots:
            await self._take_token()
            self._in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
            try:
                return await self._inner.complete_json(system=system, user=user, schema=schema)
            finally:
                self._in_flight -= 1
