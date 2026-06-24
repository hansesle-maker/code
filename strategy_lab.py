#!/usr/bin/env python3
"""Strategy lab — backtest several TSI entry/exit rules on REAL BTC data and
compare them honestly (look-ahead-free, fees included).

This is a clean rewrite that does NOT use the old signals.py/engine.py strategy
(that approach was retired). It only reuses the TSI math (indicators.py) and the
kline fetcher (data.py).

Everything is evaluated on a single 15-minute time grid. Higher-timeframe TSI
(1h, 4h) is computed from resampled candles and forward-filled onto the 15m grid
using only CLOSED higher-TF bars, so no strategy can peek into the future.

Usage (run on a host that can reach Binance, e.g. your Oracle VM):

    # ~6 months of BTC 15m, compare all strategies:
    python strategy_lab.py --symbol BTCUSDT --days 180

    # a specific date range, custom fee:
    python strategy_lab.py --symbol BTCUSDT --start 2024-01-01 --fee-bps 5

    # offline self-test on synthetic data (no network):
    python strategy_lab.py --selftest
"""
from __future__ import annotations

import argparse
import itertools
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from tsi_signal.data import (
    FUTURES_BASE_URL,
    FUTURES_KLINES_PATH,
    Candle,
    fetch_klines_range,
)
from tsi_signal.indicators import ema, true_strength_index

# 15m bars per higher timeframe.
BARS_PER_1H = 4
BARS_PER_4H = 16
MS_15M = 15 * 60 * 1000


# --------------------------------------------------------------------------- #
# Resampling + indicators (all look-ahead safe)
# --------------------------------------------------------------------------- #
def resample(candles_15m: List[Candle], factor: int) -> List[Candle]:
    """Aggregate 15m candles into `factor`-sized buckets, aligned to epoch.

    A bucket is emitted only when it is COMPLETE (has `factor` bars), so the
    last (forming) higher-TF bar is never produced — that keeps the backtest
    honest about what was actually closed at decision time.
    """
    if not candles_15m:
        return []
    step = MS_15M * factor
    out: List[Candle] = []
    bucket: List[Candle] = []
    cur_key = candles_15m[0].open_time // step
    for c in candles_15m:
        key = c.open_time // step
        if key != cur_key:
            if len(bucket) == factor:
                out.append(_merge(bucket))
            bucket = []
            cur_key = key
        bucket.append(c)
    if len(bucket) == factor:
        out.append(_merge(bucket))
    return out


def _merge(bucket: List[Candle]) -> Candle:
    return Candle(
        open_time=bucket[0].open_time,
        open=bucket[0].open,
        high=max(c.high for c in bucket),
        low=min(c.low for c in bucket),
        close=bucket[-1].close,
        volume=sum(c.volume for c in bucket),
    )


def atr(candles: List[Candle], period: int = 14) -> List[float]:
    """Wilder's ATR as a plain list aligned to `candles` (seeded with TR)."""
    if not candles:
        return []
    trs: List[float] = [candles[0].high - candles[0].low]
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    # Wilder smoothing == EMA with alpha = 1/period
    out: List[float] = []
    prev = trs[0]
    a = 1.0 / period
    for tr in trs:
        prev = tr * a + prev * (1.0 - a)
        out.append(prev)
    return out


def _ffill_higher_tf(
    closes_15m_times: List[int],
    htf_candles: List[Candle],
    htf_series: List[float],
) -> List[float]:
    """Map a higher-TF series onto the 15m grid using only CLOSED htf bars.

    For a 15m bar that closes at t+15m, the latest usable htf value is the one
    whose bar already closed by then, i.e. htf_close_time <= this bar's close.
    htf bar [open, open+span) closes at open+span. We forward-fill that value
    onto every 15m bar at or after the close.
    """
    if not htf_candles:
        return [0.0] * len(closes_15m_times)
    span = htf_candles[1].open_time - htf_candles[0].open_time if len(htf_candles) > 1 else MS_15M
    close_times = [c.open_time + span for c in htf_candles]  # when each htf bar closes
    out: List[float] = []
    j = -1  # index of the latest htf bar known to be closed
    n = len(htf_candles)
    for t in closes_15m_times:
        bar_close = t + MS_15M
        while j + 1 < n and close_times[j + 1] <= bar_close:
            j += 1
        out.append(htf_series[j] if j >= 0 else 0.0)
    return out


@dataclass
class Indicators:
    """All look-ahead-safe series on the 15m grid."""
    times: List[int]
    close: List[float]
    high: List[float]
    low: List[float]
    tsi15: List[float]
    sig15: List[float]
    tsi1h: List[float]
    sig1h: List[float]
    tsi4h: List[float]
    sig4h: List[float]
    atr15: List[float]


def build_indicators(c15: List[Candle], tsi_params=(25, 13, 13)) -> Indicators:
    L, S, G = tsi_params
    times = [c.open_time for c in c15]
    close = [c.close for c in c15]
    high = [c.high for c in c15]
    low = [c.low for c in c15]

    tsi15, sig15 = true_strength_index(close, L, S, G)

    c1h = resample(c15, BARS_PER_1H)
    t1, s1 = true_strength_index([c.close for c in c1h], L, S, G)
    tsi1h = _ffill_higher_tf(times, c1h, t1)
    sig1h = _ffill_higher_tf(times, c1h, s1)

    c4h = resample(c15, BARS_PER_4H)
    t4, s4 = true_strength_index([c.close for c in c4h], L, S, G)
    tsi4h = _ffill_higher_tf(times, c4h, t4)
    sig4h = _ffill_higher_tf(times, c4h, s4)

    return Indicators(times, close, high, low, tsi15, sig15,
                      tsi1h, sig1h, tsi4h, sig4h, atr(c15))


# --------------------------------------------------------------------------- #
# Strategy interface
# --------------------------------------------------------------------------- #
# A strategy looks at the indicator bundle `ind` at bar `i` (already closed) and
# the current position `pos` (+1 long / -1 short / 0 flat), and returns the
# DESIRED position for the next bar: +1, -1, or 0. The engine fills at the next
# bar's open. Strategies must read only values at index i (no i+1).
StrategyFn = Callable[[Indicators, int, int, dict], int]


def _crossed_up(a: List[float], b: List[float], i: int) -> bool:
    return i > 0 and a[i - 1] <= b[i - 1] and a[i] > b[i]


def _crossed_dn(a: List[float], b: List[float], i: int) -> bool:
    return i > 0 and a[i - 1] >= b[i - 1] and a[i] < b[i]


def s1_15m_cross(ind, i, pos, st):
    if _crossed_up(ind.tsi15, ind.sig15, i):
        return 1
    if _crossed_dn(ind.tsi15, ind.sig15, i):
        return -1
    return pos


def s2_1h_cross(ind, i, pos, st):
    if _crossed_up(ind.tsi1h, ind.sig1h, i):
        return 1
    if _crossed_dn(ind.tsi1h, ind.sig1h, i):
        return -1
    return pos


def s3_4hdir_1h_cross(ind, i, pos, st):
    up4 = ind.tsi4h[i] > 0
    dn4 = ind.tsi4h[i] < 0
    if up4 and _crossed_up(ind.tsi1h, ind.sig1h, i):
        return 1
    if dn4 and _crossed_dn(ind.tsi1h, ind.sig1h, i):
        return -1
    # exit when the 1h momentum rolls back against us
    if pos == 1 and _crossed_dn(ind.tsi1h, ind.sig1h, i):
        return 0
    if pos == -1 and _crossed_up(ind.tsi1h, ind.sig1h, i):
        return 0
    return pos


def s4_1h_zero(ind, i, pos, st):
    if i == 0:
        return pos
    up = ind.tsi1h[i - 1] <= 0 < ind.tsi1h[i]
    dn = ind.tsi1h[i - 1] >= 0 > ind.tsi1h[i]
    if up:
        return 1
    if dn:
        return -1
    return pos


def s5_1h_oversold_rev(ind, i, pos, st, lvl=25.0):
    # enter long when 1h TSI turns up from deep oversold; exit when it reaches >0
    if pos == 0:
        if ind.tsi1h[i] < -lvl and _crossed_up(ind.tsi1h, ind.sig1h, i):
            return 1
        if ind.tsi1h[i] > lvl and _crossed_dn(ind.tsi1h, ind.sig1h, i):
            return -1
        return 0
    if pos == 1 and ind.tsi1h[i] > 0:
        return 0
    if pos == -1 and ind.tsi1h[i] < 0:
        return 0
    return pos


def s6_4hdir_1h_cross_atr(ind, i, pos, st, mult=2.5):
    """S3 entries, but exit on an ATR trailing stop instead of the 1h cross."""
    price = ind.close[i]
    a = ind.atr15[i]
    if pos == 0:
        st.pop("trail", None)
        if ind.tsi4h[i] > 0 and _crossed_up(ind.tsi1h, ind.sig1h, i):
            st["trail"] = price - mult * a
            return 1
        if ind.tsi4h[i] < 0 and _crossed_dn(ind.tsi1h, ind.sig1h, i):
            st["trail"] = price + mult * a
            return -1
        return 0
    if pos == 1:
        st["trail"] = max(st.get("trail", price - mult * a), price - mult * a)
        if price <= st["trail"]:
            return 0
        return 1
    if pos == -1:
        st["trail"] = min(st.get("trail", price + mult * a), price + mult * a)
        if price >= st["trail"]:
            return 0
        return -1
    return pos


STRATEGIES: Dict[str, StrategyFn] = {
    "S1_15m_cross": s1_15m_cross,
    "S2_1h_cross": s2_1h_cross,
    "S3_4hdir+1h": s3_4hdir_1h_cross,
    "S4_1h_zero": s4_1h_zero,
    "S5_1h_oversold": s5_1h_oversold_rev,
    "S6_4hdir+1h+ATR": s6_4hdir_1h_cross_atr,
}


# --------------------------------------------------------------------------- #
# Backtest engine
# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    side: int
    entry_i: int
    exit_i: int
    entry_px: float
    exit_px: float
    ret: float  # net return on this trade (after fees), as a fraction


@dataclass
class Result:
    name: str
    trades: List[Trade]
    equity: List[float]
    bars: int
    bar_ms: int = MS_15M

    @property
    def total_return(self) -> float:
        return self.equity[-1] - 1.0

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.ret > 0) / len(self.trades)

    @property
    def profit_factor(self) -> float:
        gains = sum(t.ret for t in self.trades if t.ret > 0)
        losses = -sum(t.ret for t in self.trades if t.ret < 0)
        if losses == 0:
            return math.inf if gains > 0 else 0.0
        return gains / losses

    @property
    def avg_win(self) -> float:
        w = [t.ret for t in self.trades if t.ret > 0]
        return sum(w) / len(w) if w else 0.0

    @property
    def avg_loss(self) -> float:
        l = [t.ret for t in self.trades if t.ret < 0]
        return sum(l) / len(l) if l else 0.0

    @property
    def max_drawdown(self) -> float:
        peak = self.equity[0]
        mdd = 0.0
        for e in self.equity:
            peak = max(peak, e)
            mdd = min(mdd, e / peak - 1.0)
        return mdd

    @property
    def cagr(self) -> float:
        years = (self.bars * self.bar_ms / 1000) / (365.25 * 24 * 3600)
        if years <= 0 or self.equity[-1] <= 0:
            return 0.0
        return self.equity[-1] ** (1.0 / years) - 1.0

    @property
    def sharpe(self) -> float:
        # per-bar equity returns annualised (15m bars => 35040 per year)
        rets = [self.equity[k] / self.equity[k - 1] - 1.0 for k in range(1, len(self.equity))]
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        sd = math.sqrt(var)
        if sd == 0:
            return 0.0
        bars_per_year = (365.25 * 24 * 3600) / (self.bar_ms / 1000)
        return mean / sd * math.sqrt(bars_per_year)


def run_backtest(
    ind: Indicators,
    strategy: StrategyFn,
    name: str,
    fee_rate: float = 0.0005,
    slip_rate: float = 0.0,
    warmup: int = BARS_PER_4H * 60,  # ~60 4h-bars of TSI warm-up
    allow_short: bool = True,
) -> Result:
    """Honest next-bar-fill backtest.

    Timing: a decision uses information up to the close of bar ``i`` and the
    resulting position is held starting from bar ``i+1`` (filled at its open,
    which for contiguous 15m candles ≈ ``close[i]``). Cost = ``fee+slippage``
    is charged per side changed, so a long→short flip pays it twice. Trade-level
    and equity-level accounting use the SAME fills and costs so they reconcile.
    """
    n = len(ind.close)
    close = ind.close
    cost = fee_rate + slip_rate

    # 1) decision at the close of each bar i (effective from bar i+1's open)
    pos = 0
    st: dict = {}
    decision = [0] * n
    for i in range(n):
        if i < warmup:
            continue
        d = strategy(ind, i, pos, st)
        if not allow_short and d == -1:
            d = 0
        pos = d
        decision[i] = d

    # 2) walk forward; position held DURING bar i is decision[i-1].
    equity = [1.0]
    eq = 1.0
    trades: List[Trade] = []
    entry_px = 0.0
    entry_i = 0
    prev_held = 0
    for i in range(1, n):
        held = decision[i - 1]
        if held != prev_held:                      # position changes at open[i]
            fill = close[i - 1]                    # ≈ open[i]
            if prev_held != 0:                     # close the old trade
                gross = prev_held * (fill / entry_px - 1.0)
                trades.append(Trade(prev_held, entry_i, i, entry_px, fill, gross - 2 * cost))
            eq *= (1.0 - cost * abs(held - prev_held))   # |Δpos| sides changed
            if held != 0:                          # open the new trade
                entry_px = fill
                entry_i = i
        if held != 0:                              # mark to market over bar i
            eq *= (1.0 + held * (close[i] / close[i - 1] - 1.0))
        equity.append(eq)
        prev_held = held

    if prev_held != 0 and entry_px:                # close the final open trade
        fill = close[-1]
        gross = prev_held * (fill / entry_px - 1.0)
        trades.append(Trade(prev_held, entry_i, n - 1, entry_px, fill, gross - 2 * cost))

    return Result(name=name, trades=trades, equity=equity, bars=n)


def buy_hold(ind: Indicators) -> Result:
    eq = [1.0]
    for i in range(1, len(ind.close)):
        eq.append(eq[-1] * (ind.close[i] / ind.close[i - 1]))
    return Result("Buy&Hold", [], eq, len(ind.close))


# --------------------------------------------------------------------------- #
# Dynamic strategy builder  (used by the web Strategy Lab)
# --------------------------------------------------------------------------- #

_FLIP_DIRS: Dict[str, Dict[str, str]] = {
    "tsi_vs_zero":   {"above": "below", "below": "above"},
    "sig_vs_zero":   {"above": "below", "below": "above"},
    "zero_cross":    {"up": "down", "down": "up"},
    "tsi_vs_signal": {"above": "below", "below": "above"},
    "fresh_cross":   {"up": "down", "down": "up"},
    "gap_change":    {"expanding": "contracting", "contracting": "expanding"},
    "tsi_slope":     {"rising": "falling", "falling": "rising"},
    "sig_slope":     {"rising": "falling", "falling": "rising"},
}


def _flip_dir(ctype: str, direction: str) -> str:
    return _FLIP_DIRS.get(ctype, {}).get(direction, direction)


def _get_tsi_sig(ind: Indicators, tf: str) -> Tuple[List[float], List[float]]:
    if tf == "15m":
        return ind.tsi15, ind.sig15
    if tf == "1h":
        return ind.tsi1h, ind.sig1h
    if tf == "4h":
        return ind.tsi4h, ind.sig4h
    raise ValueError(f"Unknown timeframe: {tf!r}")


def _eval_cond(ind: Indicators, i: int, ctype: str, direction: str, tf: str) -> bool:
    """Evaluate one condition at bar i (caller handles direction flipping)."""
    tsi, sig = _get_tsi_sig(ind, tf)
    if ctype == "tsi_vs_zero":
        return tsi[i] > 0 if direction == "above" else tsi[i] < 0
    if ctype == "sig_vs_zero":
        return sig[i] > 0 if direction == "above" else sig[i] < 0
    if ctype == "zero_cross":
        if i == 0:
            return False
        if direction == "up":
            return tsi[i - 1] <= 0 < tsi[i]
        return tsi[i - 1] >= 0 > tsi[i]
    if ctype == "tsi_vs_signal":
        return tsi[i] > sig[i] if direction == "above" else tsi[i] < sig[i]
    if ctype == "fresh_cross":
        if i == 0:
            return False
        if direction == "up":
            return tsi[i - 1] <= sig[i - 1] and tsi[i] > sig[i]
        return tsi[i - 1] >= sig[i - 1] and tsi[i] < sig[i]
    if ctype == "gap_change":
        if i == 0:
            return False
        gap_now  = tsi[i]     - sig[i]
        gap_prev = tsi[i - 1] - sig[i - 1]
        return gap_now > gap_prev if direction == "expanding" else gap_now < gap_prev
    if ctype == "tsi_slope":
        if i == 0:
            return False
        return tsi[i] > tsi[i - 1] if direction == "rising" else tsi[i] < tsi[i - 1]
    if ctype == "sig_slope":
        if i == 0:
            return False
        return sig[i] > sig[i - 1] if direction == "rising" else sig[i] < sig[i - 1]
    return False


def _eval_rule(ind: Indicators, i: int, rule: dict, flip: bool) -> bool:
    """Evaluate an entry/exit rule at bar i.

    rule structure::
        {"tf_logic": "AND"|"OR",
         "4h": {"logic": "AND", "conditions": [{"type": .., "dir": ..}, ...]},
         "1h": {...}, "15m": {...}}

    flip=True mirrors all directions (used for the short side).
    Returns False when the rule has no conditions (no constraint).
    """
    tf_results = []
    for tf in ("4h", "1h", "15m"):
        grp = rule.get(tf, {})
        conds = grp.get("conditions", [])
        if not conds:
            continue
        logic = grp.get("logic", "AND")
        res = []
        for c in conds:
            d = _flip_dir(c["type"], c["dir"]) if flip else c["dir"]
            res.append(_eval_cond(ind, i, c["type"], d, tf))
        tf_results.append(all(res) if logic == "AND" else any(res))

    if not tf_results:
        return False
    tf_logic = rule.get("tf_logic", "AND")
    return all(tf_results) if tf_logic == "AND" else any(tf_results)


def build_dynamic_strategy(
    entry_rule: dict,
    exit_rule: dict,
    allow_short: bool = True,
) -> StrategyFn:
    """Build a StrategyFn from entry/exit rule configuration dicts.

    Entry conditions are expressed in the **bullish** direction (LONG triggers).
    Exit conditions are expressed in the **bearish** direction (LONG-exit triggers).
    SHORT entries and exits are the automatic mirror of both.

    If no exit conditions are given, a position is only closed when the
    opposite entry fires (direct flip) or the entry drops away (stays open).
    """
    def strategy(ind: Indicators, i: int, pos: int, st: dict) -> int:
        long_entry  = _eval_rule(ind, i, entry_rule, flip=False)
        short_entry = allow_short and _eval_rule(ind, i, entry_rule, flip=True)
        long_exit   = _eval_rule(ind, i, exit_rule,  flip=False)
        short_exit  = _eval_rule(ind, i, exit_rule,  flip=True)

        if pos == 1:                          # holding long
            if long_exit or short_entry:
                return -1 if short_entry else 0
            return 1
        if pos == -1:                         # holding short
            if short_exit or long_entry:
                return 1 if long_entry else 0
            return -1
        # flat → enter on first signal
        if long_entry:
            return 1
        if short_entry:
            return -1
        return 0

    return strategy


def run_lab_backtest(c15: List[Candle], config: dict) -> dict:
    """Run a dynamic-strategy backtest and return a JSON-serialisable result dict.

    Required config keys: ``entry`` (rule dict), ``exit`` (rule dict).
    Optional: ``fee_bps`` (default 5), ``slip_bps`` (default 1),
    ``allow_short`` (default True), ``tsi_long/short/signal`` (default 25/13/13).
    """
    fee_rate    = config.get("fee_bps",  5.0) / 10000.0
    slip_rate   = config.get("slip_bps", 1.0) / 10000.0
    allow_short = bool(config.get("allow_short", True))
    tsi_params  = (
        int(config.get("tsi_long",   25)),
        int(config.get("tsi_short",  13)),
        int(config.get("tsi_signal", 13)),
    )

    ind = build_indicators(c15, tsi_params)
    fn  = build_dynamic_strategy(config["entry"], config["exit"], allow_short)
    res = run_backtest(ind, fn, "custom", fee_rate, slip_rate, allow_short=allow_short)
    bh  = buy_hold(ind)

    # Down-sample to ≤ 2000 points for the browser chart
    n    = len(res.equity)
    step = max(1, n // 2000)

    def _stats(r: Result) -> dict:
        pf = r.profit_factor
        return {
            "total_return":  round(r.total_return  * 100, 2),
            "cagr":          round(r.cagr           * 100, 2),
            "max_drawdown":  round(r.max_drawdown   * 100, 2),
            "sharpe":        round(r.sharpe,              3),
            "profit_factor": None if pf == math.inf else round(pf, 3),
            "win_rate":      round(r.win_rate        * 100, 2),
            "n_trades":      r.n_trades,
            "avg_win":       round(r.avg_win         * 100, 3),
            "avg_loss":      round(r.avg_loss        * 100, 3),
        }

    return {
        "ok":       True,
        "bars":     len(c15),
        "strategy": _stats(res),
        "buy_hold": _stats(bh),
        "equity":   res.equity[::step],
        "bh_equity": bh.equity[::step],
        "times":    ind.times[::step],
    }


# --------------------------------------------------------------------------- #
# Auto-optimizer:  search condition combinations for the best strategy
# --------------------------------------------------------------------------- #
# The atomic search pool: every condition type, in its BULLISH direction, on
# each timeframe. A candidate entry rule is an AND of some of these atoms; its
# bearish mirror provides the short side (or, when shorts are off, the exit).
_POOL_TYPES_DIRS: List[Tuple[str, str]] = [
    ("tsi_vs_zero",   "above"),
    ("sig_vs_zero",   "above"),
    ("tsi_vs_signal", "above"),
    ("zero_cross",    "up"),
    ("fresh_cross",   "up"),
    ("gap_change",    "expanding"),
    ("tsi_slope",     "rising"),
    ("sig_slope",     "rising"),
]
ATOMIC: List[Tuple[str, str, str]] = [
    (tf, t, d) for tf in ("4h", "1h", "15m") for (t, d) in _POOL_TYPES_DIRS
]

_WARMUP = BARS_PER_4H * 60  # matches run_backtest default

_LABELS = {
    ("tsi_vs_zero",   "above"): "TSI>0",   ("tsi_vs_zero",   "below"): "TSI<0",
    ("sig_vs_zero",   "above"): "SIG>0",   ("sig_vs_zero",   "below"): "SIG<0",
    ("tsi_vs_signal", "above"): "TSI>SIG", ("tsi_vs_signal", "below"): "TSI<SIG",
    ("zero_cross",    "up"):    "TSI↑0",   ("zero_cross",    "down"):  "TSI↓0",
    ("fresh_cross",   "up"):    "↑교차",    ("fresh_cross",   "down"):  "↓교차",
    ("gap_change",    "expanding"): "GAP↗", ("gap_change",  "contracting"): "GAP↘",
    ("tsi_slope",     "rising"): "TSI↗",   ("tsi_slope",     "falling"): "TSI↘",
    ("sig_slope",     "rising"): "SIG↗",   ("sig_slope",     "falling"): "SIG↘",
}


def _cond_label(tf: str, t: str, d: str) -> str:
    return f"{tf} {_LABELS.get((t, d), t + '/' + d)}"


def make_rule(conds, tf_logic: str = "AND", group_logic: str = "AND") -> dict:
    """Build an entry/exit rule dict from a list of (tf, type, dir) atoms."""
    rule: dict = {"tf_logic": tf_logic}
    for tf in ("4h", "1h", "15m"):
        rule[tf] = {
            "logic": group_logic,
            "conditions": [{"type": t, "dir": d} for (ctf, t, d) in conds if ctf == tf],
        }
    return rule


def _and_list(arrs: List[List[bool]], n: int) -> List[bool]:
    if not arrs:
        return [False] * n
    out = list(arrs[0])
    for a in arrs[1:]:
        for i in range(n):
            if out[i] and not a[i]:
                out[i] = False
    return out


def _simulate(long_sig, short_sig, close, warmup, cost, allow_short):
    """Fast next-bar-fill sim for a precomputed long/short signal pair.

    Semantics match build_dynamic_strategy with an empty exit (stop-and-reverse)
    when shorts are allowed, or exit-to-flat on the mirror when they are not —
    so the emitted config reproduces exactly in the general engine / web lab.
    """
    n = len(close)
    decision = [0] * n
    pos = 0
    for i in range(warmup, n):
        if long_sig[i]:
            d = 1
        elif short_sig[i]:
            d = -1 if allow_short else 0
        else:
            d = pos
        pos = d
        decision[i] = d

    equity = [1.0]
    eq = 1.0
    trades: List[Trade] = []
    entry_px = 0.0
    entry_i = 0
    prev = 0
    for i in range(1, n):
        held = decision[i - 1]
        if held != prev:
            fill = close[i - 1]
            if prev != 0:
                gross = prev * (fill / entry_px - 1.0)
                trades.append(Trade(prev, entry_i, i, entry_px, fill, gross - 2 * cost))
            eq *= (1.0 - cost * abs(held - prev))
            if held != 0:
                entry_px = fill
                entry_i = i
        if held != 0:
            eq *= (1.0 + held * (close[i] / close[i - 1] - 1.0))
        equity.append(eq)
        prev = held
    if prev != 0 and entry_px:
        fill = close[-1]
        gross = prev * (fill / entry_px - 1.0)
        trades.append(Trade(prev, entry_i, n - 1, entry_px, fill, gross - 2 * cost))
    return equity, trades


def _sub_metrics(eq_slice: List[float], trades: List[Trade], bar_ms: int = MS_15M) -> dict:
    """Performance metrics over an equity sub-curve + the trades inside it."""
    out = dict(total_return=0.0, cagr=0.0, max_drawdown=0.0, sharpe=0.0,
               profit_factor=None, win_rate=0.0, n_trades=len(trades),
               avg_win=0.0, avg_loss=0.0)
    if len(eq_slice) >= 2 and eq_slice[0] > 0:
        base = eq_slice[0]
        eq = [e / base for e in eq_slice]
        out["total_return"] = eq[-1] - 1.0
        peak = eq[0]
        mdd = 0.0
        for e in eq:
            if e > peak:
                peak = e
            d = e / peak - 1.0
            if d < mdd:
                mdd = d
        out["max_drawdown"] = mdd
        rets = [eq[k] / eq[k - 1] - 1.0 for k in range(1, len(eq))]
        if len(rets) >= 2:
            m = sum(rets) / len(rets)
            var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
            sd = math.sqrt(var)
            if sd > 0:
                bpy = (365.25 * 24 * 3600) / (bar_ms / 1000)
                out["sharpe"] = m / sd * math.sqrt(bpy)
        years = len(eq) * bar_ms / 1000 / (365.25 * 24 * 3600)
        if years > 0 and eq[-1] > 0:
            out["cagr"] = eq[-1] ** (1.0 / years) - 1.0
    if trades:
        wins = [t.ret for t in trades if t.ret > 0]
        losses = [t.ret for t in trades if t.ret < 0]
        out["win_rate"] = len(wins) / len(trades)
        g = sum(wins)
        l = -sum(losses)
        out["profit_factor"] = (math.inf if g > 0 else 0.0) if l == 0 else g / l
        out["avg_win"] = sum(wins) / len(wins) if wins else 0.0
        out["avg_loss"] = sum(losses) / len(losses) if losses else 0.0
    return out


def _score(m: Optional[dict], objective: str, min_trades: int) -> float:
    if not m or m["n_trades"] < min_trades:
        return -1e18
    if objective == "return":
        return m["total_return"]
    if objective == "calmar":
        mdd = abs(m["max_drawdown"])
        return m["cagr"] / mdd if mdd > 1e-9 else (m["cagr"] if m["cagr"] > 0 else 0.0)
    return m["sharpe"]  # default


def optimize(
    ind: Indicators,
    *,
    objective: str = "sharpe",
    max_depth: int = 2,
    greedy_depth: int = 4,
    min_trades: int = 10,
    allow_short: bool = True,
    fee_rate: float = 0.0005,
    slip_rate: float = 0.0001,
    holdout: float = 0.3,
    top: int = 15,
):
    """Search condition combinations and return the best, ranked.

    Strategy: precompute every atomic condition's boolean array once, then test
    all singles + all pairs (and up to ``max_depth``-tuples) exhaustively, plus
    a greedy forward-selection chain for deeper combos. Rank by the in-sample
    ``objective``; if ``holdout`` > 0, the top survivors are re-ranked by their
    out-of-sample objective so the winner is the one that generalises.

    Returns a list of ``(conds, is_metrics, oos_metrics, is_score)`` tuples.
    """
    n = len(ind.close)
    if n <= _WARMUP + 50:
        raise ValueError("not enough data to optimize")
    split = _WARMUP + int((n - _WARMUP) * (1.0 - holdout)) if holdout > 0 else n
    cost = fee_rate + slip_rate

    bull: Dict[Tuple[str, str, str], List[bool]] = {}
    bear: Dict[Tuple[str, str, str], List[bool]] = {}
    for (tf, t, d) in ATOMIC:
        bull[(tf, t, d)] = [_eval_cond(ind, i, t, d, tf) for i in range(n)]
        fd = _flip_dir(t, d)
        bear[(tf, t, d)] = [_eval_cond(ind, i, t, fd, tf) for i in range(n)]

    cache: Dict[frozenset, tuple] = {}

    def evaluate(conds):
        key = frozenset(conds)
        if key in cache:
            return cache[key]
        long_sig = _and_list([bull[c] for c in conds], n)
        short_sig = _and_list([bear[c] for c in conds], n)
        eq, tr = _simulate(long_sig, short_sig, ind.close, _WARMUP, cost, allow_short)
        is_m = _sub_metrics(eq[_WARMUP:split], [t for t in tr if t.entry_i < split])
        oos_m = (_sub_metrics(eq[split:], [t for t in tr if t.entry_i >= split])
                 if holdout > 0 else None)
        cache[key] = (is_m, oos_m, list(conds))
        return cache[key]

    candidates = set()
    for c in ATOMIC:                                   # singles
        evaluate([c])
        candidates.add(frozenset([c]))
    for k in range(2, max_depth + 1):                  # exhaustive small tuples
        for combo in itertools.combinations(ATOMIC, k):
            evaluate(list(combo))
            candidates.add(frozenset(combo))

    # greedy forward selection from the best single
    best_key = max((frozenset([c]) for c in ATOMIC),
                   key=lambda k: _score(cache[k][0], objective, min_trades))
    cur = list(best_key)
    cur_s = _score(cache[best_key][0], objective, min_trades)
    used = set(cur)
    while len(cur) < greedy_depth:
        best_add, best_s = None, cur_s
        for c in ATOMIC:
            if c in used:
                continue
            is_m, _, _ = evaluate(cur + [c])
            s = _score(is_m, objective, min_trades)
            if s > best_s:
                best_s, best_add = s, c
        if best_add is None:
            break
        cur.append(best_add)
        used.add(best_add)
        cur_s = best_s
        candidates.add(frozenset(cur))

    def oos_pass(oos_m: Optional[dict]) -> bool:
        """Did the combo survive out-of-sample? (lenient trade floor for the
        shorter OOS window, plus positive risk-adjusted + total return)."""
        if oos_m is None:
            return True
        floor = max(2, round(min_trades * holdout))
        return (oos_m["n_trades"] >= floor and oos_m["sharpe"] > 0
                and oos_m["total_return"] > 0)

    ranked = []
    for key in candidates:
        is_m, oos_m, conds = cache[key]
        s = _score(is_m, objective, min_trades)
        if s <= -1e17:                       # failed the in-sample trade floor
            continue
        ranked.append((conds, is_m, oos_m, s, oos_pass(oos_m)))

    # OOS-confirmed combos first, then by in-sample objective. With holdout=0
    # every combo "passes", so this is a pure in-sample ranking.
    ranked.sort(key=lambda x: (x[4], x[3]), reverse=True)
    return [(c, im, om, s) for (c, im, om, s, _p) in ranked[:top]]


def _serialise_metrics(m: Optional[dict]) -> Optional[dict]:
    if not m:
        return None
    pf = m["profit_factor"]
    return dict(
        total_return=round(m["total_return"] * 100, 2),
        cagr=round(m["cagr"] * 100, 2),
        max_drawdown=round(m["max_drawdown"] * 100, 2),
        sharpe=round(m["sharpe"], 3),
        profit_factor=None if pf in (None, math.inf) else round(pf, 3),
        win_rate=round(m["win_rate"] * 100, 2),
        n_trades=m["n_trades"],
    )


def run_lab_optimize(c15: List[Candle], config: dict) -> dict:
    """Run the auto-optimizer and return a JSON-serialisable result for the web."""
    fee = config.get("fee_bps", 5.0) / 10000.0
    slip = config.get("slip_bps", 1.0) / 10000.0
    allow_short = bool(config.get("allow_short", True))
    objective = config.get("objective", "sharpe")
    min_trades = int(config.get("min_trades", 10))
    holdout = float(config.get("holdout", 0.3))
    max_depth = max(1, min(3, int(config.get("depth", 2))))
    greedy_depth = int(config.get("greedy_depth", 4))
    top = int(config.get("top", 15))
    tsi_params = (
        int(config.get("tsi_long", 25)),
        int(config.get("tsi_short", 13)),
        int(config.get("tsi_signal", 13)),
    )

    ind = build_indicators(c15, tsi_params)
    ranked = optimize(ind, objective=objective, max_depth=max_depth,
                      greedy_depth=greedy_depth, min_trades=min_trades,
                      allow_short=allow_short, fee_rate=fee, slip_rate=slip,
                      holdout=holdout, top=top)

    bh = buy_hold(ind)
    bh_stats = _serialise_metrics(_sub_metrics(bh.equity[_WARMUP:], []))

    combos = []
    for conds, is_m, oos_m, score in ranked:
        mirror = [(tf, t, _flip_dir(t, d)) for (tf, t, d) in conds]
        entry_rule = make_rule(conds, "AND", "AND")
        exit_rule = (make_rule([], "OR", "OR") if allow_short
                     else make_rule(mirror, "AND", "AND"))
        combos.append(dict(
            desc=" + ".join(_cond_label(*c) for c in conds),
            conds=[list(c) for c in conds],
            entry=entry_rule,
            exit=exit_rule,
            allow_short=allow_short,
            is_stats=_serialise_metrics(is_m),
            oos_stats=_serialise_metrics(oos_m),
            score=round(score, 4),
        ))

    return dict(ok=True, bars=len(c15), holdout=holdout, objective=objective,
                allow_short=allow_short, buy_hold=bh_stats, combos=combos)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def format_table(results: List[Result]) -> str:
    hdr = (f"{'strategy':<18} {'RET':>8} {'CAGR':>7} {'MDD':>7} {'Sharpe':>7} "
           f"{'PF':>6} {'WIN%':>6} {'#tr':>5} {'avgW':>6} {'avgL':>6}")
    lines = [hdr, "-" * len(hdr)]
    for r in results:
        pf = "inf" if r.profit_factor == math.inf else f"{r.profit_factor:.2f}"
        lines.append(
            f"{r.name:<18} {r.total_return*100:>7.1f}% {r.cagr*100:>6.1f}% "
            f"{r.max_drawdown*100:>6.1f}% {r.sharpe:>7.2f} {pf:>6} "
            f"{r.win_rate*100:>5.1f}% {r.n_trades:>5} "
            f"{r.avg_win*100:>5.2f} {r.avg_loss*100:>5.2f}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Synthetic self-test (no network)
# --------------------------------------------------------------------------- #
def _synth_15m(n: int = 8000, seed: int = 42) -> List[Candle]:
    """Random walk with slowly drifting trend regimes — realistic enough that
    no strategy can trivially win, so the self-test really exercises the engine
    (trades fire, fees bite, results land near Buy&Hold)."""
    import random
    random.seed(seed)
    price = 30000.0
    drift = 0.0
    closes: List[float] = []
    for _ in range(n):
        drift += random.gauss(0, 2e-5)
        drift = max(-5e-4, min(5e-4, drift))      # bounded trend regimes
        price *= 1.0 + drift + random.gauss(0, 0.003)
        closes.append(price)
    out: List[Candle] = []
    t0 = 1_700_000_000_000
    prev = closes[0]
    for i, c in enumerate(closes):
        hi = max(prev, c) * (1.0 + abs(random.gauss(0, 0.001)))
        lo = min(prev, c) * (1.0 - abs(random.gauss(0, 0.001)))
        out.append(Candle(t0 + i * MS_15M, prev, hi, lo, c, 1.0))
        prev = c
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="TSI strategy lab — compare entry/exit rules")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--days", type=int, default=180, help="how many days of 15m history")
    ap.add_argument("--start", help="start date YYYY-MM-DD (UTC); overrides --days")
    ap.add_argument("--end", help="end date YYYY-MM-DD (UTC); default now")
    ap.add_argument("--market", choices=["futures", "spot"], default="futures")
    ap.add_argument("--fee-bps", type=float, default=5.0, help="per-side cost in bps (taker≈5)")
    ap.add_argument("--slippage-bps", type=float, default=1.0)
    ap.add_argument("--long-only", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="run offline on synthetic data")
    args = ap.parse_args(argv)

    if args.selftest:
        print("# SELF-TEST on synthetic data (no network)\n")
        c15 = _synth_15m()
    else:
        if args.market == "futures":
            base, path = FUTURES_BASE_URL, FUTURES_KLINES_PATH
        else:
            from tsi_signal.data import SPOT_BASE_URL, SPOT_KLINES_PATH
            base, path = SPOT_BASE_URL, SPOT_KLINES_PATH

        if args.start:
            start_ms = int(datetime.strptime(args.start, "%Y-%m-%d")
                           .replace(tzinfo=timezone.utc).timestamp() * 1000)
            end_ms = (int(datetime.strptime(args.end, "%Y-%m-%d")
                          .replace(tzinfo=timezone.utc).timestamp() * 1000)
                      if args.end else None)
        else:
            end_ms = None
            start_ms = int((datetime.now(timezone.utc)
                            - timedelta(days=args.days)).timestamp() * 1000)

        print(f"# Fetching {args.symbol} 15m klines from {args.market} …")
        try:
            c15 = fetch_klines_range(args.symbol, "15m", start_ms, end_ms,
                                     base_url=base, path=path)
        except Exception as exc:
            print(f"ERROR fetching data: {type(exc).__name__}: {exc}", file=sys.stderr)
            print("This host probably can't reach Binance (geo-block). Run on your "
                  "Oracle VM.", file=sys.stderr)
            return 1
        if len(c15) < 2000:
            print(f"WARNING: only {len(c15)} bars fetched — results will be thin.",
                  file=sys.stderr)

    print(f"# {len(c15)} × 15m bars "
          f"({len(c15)*15/60/24:.0f} days). Fee={args.fee_bps}bps/side, "
          f"slip={args.slippage_bps}bps.\n")

    ind = build_indicators(c15)
    fee = args.fee_bps / 10000.0
    slip = args.slippage_bps / 10000.0

    results = [buy_hold(ind)]
    for name, fn in STRATEGIES.items():
        results.append(run_backtest(ind, fn, name, fee_rate=fee, slip_rate=slip,
                                    allow_short=not args.long_only))

    # sort strategies (not B&H) by Sharpe for the headline ranking
    strat = sorted(results[1:], key=lambda r: r.sharpe, reverse=True)
    ordered = [results[0]] + strat

    print(format_table(ordered))
    print("\nRET=total return, CAGR=annualised, MDD=max drawdown (smaller=better),")
    print("PF=profit factor (gains/losses), WIN%=winning trades, #tr=trade count.")
    print("Pick by Sharpe + shallow MDD + PF>1.3, not RET alone. Compare vs Buy&Hold.")
    if not args.selftest:
        print("\nNext: tell me the winning row and I'll wire that rule into the live "
              "scanner/positions so your phone alerts match the backtested edge.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
