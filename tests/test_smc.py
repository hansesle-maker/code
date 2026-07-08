"""Offline tests for the SMC engine (tsi_signal.smc) — no network needed."""
from __future__ import annotations

import math
import unittest

from tsi_signal.data import Candle, synthetic_candles, trend_closes
from tsi_signal.smc import SMCParams, SMCResult, analyze


def wave_closes(n: int, period: float = 160.0, amp: float = 0.08,
                drift: float = 0.0002, start: float = 100.0) -> list:
    """A trending double sine wave: the slow leg flips the swing (50)
    structure while the fast ripple creates distinct internal (5) structure
    (real markets have both scales; a single smooth sine would make internal
    and swing pivots identical, which the engine de-duplicates by design)."""
    return [
        start * (1.0 + drift * i)
        * (1.0 + amp * math.sin(2.0 * math.pi * i / period))
        * (1.0 + 0.02 * math.sin(2.0 * math.pi * i / 22.0))
        for i in range(n)
    ]


class TestSMCEngine(unittest.TestCase):
    def test_empty_and_tiny_input(self):
        self.assertEqual(analyze([], symbol="X").error, "no data")
        candles = synthetic_candles(trend_closes(10, drift=0.001), interval="1h")
        res = analyze(candles, symbol="X", tf="1h")
        self.assertIsNone(res.error)
        self.assertEqual(res.position, "FLAT")

    def test_wave_market_produces_structure_and_trades(self):
        candles = synthetic_candles(wave_closes(600), interval="1h")
        res = analyze(candles, symbol="WAVE", tf="1h")
        self.assertIsNone(res.error)
        # oscillating market must produce structure breaks and trades
        self.assertIsNotNone(res.last_signal)
        self.assertIn(res.last_signal, ("BOS", "CHoCH"))
        self.assertIn(res.last_signal_structure, ("internal", "swing"))
        self.assertGreater(res.trades + (0 if res.position == "FLAT" else 1), 0)
        self.assertIn(res.internal_trend, (-1, 1))
        # trailing range must be sane once initialised
        if res.trailing_top is not None and res.trailing_bottom is not None:
            self.assertGreater(res.trailing_top, res.trailing_bottom)
            self.assertAlmostEqual(
                res.equilibrium, 0.5 * (res.trailing_top + res.trailing_bottom))
            self.assertGreaterEqual(res.range_pos, 0.0)
            self.assertLessEqual(res.range_pos, 100.0)
            self.assertIn(res.zone, ("PREMIUM", "EQUILIBRIUM", "DISCOUNT"))

    def test_position_invariants(self):
        candles = synthetic_candles(wave_closes(600, period=120, amp=0.06),
                                    interval="4h")
        res = analyze(candles, symbol="INV", tf="4h")
        self.assertIn(res.position, ("LONG", "SHORT", "FLAT"))
        if res.position == "LONG":
            self.assertIsNotNone(res.entry_price)
            if res.stop_loss is not None:
                self.assertLess(res.stop_loss, res.entry_price)
            if res.take_profit is not None:
                self.assertGreater(res.take_profit, res.entry_price)
        elif res.position == "SHORT":
            self.assertIsNotNone(res.entry_price)
            if res.stop_loss is not None:
                self.assertGreater(res.stop_loss, res.entry_price)
            if res.take_profit is not None:
                self.assertLess(res.take_profit, res.entry_price)
        else:
            self.assertIsNone(res.entry_price)
        # win/trade accounting must be consistent
        self.assertGreaterEqual(res.trades, res.wins)

    def test_percent_stop_is_respected(self):
        params = SMCParams(stop_type="percent", stop_percent=2.0, tp_type="rr",
                           risk_reward=2.0)
        candles = synthetic_candles(wave_closes(600), interval="1h")
        res = analyze(candles, symbol="PCT", tf="1h", params=params)
        if res.position == "LONG" and res.stop_loss is not None:
            self.assertAlmostEqual(res.stop_loss, res.entry_price * 0.98, places=6)
        if res.position == "SHORT" and res.stop_loss is not None:
            self.assertAlmostEqual(res.stop_loss, res.entry_price * 1.02, places=6)

    def test_long_only_never_short(self):
        params = SMCParams(allow_short=False)
        candles = synthetic_candles(wave_closes(600), interval="1h")
        res = analyze(candles, symbol="LO", tf="1h", params=params)
        self.assertIn(res.position, ("LONG", "FLAT"))

    def test_swing_entries_only(self):
        params = SMCParams(entry_structure="swing")
        candles = synthetic_candles(wave_closes(800, period=200, amp=0.10),
                                    interval="1h")
        res = analyze(candles, symbol="SW", tf="1h", params=params)
        self.assertIsNone(res.error)
        # swing structure flips on a 200-bar wave, so a position or at least
        # one completed trade must exist
        self.assertTrue(res.trades > 0 or res.position != "FLAT")

    def test_result_serializable(self):
        from tsi_signal.smc_scanner import smc_to_dict
        candles = synthetic_candles(wave_closes(400), interval="1h")
        res = analyze(candles, symbol="SER", tf="1h")
        d = smc_to_dict(res)
        self.assertEqual(d["symbol"], "SER")
        self.assertIn("stop_loss", d)
        self.assertIn("take_profit", d)
        self.assertIn("position", d)


if __name__ == "__main__":
    unittest.main()
