"""Shared client-side throttle for Binance Futures REST calls.

The TSI scanner and the SMC scanner both hit the same IP-level rate-limit
budget. Scanning ~300+ symbols concurrently across two independent scanners
can burst past Binance's request-weight budget, which escalates from HTTP
429 ("rate limit warning") to HTTP 418 ("I'm a teapot" — the IP is now
banned for a cooldown period, and hammering it further only extends the
ban). This module gives every caller a shared, conservative sliding-window
weight budget plus a shared cooldown flag, so a ban detected by one scanner
immediately stops the other from making it worse.
"""
from __future__ import annotations

import threading
import time
from collections import deque

# Binance USDⓈ-M futures caps at 2400 request-weight per rolling minute.
# Budget well under that: two scanners share this pool, plus manual
# refreshes, plus margin for weight estimates being approximate.
DEFAULT_WEIGHT_BUDGET = 1000.0
DEFAULT_WINDOW = 60.0


class BinanceBanned(RuntimeError):
    """Raised when Binance returns 429/418; carries the cooldown deadline."""

    def __init__(self, status_code: int, retry_after: float):
        self.status_code = status_code
        self.retry_after = retry_after
        until = time.strftime("%H:%M:%S", time.localtime(time.time() + retry_after))
        kind = "IP 차단(418)" if status_code == 418 else "요청 제한 경고(429)"
        super().__init__(
            f"Binance {kind} — {retry_after:.0f}초 대기 필요 (해제 예정 약 {until})"
        )


class RateLimiter:
    """Thread-safe sliding-window request-weight limiter with a shared cooldown."""

    def __init__(self, weight_budget: float = DEFAULT_WEIGHT_BUDGET,
                window: float = DEFAULT_WINDOW):
        self._budget = weight_budget
        self._window = window
        self._events: deque = deque()  # (timestamp, weight)
        self._used = 0.0
        self._lock = threading.Lock()
        self._banned_until = 0.0

    def wait_if_banned(self) -> None:
        """Raise immediately (no sleep) if a ban is currently in effect."""
        remaining = self._banned_until - time.time()
        if remaining > 0:
            raise BinanceBanned(418, remaining)

    def acquire(self, weight: float = 1.0) -> None:
        """Block until ``weight`` fits the rolling budget; raise if banned."""
        while True:
            self.wait_if_banned()
            with self._lock:
                now = time.time()
                while self._events and now - self._events[0][0] > self._window:
                    _, w = self._events.popleft()
                    self._used -= w
                if self._used + weight <= self._budget:
                    self._events.append((now, weight))
                    self._used += weight
                    return
                oldest_ts = self._events[0][0] if self._events else now
                sleep_for = max(0.05, self._window - (now - oldest_ts))
            time.sleep(min(sleep_for, 2.0))

    def note_error(self, exc: Exception) -> None:
        """Inspect an HTTP error; if it's a 429/418, arm the shared cooldown
        and re-raise as :class:`BinanceBanned` so callers stop immediately."""
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
        if status not in (429, 418):
            return
        retry_after = 90.0 if status == 418 else 30.0
        if resp is not None:
            hdr = resp.headers.get("Retry-After")
            if hdr:
                try:
                    retry_after = float(hdr)
                except ValueError:
                    pass
        with self._lock:
            self._banned_until = max(self._banned_until, time.time() + retry_after)
        raise BinanceBanned(status, retry_after) from exc


# One shared limiter for every futures REST call across both scanners.
FUTURES_LIMITER = RateLimiter()
