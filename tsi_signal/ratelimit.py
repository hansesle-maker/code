"""Process-wide request pacing for exchange REST APIs.

Two limiters, because the two exchanges cap different things:

* :class:`RateLimiter` — plain requests-per-second (Bithumb's public API).
* :class:`WeightLimiter` — Binance's *request weight* budget over a sliding
  minute. Klines cost 1 (limit < 100) or 2 (limit 100–499), and the fapi
  budget is 2400/min per IP.

The Binance limiter is a module-level singleton on purpose: the TSI scanner
and the CVD scanner both hammer the same IP, so a per-scanner limiter cannot
prevent a 429 — only one shared budget can.
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger(__name__)


class RateLimiter:
    """Thread-safe cap of ``rate`` requests per second (0 disables)."""

    def __init__(self, rate: float):
        self._min_gap = 1.0 / rate if rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self._min_gap <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._min_gap
        if delay > 0:
            time.sleep(delay)


class WeightLimiter:
    """Token-bucket weight budget shared by every caller in the process.

    Refills at ``budget / window`` per second with a small burst allowance,
    which paces a long scan *evenly*. A sliding-window counter would instead
    let the scan sprint through a whole minute's budget and then stall dead
    until the window rolled — the same total time, but a confusing freeze
    partway through.

    Worst case over any ``window`` is ``budget + capacity``, so keep the
    configured budget below the exchange's hard ceiling by at least the burst.
    """

    def __init__(self, budget: float, window: float = 60.0,
                 burst_frac: float = 0.1, name: str = ""):
        self.budget = max(1.0, float(budget))
        self.window = window
        self.name = name
        self.rate = self.budget / window            # weight per second
        self.capacity = max(10.0, self.budget * burst_frac)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self._until = 0.0          # global pause deadline (monotonic)

    # -- internals ---------------------------------------------------------
    def _refill(self, now: float) -> None:
        if now > self._last:
            self._tokens = min(self.capacity,
                               self._tokens + (now - self._last) * self.rate)
            self._last = now

    # -- api ---------------------------------------------------------------
    def acquire(self, weight: float = 1.0) -> None:
        # A call heavier than the bucket itself would never fit; clamp so it
        # waits for a full bucket instead of spinning forever.
        weight = min(float(weight), self.capacity)
        while True:
            with self._lock:
                now = time.monotonic()
                if now >= self._until:
                    self._refill(now)
                    if self._tokens >= weight:
                        self._tokens -= weight
                        return
                    sleep_for = (weight - self._tokens) / self.rate
                else:
                    sleep_for = self._until - now
            time.sleep(max(0.01, min(sleep_for, 5.0)))

    def penalize(self, seconds: float) -> None:
        """Pause every caller for ``seconds`` (after a 429 / 418).

        Only the deadline moves — injecting a window's worth of fake weight
        here would stall every caller for the whole window, not just the
        backoff, which is far more than a transient 429 warrants.
        """
        with self._lock:
            self._until = max(self._until, time.monotonic() + max(0.0, seconds))
        log.warning("%s rate limit hit — pausing all requests %.1fs",
                    self.name or "exchange", seconds)

    def observe(self, used: float) -> None:
        """React to the exchange's own used-weight header.

        Traffic that never passed through this limiter still counts against
        the IP, so when the exchange reports we are near the configured budget
        we empty the bucket and let it refill at the safe rate.
        """
        if used >= self.budget:
            with self._lock:
                self._tokens = 0.0
                self._last = time.monotonic()

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            self._refill(now)
            return {"tokens": round(self._tokens, 1), "capacity": self.capacity,
                    "rate_per_s": round(self.rate, 2), "budget": self.budget,
                    "paused_for": max(0.0, self._until - now)}


def klines_weight(limit: int) -> int:
    """Binance futures klines weight tiers, by ``limit``."""
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


# fapi allows 2400 weight/min per IP. 2000 sustained + a 200 burst stays
# under it while leaving room for exchangeInfo / ticker / price polls.
BINANCE_WEIGHTS = WeightLimiter(
    float(os.environ.get("TSI_BINANCE_WEIGHT_PER_MIN", 2000)), name="Binance fapi")

# Bithumb's public API caps requests, not weight.
BITHUMB_LIMITER = RateLimiter(float(os.environ.get("TSI_CVD_RATE_BITHUMB", 18.0)))
