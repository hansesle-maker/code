"""Vectorised backtester for the TSI signal strategy.

It re-uses the live decision rule (:func:`tsi_signal.signals.decide`) bar by
bar, so what you backtest is exactly what the engine trades. Conventions:

- Signals are computed on a CLOSED 4h bar t and the position is held over the
  next bar t -> t+1 (one-bar lag, no lookahead).
- The 1h state used at 4h bar t is the last 1h bar that closed inside it.
- Trading costs are charged on turnover (|change in signed exposure|).
- The relative-strength GATE, which uses a manual start time live, is here
  approximated by a ROLLING relative strength vs BTC over ``rs_lookback`` bars
  (or turned off) so it can run continuously over history.

Position size = sign(direction) x conviction size-fraction, so exposure is in
[-1, +1] (no leverage beyond full size).
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .data import Candle, Fetcher
from .indicators import relative_strength, true_strength_index
from .signals import Direction, SignalParams, decide, tsi_state

_ONE_HOUR_MS = 60 * 60 * 1000


@dataclass
class BacktestParams:
    fee_rate: float = 0.0005  # cost per unit turnover (5 bps ~ futures taker)
    rs_gate: str = "rolling"  # 'rolling' | 'off'
    rs_lookback: int = 30  # 4h bars for the rolling relative-strength gate
    warmup: int = 150  # skip the first N 4h bars (TSI convergence)
    bars_per_year: float = 6 * 365  # 4h bars -> 2190/yr (for annualising)


@dataclass
class BacktestResult:
    symbol: str
    label: str
    n_bars: int
    total_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    buyhold_return: float
    exposure: Optional[float] = None  # mean |exposure| (None for portfolio)
    n_trades: Optional[int] = None
    win_rate: Optional[float] = None
    equity: List[float] = field(default_factory=list)
    times: List[int] = field(default_factory=list)  # entry open_time per period
    net: List[float] = field(default_factory=list)  # per-period net return


# --------------------------------------------------------------------------- #
# per-bar inputs
# --------------------------------------------------------------------------- #
def _state_series(closes: List[float], p: SignalParams) -> List[int]:
    tsi, sig = true_strength_index(closes, p.tsi_long, p.tsi_short, p.tsi_signal)
    return [tsi_state(tsi[i], sig[i]) for i in range(len(closes))]


def _align_1h_state(candles_4h: List[Candle], candles_1h: List[Candle], state1: List[int]) -> List[int]:
    """For each 4h bar, the state of the last 1h bar that closed inside it."""
    times1 = [c.open_time for c in candles_1h]
    out: List[int] = []
    for c in candles_4h:
        target = c.open_time + 3 * _ONE_HOUR_MS  # last 1h open inside the 4h bar
        j = bisect.bisect_right(times1, target) - 1
        out.append(state1[j] if j >= 0 else 0)
    return out


def _gate_series(
    close4: List[float], bench4: List[Optional[float]], bt: BacktestParams
) -> Tuple[List[bool], List[bool]]:
    n = len(close4)
    if bt.rs_gate == "off":
        return [True] * n, [True] * n
    long_ok = [True] * n
    short_ok = [True] * n
    L = bt.rs_lookback
    for t in range(n):
        if t >= L and bench4[t] and bench4[t - L]:
            r = relative_strength(close4[t], close4[t - L], bench4[t], bench4[t - L])
            if r is not None:
                long_ok[t], short_ok[t] = r > 0, r < 0
    return long_ok, short_ok


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
def _equity(net: List[float]) -> List[float]:
    eq = [1.0]
    for x in net:
        eq.append(eq[-1] * (1.0 + x))
    return eq


def _sharpe(net: List[float], bpy: float) -> float:
    n = len(net)
    if n < 2:
        return 0.0
    mean = sum(net) / n
    var = sum((x - mean) ** 2 for x in net) / (n - 1)
    sd = math.sqrt(var)
    return (mean / sd) * math.sqrt(bpy) if sd > 0 else 0.0


def _max_drawdown(equity: List[float]) -> float:
    peak = equity[0] if equity else 1.0
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    return mdd


def _cagr(equity: List[float], n_periods: int, bpy: float) -> float:
    if n_periods <= 0 or equity[-1] <= 0:
        return 0.0
    return equity[-1] ** (bpy / n_periods) - 1.0


def _segments(net: List[float], exps: List[float]) -> List[List[float]]:
    """Per-period returns grouped into trades (maximal runs of one sign)."""
    segs: List[List[float]] = []
    cur_sign = 0
    cur: List[float] = []
    for r, e in zip(net, exps):
        s = (e > 0) - (e < 0)
        if s != cur_sign:
            if cur_sign != 0 and cur:
                segs.append(cur)
            cur, cur_sign = [], s
        if cur_sign != 0:
            cur.append(r)
    if cur_sign != 0 and cur:
        segs.append(cur)
    return segs


def _trade_stats(net: List[float], exps: List[float]) -> Tuple[int, float]:
    segs = _segments(net, exps)
    if not segs:
        return 0, 0.0
    wins = 0
    for seg in segs:
        ret = 1.0
        for x in seg:
            ret *= 1.0 + x
        if ret - 1.0 > 0:
            wins += 1
    return len(segs), wins / len(segs)


# --------------------------------------------------------------------------- #
# backtest
# --------------------------------------------------------------------------- #
def backtest_symbol(
    symbol: str,
    candles_4h: List[Candle],
    candles_1h: List[Candle],
    bench_by_time: Dict[int, float],
    params: SignalParams,
    bt: BacktestParams,
    label: str = "strategy",
) -> BacktestResult:
    close4 = [c.close for c in candles_4h]
    bench4 = [bench_by_time.get(c.open_time) for c in candles_4h]
    state4 = _state_series(close4, params)
    state1 = _align_1h_state(candles_4h, candles_1h, _state_series([c.close for c in candles_1h], params))
    long_ok, short_ok = _gate_series(close4, bench4, bt)

    start = max(bt.warmup, bt.rs_lookback if bt.rs_gate == "rolling" else 0)
    net: List[float] = []
    exps: List[float] = []
    bh: List[float] = []
    times: List[int] = []
    prev_exp = 0.0
    for t in range(start, len(close4) - 1):
        direction, size, _ = decide(state4[t], state1[t], long_ok[t], short_ok[t], params)
        sgn = 1.0 if direction is Direction.LONG else -1.0 if direction is Direction.SHORT else 0.0
        exp = sgn * size
        r = close4[t + 1] / close4[t] - 1.0
        net.append(exp * r - bt.fee_rate * abs(exp - prev_exp))
        exps.append(exp)
        bh.append(r)
        times.append(candles_4h[t].open_time)
        prev_exp = exp

    eq = _equity(net)
    n_trades, win_rate = _trade_stats(net, exps)
    return BacktestResult(
        symbol=symbol, label=label, n_bars=len(net),
        total_return=eq[-1] - 1.0,
        cagr=_cagr(eq, len(net), bt.bars_per_year),
        sharpe=_sharpe(net, bt.bars_per_year),
        max_drawdown=_max_drawdown(eq),
        buyhold_return=_equity(bh)[-1] - 1.0,
        exposure=(sum(abs(e) for e in exps) / len(exps)) if exps else 0.0,
        n_trades=n_trades, win_rate=win_rate, equity=eq, times=times, net=net,
    )


def combine_portfolio(results: List[BacktestResult], bt: BacktestParams,
                      label: str = "strategy") -> BacktestResult:
    """Equal-weight portfolio: average per-bar net return across symbols that
    have data at each timestamp."""
    acc: Dict[int, List[float]] = {}
    for res in results:
        for t, x in zip(res.times, res.net):
            acc.setdefault(t, []).append(x)
    times = sorted(acc)
    net = [sum(acc[t]) / len(acc[t]) for t in times]
    eq = _equity(net)
    return BacktestResult(
        symbol="PORTFOLIO", label=label, n_bars=len(net),
        total_return=eq[-1] - 1.0,
        cagr=_cagr(eq, len(net), bt.bars_per_year),
        sharpe=_sharpe(net, bt.bars_per_year),
        max_drawdown=_max_drawdown(eq),
        buyhold_return=float("nan"),
        equity=eq, times=times, net=net,
    )


def run_backtest(
    symbols: List[str],
    fetch: Fetcher,
    params: SignalParams,
    bt: BacktestParams,
    benchmark: str = "BTCUSDT",
    kline_limit: int = 1500,
    label: str = "strategy",
) -> Tuple[List[BacktestResult], BacktestResult]:
    """Backtest each symbol and return (per-symbol results, portfolio)."""
    bench_4h = fetch(benchmark, "4h", kline_limit)
    bench_by_time = {c.open_time: c.close for c in bench_4h}

    results: List[BacktestResult] = []
    for sym in symbols:
        c4 = bench_4h if sym == benchmark else fetch(sym, "4h", kline_limit)
        c1 = fetch(sym, "1h", kline_limit)
        results.append(backtest_symbol(sym, c4, c1, bench_by_time, params, bt, label=label))
    portfolio = combine_portfolio(results, bt, label=label)
    return results, portfolio


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
def _pct(x: float) -> str:
    return "nan" if x != x else f"{x * 100:+.1f}"


def format_results(rows: List[BacktestResult], title: str = "") -> str:
    header = ["LABEL", "SYMBOL", "BARS", "RET%", "CAGR%", "SHARPE", "MDD%",
              "B&H%", "EXP%", "TRADES", "WIN%"]
    lines = [header]
    for r in rows:
        lines.append([
            r.label, r.symbol, str(r.n_bars), _pct(r.total_return), _pct(r.cagr),
            f"{r.sharpe:.2f}", _pct(r.max_drawdown), _pct(r.buyhold_return),
            "-" if r.exposure is None else f"{r.exposure * 100:.0f}",
            "-" if r.n_trades is None else str(r.n_trades),
            "-" if r.win_rate is None else f"{r.win_rate * 100:.0f}",
        ])
    widths = [max(len(row[i]) for row in lines) for i in range(len(header))]
    body = "\n".join("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)) for row in lines)
    return (f"{title}\n{body}" if title else body)
