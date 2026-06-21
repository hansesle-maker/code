"""Logic tests for the signal engine — no network required.

Run directly:   python tests/test_signals.py
Or with pytest: pytest -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tsi_signal.backtest import BacktestParams, backtest_symbol
from tsi_signal.data import synthetic_candles, trend_closes
from tsi_signal.engine import _action, parse_time
from tsi_signal.indicators import ema, relative_strength, true_strength_index
from tsi_signal.signals import Direction, SignalParams, decide, evaluate_symbol, tsi_state


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
    kst = timezone(timedelta(hours=9))
    assert parse_time("") is None
    assert parse_time("1700000000000") == 1700000000000
    assert parse_time("1700000000") == 1700000000000  # seconds -> ms
    expect = int(datetime(2026, 6, 1, tzinfo=kst).timestamp() * 1000)  # naive -> KST
    assert parse_time("2026-06-01") == expect
    assert parse_time("2026/06/01") == expect
    expect_hm = int(datetime(2026, 6, 1, 8, 0, tzinfo=kst).timestamp() * 1000)
    assert parse_time("2026-06-01 08:00") == expect_hm
    assert parse_time("'2026-06-01 08:00") == expect_hm  # Excel text-prefix
    expect_utc = int(datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert parse_time("2026-06-01T08:00:00Z") == expect_utc  # explicit offset wins


# --------------------------------------------------------------------------- #
# TSI state model: zero line x signal line
# --------------------------------------------------------------------------- #
def test_tsi_state():
    assert tsi_state(50, 30) == 2    # >0 and >signal  -> confirmed up
    assert tsi_state(-50, -70) == 1  # <0 and >signal  -> turning up
    assert tsi_state(50, 70) == -1   # >0 and <signal  -> rolling over
    assert tsi_state(-50, -30) == -2  # <0 and <signal -> confirmed down


def _state_series(regime, recent, n_regime=200, n_recent=14):
    """Two-phase path: ``regime`` sets the TSI's side of zero, ``recent`` sets
    its side of the signal line. Magnitudes below land specific states."""
    base = trend_closes(n_regime, start=100.0, drift=regime, ripple=0.004)
    tail = [base[-1] * (1.0 + recent * i) for i in range(1, n_recent + 1)]
    return base + tail


# slopes -> intended state (verified by test_state_series_hits_targets)
S_PLUS2 = (0.0018, 0.004)
S_MINUS2 = (-0.0018, -0.004)
S_PLUS1 = (-0.004, 0.004)
S_MINUS1 = (0.004, -0.002)


def _eval(sym, s4, s1, params=None, **kw):
    return evaluate_symbol(
        sym, synthetic_candles(s4, "4h"), synthetic_candles(s1, "1h"),
        params=params or SignalParams(), **kw,
    )


def test_state_series_hits_targets():
    for slopes, want in [(S_PLUS2, 2), (S_MINUS2, -2), (S_PLUS1, 1), (S_MINUS1, -1)]:
        tsi, sig = true_strength_index(_state_series(*slopes))
        assert tsi_state(tsi[-1], sig[-1]) == want, (slopes, want)


# --------------------------------------------------------------------------- #
# decisions: gate (RS) x trigger (state) x conviction sizing
# --------------------------------------------------------------------------- #
def test_long_full_size():
    s = _state_series(*S_PLUS2)
    sig = _eval("ETHUSDT", s, s, sym_ref_close=s[0], bench_ref_close=100.0, bench_now_close=100.0)
    assert sig.gate == "long" and sig.state_4h == 2 and sig.state_1h == 2
    assert sig.direction is Direction.LONG
    assert sig.conviction == 4 and sig.size_fraction == 1.0


def test_long_partial_size_on_lower_conviction():
    s4, s1 = _state_series(*S_PLUS2), _state_series(*S_PLUS1)  # 4h +2, 1h +1
    sig = _eval("LINKUSDT", s4, s1, sym_ref_close=s4[0], bench_ref_close=100.0, bench_now_close=100.0)
    assert sig.state_4h == 2 and sig.state_1h == 1
    assert sig.direction is Direction.LONG
    assert sig.conviction == 3 and abs(sig.size_fraction - 0.6) < 1e-9


def test_short_full_size():
    s = _state_series(*S_MINUS2)
    sig = _eval("SOLUSDT", s, s, sym_ref_close=s[0], bench_ref_close=100.0, bench_now_close=100.0)
    assert sig.gate == "short" and sig.direction is Direction.SHORT
    assert sig.conviction == -4 and sig.size_fraction == 1.0


def test_flat_when_4h_not_confirmed():
    # 4h is +1 (rising but still below zero & signal) -> confirmed mode rejects.
    s4, s1 = _state_series(*S_PLUS1), _state_series(*S_PLUS2)
    sig = _eval("BTCUSDT", s4, s1, sym_ref_close=None, bench_ref_close=None,
                bench_now_close=None, is_benchmark=True)
    assert sig.state_4h == 1 and sig.direction is Direction.FLAT


def test_aggressive_mode_allows_state_plus1():
    s = _state_series(*S_PLUS1)  # +1 on both timeframes
    aggressive = SignalParams(require_zero_4h=False)
    sig = _eval("BTCUSDT", s, s, params=aggressive, sym_ref_close=None,
                bench_ref_close=None, bench_now_close=None, is_benchmark=True)
    assert sig.direction is Direction.LONG
    assert sig.conviction == 2 and abs(sig.size_fraction - 0.3) < 1e-9


def test_flat_when_1h_rolled_over():
    # 4h confirmed up (+2) but 1h is -1 (above zero yet below its signal) -> wait.
    s4, s1 = _state_series(*S_PLUS2), _state_series(*S_MINUS1)
    sig = _eval("XRPUSDT", s4, s1, sym_ref_close=s4[0], bench_ref_close=100.0, bench_now_close=100.0)
    assert sig.state_4h == 2 and sig.state_1h == -1
    assert sig.direction is Direction.FLAT


def test_gate_blocks_long_when_weak_vs_btc():
    # TSI is long-ready (+2/+2) but the coin is weaker than BTC -> no long.
    s = _state_series(*S_PLUS2)
    sig = _eval("XYZUSDT", s, s, sym_ref_close=s[0], bench_ref_close=100.0, bench_now_close=10000.0)
    assert sig.gate == "short" and sig.direction is Direction.FLAT


def test_benchmark_uses_tsi_only():
    s = _state_series(*S_PLUS2)
    sig = _eval("BTCUSDT", s, s, sym_ref_close=None, bench_ref_close=None,
                bench_now_close=None, is_benchmark=True)
    assert sig.direction is Direction.LONG
    assert sig.gate == "both" and sig.rs_vs_bench is None


def test_missing_ref_skips_gate_unless_required():
    s = _state_series(*S_PLUS2)
    sig = _eval("ETHUSDT", s, s, sym_ref_close=None, bench_ref_close=None, bench_now_close=100.0)
    assert sig.direction is Direction.LONG and sig.gate == "both"
    sig2 = _eval("ETHUSDT", s, s, params=SignalParams(require_ref=True),
                 sym_ref_close=None, bench_ref_close=None, bench_now_close=100.0)
    assert sig2.direction is Direction.FLAT and sig2.gate == "n/a"


# --------------------------------------------------------------------------- #
# backtester
# --------------------------------------------------------------------------- #
def test_backtest_long_uptrend_is_profitable():
    c4 = synthetic_candles(trend_closes(500, drift=0.004, ripple=0.01), "4h")
    c1 = synthetic_candles(trend_closes(2000, drift=0.001, ripple=0.01), "1h")
    bench_by_time = {c.open_time: 100.0 for c in c4}  # flat benchmark
    res = backtest_symbol("X", c4, c1, bench_by_time, SignalParams(),
                          BacktestParams(rs_gate="off", warmup=150))
    assert res.n_bars > 0 and len(res.equity) == res.n_bars + 1
    assert res.exposure > 0          # took long exposure during the uptrend
    assert res.total_return > 0      # and made money net of fees


def test_backtest_short_downtrend_beats_buyhold():
    c4 = synthetic_candles(trend_closes(500, drift=-0.004, ripple=0.01), "4h")
    c1 = synthetic_candles(trend_closes(2000, drift=-0.001, ripple=0.01), "1h")
    bench_by_time = {c.open_time: 100.0 for c in c4}
    res = backtest_symbol("X", c4, c1, bench_by_time, SignalParams(),
                          BacktestParams(rs_gate="off", warmup=150))
    assert res.buyhold_return < 0     # falling market
    assert res.total_return > res.buyhold_return  # shorting helps


def test_funding_drag_reduces_long_returns():
    c4 = synthetic_candles(trend_closes(500, drift=0.004, ripple=0.01), "4h")
    c1 = synthetic_candles(trend_closes(2000, drift=0.001, ripple=0.01), "1h")
    bench_by_time = {c.open_time: 100.0 for c in c4}
    base = backtest_symbol("X", c4, c1, bench_by_time, SignalParams(),
                           BacktestParams(rs_gate="off", warmup=150))
    funded = backtest_symbol("X", c4, c1, bench_by_time, SignalParams(),
                             BacktestParams(rs_gate="off", warmup=150, funding_apr=0.5))
    assert funded.total_return < base.total_return  # longs pay funding


# --------------------------------------------------------------------------- #
# hysteresis (stateful hold/exit)
# --------------------------------------------------------------------------- #
def test_hysteresis_holds_through_weak_state_and_exits_on_reversal():
    p = SignalParams(hysteresis=True)
    # fresh confirmed entry
    d, sz, _ = decide(2, 2, True, True, p, Direction.FLAT, 0.0)
    assert d is Direction.LONG and sz == 1.0
    # 4h rolls over to -1 (not yet -2): HOLD long, size sticky
    d, sz, _ = decide(-1, 2, True, True, p, Direction.LONG, 1.0)
    assert d is Direction.LONG and sz == 1.0
    # 4h reverses to -2 with short allowed: switch to short
    d, _, _ = decide(-2, -2, True, True, p, Direction.LONG, 1.0)
    assert d is Direction.SHORT
    # 4h reverses to -2 but gate forbids short: exit to flat
    d, _, _ = decide(-2, -2, True, False, p, Direction.LONG, 1.0)
    assert d is Direction.FLAT
    # gate flips (long no longer allowed): exit even if TSI still up
    d, _, _ = decide(2, 2, False, True, p, Direction.LONG, 1.0)
    assert d is Direction.FLAT
    # without hysteresis the -1 would already be flat
    d, _, _ = decide(-1, 2, True, True, SignalParams(), Direction.LONG, 1.0)
    assert d is Direction.FLAT


def test_long_only_blocks_shorts():
    p = SignalParams(long_only=True)
    assert decide(-2, -2, True, True, p)[0] is Direction.FLAT  # short setup blocked
    assert decide(2, 2, True, True, p)[0] is Direction.LONG    # long still fires


def test_exit_state_signal_cross_vs_full_reversal():
    p_cross = SignalParams(hysteresis=True, exit_state_4h=-1)  # exit when TSI<signal
    p_rev = SignalParams(hysteresis=True, exit_state_4h=-2)    # hold until full reversal
    # state +1 = TSI still ABOVE signal (just below zero): both HOLD the long
    assert decide(1, 1, True, False, p_cross, Direction.LONG, 1.0)[0] is Direction.LONG
    assert decide(1, 1, True, False, p_rev, Direction.LONG, 1.0)[0] is Direction.LONG
    # state -1 = TSI just crossed BELOW signal (still >0):
    assert decide(-1, 1, True, False, p_cross, Direction.LONG, 1.0)[0] is Direction.FLAT  # exit at cross
    assert decide(-1, 1, True, False, p_rev, Direction.LONG, 1.0)[0] is Direction.LONG    # still held


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
