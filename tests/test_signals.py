"""Logic tests for the signal engine — no network required.

Run directly:   python tests/test_signals.py
Or with pytest: pytest -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tsi_signal.data import synthetic_candles, trend_closes
from tsi_signal.engine import _action, parse_time
from tsi_signal.indicators import ema, relative_strength, true_strength_index
from tsi_signal.signals import Direction, SignalParams, evaluate_symbol


# --------------------------------------------------------------------------- #
# indicators
# --------------------------------------------------------------------------- #
def test_ema_constant_series_is_constant():
    assert all(abs(x - 1.0) < 1e-9 for x in ema([1.0] * 10, 3))


def test_ema_tracks_rising_input():
    out = ema([float(i) for i in range(20)], 5)
    assert out[-1] > out[0]
    assert all(out[i] <= out[i + 1] + 1e-9 for i in range(len(out) - 1))


def test_tsi_bounds_and_sign():
    tsi_up, _ = true_strength_index([100.0 + i for i in range(200)])
    tsi_down, _ = true_strength_index([300.0 - i for i in range(200)])
    tsi_flat, _ = true_strength_index([100.0] * 200)
    assert tsi_up[-1] > 90.0
    assert tsi_down[-1] < -90.0
    assert abs(tsi_flat[-1]) < 1e-9


def test_relative_strength_sign():
    assert relative_strength(130, 100, 100, 100) > 0  # outperformed flat bench
    assert relative_strength(80, 100, 100, 100) < 0   # underperformed
    assert relative_strength(0, 0, 100, 100) is None  # guarded


def test_parse_time():
    assert parse_time("") is None
    assert parse_time("1700000000000") == 1700000000000
    assert parse_time("1700000000") == 1700000000000  # seconds -> ms
    expect = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert parse_time("2026-06-01") == expect


# --------------------------------------------------------------------------- #
# signal decisions — gate (RS vs BTC) x trigger (own 4h/1h TSI)
# --------------------------------------------------------------------------- #
def _series(slope, n_base=200, n_tail=20):
    """Rippling flat base + a short directional tail (sets TSI sign/direction
    without saturating it at +-100)."""
    base = trend_closes(n_base, start=100.0, drift=0.0, ripple=0.02)
    tail = [base[-1] * (1.0 + slope * i) for i in range(1, n_tail + 1)]
    return base + tail


def test_long_when_strong_and_tsi_up():
    up = _series(+0.004)
    c4, c1 = synthetic_candles(up, "4h"), synthetic_candles(up, "1h")
    sig = evaluate_symbol("ETHUSDT", c4, c1, sym_ref_close=up[0],
                          bench_ref_close=100.0, bench_now_close=100.0, params=SignalParams())
    assert sig.direction is Direction.LONG
    assert sig.gate == "long" and sig.rs_vs_bench > 0
    assert sig.tsi_4h_slope > 0 and sig.tsi_1h >= 0


def test_short_when_weak_and_tsi_down():
    down = _series(-0.004)
    c4, c1 = synthetic_candles(down, "4h"), synthetic_candles(down, "1h")
    sig = evaluate_symbol("SOLUSDT", c4, c1, sym_ref_close=down[0],
                          bench_ref_close=100.0, bench_now_close=100.0, params=SignalParams())
    assert sig.direction is Direction.SHORT
    assert sig.gate == "short" and sig.rs_vs_bench < 0
    assert sig.tsi_4h_slope < 0 and sig.tsi_1h <= 0


def test_gate_blocks_long_when_weak_vs_btc():
    # TSI screams long (4h up, 1h>=0) but the coin is WEAKER than BTC since the
    # start time -> gate forbids long, 4h isn't down -> no short -> FLAT.
    up = _series(+0.004)
    c4, c1 = synthetic_candles(up, "4h"), synthetic_candles(up, "1h")
    sig = evaluate_symbol("XYZUSDT", c4, c1, sym_ref_close=up[0],
                          bench_ref_close=100.0, bench_now_close=200.0, params=SignalParams())
    assert sig.gate == "short"
    assert sig.direction is Direction.FLAT


def test_flat_when_1h_below_zero():
    # Strong vs BTC and 4h up, but 1h TSI < 0 -> long trigger fails -> FLAT.
    up, down = _series(+0.004), _series(-0.004)
    c4, c1 = synthetic_candles(up, "4h"), synthetic_candles(down, "1h")
    sig = evaluate_symbol("XRPUSDT", c4, c1, sym_ref_close=up[0],
                          bench_ref_close=100.0, bench_now_close=100.0, params=SignalParams())
    assert sig.gate == "long" and sig.tsi_1h < 0
    assert sig.direction is Direction.FLAT


def test_benchmark_uses_tsi_only():
    up = _series(+0.004)
    c4, c1 = synthetic_candles(up, "4h"), synthetic_candles(up, "1h")
    sig = evaluate_symbol("BTCUSDT", c4, c1, None, None, None, SignalParams(), is_benchmark=True)
    assert sig.direction is Direction.LONG
    assert sig.rs_vs_bench is None and sig.gate == "both"


def test_missing_ref_skips_gate_unless_required():
    up = _series(+0.004)
    c4, c1 = synthetic_candles(up, "4h"), synthetic_candles(up, "1h")
    # no start time -> gate skipped by default, trigger still fires
    sig = evaluate_symbol("ETHUSDT", c4, c1, None, None, 100.0, SignalParams())
    assert sig.direction is Direction.LONG and sig.gate == "both"
    # require_ref=True -> blocked
    sig2 = evaluate_symbol("ETHUSDT", c4, c1, None, None, 100.0, SignalParams(require_ref=True))
    assert sig2.direction is Direction.FLAT and sig2.gate == "n/a"


# --------------------------------------------------------------------------- #
# action mapping
# --------------------------------------------------------------------------- #
def test_action_mapping():
    assert _action(0, 1000) == "ENTER_LONG"
    assert _action(0, -1000) == "ENTER_SHORT"
    assert _action(1000, -1000) == "SWITCH_TO_SHORT"
    assert _action(-1000, 1000) == "SWITCH_TO_LONG"
    assert _action(1000, 0) == "EXIT"
    assert _action(500, 1000) == "ADD"
    assert _action(1000, 500) == "REDUCE"
    assert _action(1000, 1000) == "HOLD"


# --------------------------------------------------------------------------- #
# runner (so the file works without pytest installed)
# --------------------------------------------------------------------------- #
def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {fn.__name__}: {exc!r}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
