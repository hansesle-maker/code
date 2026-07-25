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
from collections import deque
from typing import Deque, Tuple

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
    """Sliding-window weight budget shared by every caller in the process.

    ``acquire`` blocks until spending ``weight`` keeps the last ``window``
    seconds under ``budget``. ``penalize`` parks all callers after a 429/418,
    and ``observe`` re-syncs the window from the exchange's own accounting
    header so we stay honest even if some traffic bypassed the limiter.
    """

    def __init__(self, budget: float, window: float = 60.0, name: str = ""):
        self.budget = max(1.0, float(budget))
        self.window = window
        self.name = name
        self._lock = threading.Lock()
        self._spent: Deque[Tuple[float, float]] = deque()
        self._until = 0.0          # global pause deadline (monotonic)

    # -- internals ---------------------------------------------------------
    def _prune(self, now: float) -> float:
        while self._spent and self._spent[0][0] <= now - self.window:
            self._spent.popleft()
        return sum(w for _, w in self._spent)

    # -- api ---------------------------------------------------------------
    def acquire(self, weight: float = 1.0) -> None:
        # A single call heavier than the whole budget would never fit; clamp so
        # it waits for an empty window instead of spinning forever.
        weight = min(float(weight), self.budget)
        while True:
            with self._lock:
                now = time.monotonic()
                if now >= self._until:
                    used = self._prune(now)
                    if used + weight <= self.budget:
                        self._spent.append((now, weight))
                        return
                    # wait for the oldest slice to age out of the window
                    sleep_for = self._spent[0][0] + self.window - now if self._spent else 0.05
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
        """Sync from the exchange's used-weight header for this window.

        Capped at ``budget`` so a high reading slows us down without parking
        everyone for a full window.
        """
        with self._lock:
            now = time.monotonic()
            tracked = self._prune(now)
            delta = min(float(used), self.budget) - tracked
            if delta > 0:           # someone spent weight without asking us
                self._spent.append((now, delta))

    def snapshot(self) -> dict:
        with self._lock:
            return {"used": self._prune(time.monotonic()), "budget": self.budget,
                    "paused_for": max(0.0, self._until - time.monotonic())}


def klines_weight(limit: int) -> int:
    """Binance futures klines weight tiers, by ``limit``."""
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


# fapi allows 2400 weight/min per IP. Default to 2000 so bursts from
# exchangeInfo / ticker / the compound page's price polls still fit.
BINANCE_WEIGHTS = WeightLimiter(
    float(os.environ.get("TSI_BINANCE_WEIGHT_PER_MIN", 2000)), name="Binance fapi")

# Bithumb's public API caps requests, not weight.
BITHUMB_LIMITER = RateLimiter(float(os.environ.get("TSI_CVD_RATE_BITHUMB", 18.0)))
