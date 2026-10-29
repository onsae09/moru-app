"""Adaptive provider-request admission for the translation pipeline."""

from __future__ import annotations

import asyncio
import time

#: First backoff after a transport failure; doubles per consecutive one.
TRANSPORT_BACKOFF_BASE = 2.0
TRANSPORT_BACKOFF_MAX = 60.0


class AdaptiveLimiter:
    """Provider-request admission that sheds concurrency under overload.

    A plain semaphore cannot shrink, and a fixed limit is precisely what
    turns one slow provider into a congestion collapse: every request that
    times out becomes more concurrent load, which makes the next timeout
    more likely. This starts at ``start`` permits, halves toward ``floor``
    whenever the caller reports a transport failure, and earns one permit
    back per ``_GROW_AFTER`` consecutive successes until ``maximum``.

    Growth matters as much as shrinking: it means a conservative start for
    a local runtime costs nothing on a server that can actually keep up.
    """

    #: Consecutive successes that earn one permit back.
    _GROW_AFTER = 8

    def __init__(
        self,
        start: int,
        maximum: int,
        *,
        floor: int = 1,
        backoff_base: float = TRANSPORT_BACKOFF_BASE,
        backoff_max: float = TRANSPORT_BACKOFF_MAX,
    ) -> None:
        self._maximum = max(1, maximum)
        self._floor = max(1, min(floor, self._maximum))
        self._limit = max(self._floor, min(start, self._maximum))
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._active = 0
        self._successes = 0
        self._failures = 0
        #: Monotonic time of the first transport failure since the last
        #: success; None while calls are getting through.
        self._failing_since: float | None = None
        self._cond = asyncio.Condition()

    @property
    def limit(self) -> int:
        """Permits currently admitted concurrently."""
        return self._limit

    @property
    def failing_for(self) -> float:
        """Seconds every call has failed in transport, 0 after a success."""
        if self._failing_since is None:
            return 0.0
        return time.monotonic() - self._failing_since

    async def __aenter__(self) -> None:
        async with self._cond:
            while self._active >= self._limit:
                await self._cond.wait()
            self._active += 1

    async def __aexit__(self, *_exc: object) -> None:
        async with self._cond:
            self._active -= 1
            self._cond.notify()

    async def record_success(self) -> None:
        async with self._cond:
            self._failures = 0
            self._failing_since = None
            if self._limit >= self._maximum:
                return
            self._successes += 1
            if self._successes >= self._GROW_AFTER:
                self._successes = 0
                self._limit += 1
                # A raised ceiling has to wake a waiter itself: __aexit__
                # only ever notifies for a permit it released.
                self._cond.notify()

    async def record_transport_failure(self) -> float:
        """Halve admission; return the seconds the caller must back off."""
        async with self._cond:
            self._successes = 0
            self._failures += 1
            if self._failing_since is None:
                self._failing_since = time.monotonic()
            self._limit = max(self._floor, self._limit // 2)
            return min(
                self._backoff_base * 2 ** (self._failures - 1),
                self._backoff_max,
            )
