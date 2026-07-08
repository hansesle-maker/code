"""Offline tests for tsi_signal.rate_limit — no network needed.

Uses fresh RateLimiter instances (not the shared FUTURES_LIMITER singleton)
so tests don't interfere with each other.
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock

from tsi_signal.rate_limit import BinanceBanned, RateLimiter


class TestRateLimiter(unittest.TestCase):
    def test_acquire_within_budget_does_not_block(self):
        rl = RateLimiter(weight_budget=100, window=60)
        start = time.time()
        for _ in range(10):
            rl.acquire(2)
        self.assertLess(time.time() - start, 0.5)

    def test_acquire_blocks_and_prunes_old_events(self):
        rl = RateLimiter(weight_budget=10, window=0.2)
        rl.acquire(10)  # exhaust the budget
        start = time.time()
        rl.acquire(5)   # must wait for the 0.2s window to roll off
        self.assertGreaterEqual(time.time() - start, 0.15)

    def test_note_error_ignores_non_rate_limit_status(self):
        rl = RateLimiter()
        exc = Exception("boom")
        exc.response = MagicMock(status_code=500, headers={})
        rl.note_error(exc)  # must not raise
        rl.wait_if_banned()  # must not raise either

    def test_note_error_429_arms_cooldown_and_raises(self):
        rl = RateLimiter()
        exc = Exception("rate limited")
        exc.response = MagicMock(status_code=429, headers={"Retry-After": "5"})
        with self.assertRaises(BinanceBanned) as ctx:
            rl.note_error(exc)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertAlmostEqual(ctx.exception.retry_after, 5.0, delta=0.5)
        with self.assertRaises(BinanceBanned):
            rl.wait_if_banned()
        with self.assertRaises(BinanceBanned):
            rl.acquire(1)

    def test_note_error_418_defaults_retry_after_without_header(self):
        rl = RateLimiter()
        exc = Exception("banned")
        exc.response = MagicMock(status_code=418, headers={})
        with self.assertRaises(BinanceBanned) as ctx:
            rl.note_error(exc)
        self.assertEqual(ctx.exception.status_code, 418)
        self.assertGreater(ctx.exception.retry_after, 0)

    def test_ban_expires_after_retry_after(self):
        rl = RateLimiter()
        exc = Exception("banned")
        exc.response = MagicMock(status_code=429, headers={"Retry-After": "0.1"})
        with self.assertRaises(BinanceBanned):
            rl.note_error(exc)
        time.sleep(0.2)
        rl.wait_if_banned()  # must not raise once the cooldown elapsed
        rl.acquire(1)        # must not raise either


if __name__ == "__main__":
    unittest.main()
