#!/usr/bin/env python3
"""Open-Close Cross strategy — NON-REPAINTING backtest.

A faithful, look-ahead-free port of JustUncleL's "Open Close Cross Strategy
R5.1" (TradingView, @JayRogers / @JustUncleL). The idea:

    closeMA = MA(close, len)        # on an "alternate resolution" = chart TF × N
    openMA  = MA(open,  len)
    LONG  when closeMA crosses ABOVE openMA
    SHORT when closeMA crosses BELOW openMA

Why the original repaints, and how this fixes it
-------------------------------------------------
The Pine version reads the higher timeframe with
``security(..., lookahead=barmerge.lookahead_on)``, which pulls the higher-TF
bar's FINAL value onto lower-TF bars that close *before* that higher-TF bar is
actually done — so signals shift after the fact (repaint).

Here the higher timeframe is built by resampling the base candles and the MA is
forward-filled onto the base grid using **only CLOSED higher-TF bars** (a bar
[open, open+span) is usable on a base bar only once base_bar_close >= open+span).
Every decision uses closed data up to bar i and is filled at bar i+1's open, so
there is no look-ahead and nothing repaints.

Usage (run where Binance is reachable, e.g. your Oracle VM):

    # default SMMA(8), alt-resolution = 15m × 3, both sides:
    python3 occ_strategy.py --symbol BTCUSDT --interval 15m --mult 3 --days 180

    # rank every MA type on the same data:
    python3 occ_strategy.py --symbol BTCUSDT --interval 15m --mult 3 --compare-ma

    # offline self-test (synthetic data, no network):
    python3 occ_strategy.py --selftest
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

from tsi_signal.data import Candle, fetch_klines_range
from tsi_signal.indicators import ema
from strategy_lab import Result, Trade, format_table

# Base interval -> milliseconds (Binance-supported intraday + daily).
INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}

MA_TYPES = ["SMA", "EMA", "DEMA", "TEMA", "WMA", "VWMA", "SMMA",
            "HullMA", "LSMA", "ALMA", "SSMA", "TMA"]


# --------------------------------------------------------------------------- #
# Moving averages (pure Python, full-length output; warm-up region is skipped
# by the backtest so partial early windows don't matter)
# --------------------------------------------------------------------------- #
def _sma(src: Sequence[float], n: int) -> List[float]:
    out, s = [], 0.0
    for i, v in enumerate(src):
        s += v
        if i >= n:
            s -= src[i - n]
        out.append(s / min(i + 1, n))
    return out


def _wma(src: Sequence[float], n: int) -> List[float]:
    out = []
    for i in range(len(src)):
        k = min(n, i + 1)
        num = den = 0.0
        for j in range(k):                       # weight 1..k, newest heaviest
            w = k - j
            num += src[i - j] * w
            den += w
        out.append(num / den)
    return out


def _vwma(src: Sequence[float], vol: Sequence[float], n: int) -> List[float]:
    out = []
    for i in range(len(src)):
        k = min(n, i + 1)
        num = den = 0.0
        for j in range(k):
            num += src[i - j] * vol[i - j]
            den += vol[i - j]
        out.append(num / den if den else src[i])
    return out


def _smma(src: Sequence[float], n: int) -> List[float]:
    """Wilder's smoothed MA (a.k.a. RMA): prev*(n-1)/n + src/n, SMA-seeded."""
    out: List[float] = []
    prev: Optional[float] = None
    run = 0.0
    for i, v in enumerate(src):
        if i < n:
            run += v
            prev = run / (i + 1)
        else:
            prev = (prev * (n - 1) + v) / n
        out.append(prev)
    return out


def _dema(src: Sequence[float], n: int) -> List[float]:
    e1 = ema(src, n)
    e2 = ema(e1, n)
    return [2 * a - b for a, b in zip(e1, e2)]


def _tema(src: Sequence[float], n: int) -> List[float]:
    e1 = ema(src, n)
    e2 = ema(e1, n)
    e3 = ema(e2, n)
    return [3 * (a - b) + c for a, b, c in zip(e1, e2, e3)]


def _hull(src: Sequence[float], n: int) -> List[float]:
    half = max(1, n // 2)
    sq = max(1, int(round(math.sqrt(n))))
    w_half = _wma(src, half)
    w_full = _wma(src, n)
    raw = [2 * h - f for h, f in zip(w_half, w_full)]
    return _wma(raw, sq)


def _lsma(src: Sequence[float], n: int, offset: int) -> List[float]:
    """Least-squares (linear-regression) MA; value of the fit line `offset`
    bars back, matching TradingView's linreg(src, len, offset)."""
    out = []
    for i in range(len(src)):
        k = min(n, i + 1)
        xs = range(k)                            # x = 0 (oldest) .. k-1 (newest)
        ys = [src[i - (k - 1) + x] for x in xs]
        sx = sum(xs); sy = sum(ys)
        sxx = sum(x * x for x in xs)
        sxy = sum(x * ys[x] for x in xs)
        denom = k * sxx - sx * sx
        if denom == 0:
            out.append(ys[-1])
            continue
        slope = (k * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / k
        out.append(intercept + slope * (k - 1 - offset))
    return out


def _alma(src: Sequence[float], n: int, offset: float, sigma: float) -> List[float]:
    out = []
    m = offset * (n - 1)
    s = n / sigma if sigma else 1.0
    weights = [math.exp(-((j - m) ** 2) / (2 * s * s)) for j in range(n)]
    wsum = sum(weights)
    for i in range(len(src)):
        if i < n - 1:
            out.append(src[i])                   # not enough history yet
            continue
        acc = 0.0
        for j in range(n):                       # j=0 oldest .. n-1 newest
            acc += src[i - (n - 1) + j] * weights[j]
        out.append(acc / wsum)
    return out


def _ssma(src: Sequence[float], n: int) -> List[float]:
    """Ehlers SuperSmoother (2-pole)."""
    a1 = math.exp(-1.414 * math.pi / n)
    b1 = 2 * a1 * math.cos(1.414 * math.pi / n)
    c2, c3 = b1, -a1 * a1
    c1 = 1 - c2 - c3
    out: List[float] = []
    for i, v in enumerate(src):
        prev1 = out[i - 1] if i >= 1 else v
        prev2 = out[i - 2] if i >= 2 else v
        s_prev = src[i - 1] if i >= 1 else v
        out.append(c1 * (v + s_prev) / 2 + c2 * prev1 + c3 * prev2)
    return out


def _tma(src: Sequence[float], n: int) -> List[float]:
    return _sma(_sma(src, n), n)


def moving_average(ma_type: str, src: Sequence[float], vol: Sequence[float],
                   n: int, offset_sigma: float, offset_alma: float) -> List[float]:
    t = ma_type
    if t == "EMA":    return ema(src, n)
    if t == "DEMA":   return _dema(src, n)
    if t == "TEMA":   return _tema(src, n)
    if t == "WMA":    return _wma(src, n)
    if t == "VWMA":   return _vwma(src, vol, n)
    if t == "SMMA":   return _smma(src, n)
    if t == "HullMA": return _hull(src, n)
    if t == "LSMA":   return _lsma(src, n, int(offset_sigma))
    if t == "ALMA":   return _alma(src, n, offset_alma, offset_sigma)
    if t == "SSMA":   return _ssma(src, n)
    if t == "TMA":    return _tma(src, n)
    return _sma(src, n)                            # SMA / fallback


# --------------------------------------------------------------------------- #
# Resampling + non-repainting forward-fill
# --------------------------------------------------------------------------- #
def _resample(c: List[Candle], factor: int) -> List[Candle]:
    """Aggregate base candles into `factor`-sized buckets; emit COMPLETE buckets
    only, so the still-forming higher-TF bar is never produced."""
    if factor <= 1 or not c:
        return list(c)
    step = (c[1].open_time - c[0].open_time) * factor
    out: List[Candle] = []
    bucket: List[Candle] = []
    cur = c[0].open_time // step
    for cd in c:
        key = cd.open_time // step
        if key != cur:
            if len(bucket) == factor:
                out.append(_merge(bucket))
            bucket = []
            cur = key
        bucket.append(cd)
    if len(bucket) == factor:
        out.append(_merge(bucket))
    return out


def _merge(b: List[Candle]) -> Candle:
    return Candle(b[0].open_time, b[0].open, max(c.high for c in b),
                  min(c.low for c in b), b[-1].close, sum(c.volume for c in b))


def _ffill(base_times: List[int], htf: List[Candle], series: List[float],
           base_step: int) -> List[float]:
    """Map a higher-TF series onto the base grid using only CLOSED htf bars.

    An htf bar [open, open+span) is only usable on a base bar once that base
    bar has closed at/after open+span — i.e. no peeking into a forming htf bar.
    """
    if not htf:
        return [0.0] * len(base_times)
    span = htf[1].open_time - htf[0].open_time if len(htf) > 1 else base_step
    close_times = [c.open_time + span for c in htf]
    out, j, n = [], -1, len(htf)
    for t in base_times:
        bar_close = t + base_step
        while j + 1 < n and close_times[j + 1] <= bar_close:
            j += 1
        out.append(series[j] if j >= 0 else series[0])
    return out


@dataclass
class OCCParams:
    ma_type: str = "SMMA"
    ma_len: int = 8
    mult: int = 3
    use_res: bool = True
    trade_type: str = "BOTH"          # LONG | SHORT | BOTH | NONE
    sl_pct: float = 0.0               # stop-loss %, 0 = off
    tp_pct: float = 0.0               # take-profit %, 0 = off
    offset_sigma: float = 6.0         # LSMA offset / ALMA sigma
    offset_alma: float = 0.85         # ALMA offset
    mode: str = "nonrepaint"          # nonrepaint | realtime | lookahead
    entry_filter: bool = False        # skip entries worse than the signal's first-paint price
    filter_tol_bps: float = 0.0       # allow this much adverse slack (bps) before skipping


def build_series(c_base: List[Candle], p: OCCParams) -> Tuple[List[int], List[float], List[float]]:
    """Return (base_times, closeMA_alt, openMA_alt) — both forward-filled onto
    the base grid from CLOSED alternate-resolution bars (NON-REPAINTING)."""
    base_times = [c.open_time for c in c_base]
    base_step = (c_base[1].open_time - c_base[0].open_time) if len(c_base) > 1 else 1
    factor = p.mult if p.use_res else 1

    alt = _resample(c_base, factor)
    close_ma = moving_average(p.ma_type, [c.close for c in alt], [c.volume for c in alt],
                              p.ma_len, p.offset_sigma, p.offset_alma)
    open_ma = moving_average(p.ma_type, [c.open for c in alt], [c.volume for c in alt],
                             p.ma_len, p.offset_sigma, p.offset_alma)
    close_alt = _ffill(base_times, alt, close_ma, base_step)
    open_alt = _ffill(base_times, alt, open_ma, base_step)
    return base_times, close_alt, open_alt


def realtime_alt_series(c_base: List[Candle], p: OCCParams) -> Tuple[List[float], List[float]]:
    """REPAINT-AWARE but look-ahead-free. At every base bar we recompute the
    alternate-resolution MA using the CURRENTLY-FORMING higher-TF bar, exactly
    as a live trader watching the repainting indicator would see it:

      - the forming alt bar's close = the latest base close (updates each bar),
      - its open = the alt bucket's first base open (fixed for the bucket).

    So the close-MA wiggles within a bucket and can cross the open-MA back and
    forth (the "repaint"); each such cross is a real, tradeable event because it
    only uses data up to the current bar. This captures "entered on the
    intermediate paint, adjusted when it repainted" — without any peeking.
    """
    n = len(c_base)
    if n == 0:
        return [], []
    base_step = (c_base[1].open_time - c_base[0].open_time) if n > 1 else 1
    factor = p.mult if p.use_res else 1
    alt_step = base_step * factor
    N = p.ma_len
    t = p.ma_type
    recursive = t in ("EMA", "DEMA", "TEMA", "SMMA", "SSMA")
    composite = t in ("TMA", "HullMA")
    W = 320 if recursive else (2 * N + 8 if composite else N + 2)  # tail; exact to float precision

    done_close: List[float] = []   # closes of COMPLETED alt buckets
    done_open: List[float] = []
    done_vol: List[float] = []
    close_rt = [0.0] * n
    open_rt = [0.0] * n
    cur_bucket: Optional[int] = None
    bucket_open = 0.0
    bucket_vol = 0.0

    def ma_last(hist: List[float], vols: List[float], forming: float, fvol: float) -> float:
        src = hist[-W:] + [forming]
        vol = vols[-W:] + [fvol]
        return moving_average(t, src, vol, N, p.offset_sigma, p.offset_alma)[-1]

    for i, c in enumerate(c_base):
        b = c.open_time // alt_step
        if b != cur_bucket:
            if cur_bucket is not None:                 # previous bucket closed at i-1
                done_close.append(c_base[i - 1].close)
                done_open.append(bucket_open)
                done_vol.append(bucket_vol)
            cur_bucket = b
            bucket_open = c.open
            bucket_vol = 0.0
        bucket_vol += c.volume
        close_rt[i] = ma_last(done_close, done_vol, c.close, bucket_vol)
        open_rt[i] = ma_last(done_open, done_vol, bucket_open, bucket_vol)
    return close_rt, open_rt


def lookahead_alt_series(c_base: List[Candle], p: OCCParams) -> Tuple[List[float], List[float]]:
    """TradingView-style REPAINTING (look-ahead ON): every base bar uses its own
    (current) alt bucket's FINAL MA value — which is only known once that bucket
    closes in the future. This is NOT achievable live; provided only so you can
    measure how much of the apparent edge is look-ahead fiction."""
    n = len(c_base)
    if n == 0:
        return [], []
    base_step = (c_base[1].open_time - c_base[0].open_time) if n > 1 else 1
    factor = p.mult if p.use_res else 1
    alt_step = base_step * factor

    alt = _resample(c_base, factor)
    close_ma = moving_average(p.ma_type, [c.close for c in alt], [c.volume for c in alt],
                              p.ma_len, p.offset_sigma, p.offset_alma)
    open_ma = moving_average(p.ma_type, [c.open for c in alt], [c.volume for c in alt],
                             p.ma_len, p.offset_sigma, p.offset_alma)
    idx = {ac.open_time // alt_step: k for k, ac in enumerate(alt)}

    cma = [0.0] * n
    oma = [0.0] * n
    last = -1
    for i, c in enumerate(c_base):
        k = idx.get(c.open_time // alt_step, last)   # forming trailing bucket -> carry
        if k is not None and k >= 0:
            cma[i], oma[i], last = close_ma[k], open_ma[k], k
        else:
            cma[i], oma[i] = c.close, c.open
    return cma, oma


def _occ_ma_arrays(c_base: List[Candle], p: OCCParams) -> Tuple[List[float], List[float]]:
    if p.mode == "realtime":
        return realtime_alt_series(c_base, p)
    if p.mode == "lookahead":
        return lookahead_alt_series(c_base, p)
    _, cma, oma = build_series(c_base, p)            # nonrepaint (default)
    return cma, oma


# --------------------------------------------------------------------------- #
# Backtest (next-bar fill, intrabar SL/TP, fees per side)
# --------------------------------------------------------------------------- #
def backtest_occ(c_base: List[Candle], p: OCCParams, fee_rate: float = 0.0005,
                 slip_rate: float = 0.0001, warmup: Optional[int] = None,
                 name: str = "OCC") -> Result:
    n = len(c_base)
    op = [c.open for c in c_base]
    hi = [c.high for c in c_base]
    lo = [c.low for c in c_base]
    cl = [c.close for c in c_base]
    bar_ms = (c_base[1].open_time - c_base[0].open_time) if n > 1 else INTERVAL_MS["15m"]

    _, close_ma, open_ma = (None, *_occ_ma_arrays(c_base, p))
    cost = fee_rate + slip_rate
    if warmup is None:
        warmup = p.ma_len * max(1, p.mult) + 50

    long_ok = p.trade_type in ("LONG", "BOTH")
    short_ok = p.trade_type in ("SHORT", "BOTH")

    # Favorable-entry filter (repaint-aware): within each forming alt bucket,
    # remember the price at which each direction FIRST painted (the "더 유리한
    # 과거 봉" the repaint anchors to). An entry is only taken if the current
    # fill is equal-or-favorable vs that price; otherwise the signal can only
    # CLOSE an opposite position, never open a new one. Look-ahead-free: the
    # reference is always an earlier bar in the same bucket.
    alt_step = bar_ms * (p.mult if p.use_res else 1)
    tol = p.filter_tol_bps / 10000.0
    sig_bucket = None
    ref_long: Optional[float] = None
    ref_short: Optional[float] = None
    skipped = 0

    equity = [1.0]
    eq = 1.0
    trades: List[Trade] = []
    pos = 0
    entry_px = 0.0
    entry_i = 0

    for i in range(1, n):
        # Signal from CLOSED data on bar i-1; order fills at this bar's open ≈ cl[i-1].
        lc = (i - 1 >= 1 and close_ma[i - 2] <= open_ma[i - 2] and close_ma[i - 1] > open_ma[i - 1])
        sc = (i - 1 >= 1 and close_ma[i - 2] >= open_ma[i - 2] and close_ma[i - 1] < open_ma[i - 1])
        fill = cl[i - 1]

        if p.entry_filter:                       # track per-bucket first-paint price
            b = c_base[i - 1].open_time // alt_step
            if b != sig_bucket:
                sig_bucket, ref_long, ref_short = b, None, None
            if lc and ref_long is None:
                ref_long = fill
            if sc and ref_short is None:
                ref_short = fill

        if i - 1 >= warmup and p.trade_type != "NONE":
            new_pos = pos
            if p.trade_type == "BOTH":
                if lc and long_ok:   new_pos = 1
                elif sc and short_ok: new_pos = -1
            elif p.trade_type == "LONG":
                if lc:   new_pos = 1
                elif sc: new_pos = 0
            elif p.trade_type == "SHORT":
                if sc:   new_pos = -1
                elif lc: new_pos = 0

            if p.entry_filter and new_pos != pos and new_pos != 0:
                if new_pos == 1:
                    favorable = ref_long is not None and fill <= ref_long * (1 + tol)
                else:
                    favorable = ref_short is not None and fill >= ref_short * (1 - tol)
                if not favorable:
                    new_pos = 0          # close any opposite position; do NOT open
                    skipped += 1

            if new_pos != pos:
                if pos != 0:
                    gross = pos * (fill / entry_px - 1.0)
                    trades.append(Trade(pos, entry_i, i, entry_px, fill, gross - 2 * cost))
                eq *= (1.0 - cost * abs(new_pos - pos))
                if new_pos != 0:
                    entry_px = fill
                    entry_i = i
                pos = new_pos

        # Hold `pos` through bar i with intrabar SL/TP (SL checked first).
        if pos != 0:
            exit_px = None
            if pos == 1:
                sl = entry_px * (1 - p.sl_pct / 100) if p.sl_pct > 0 else None
                tp = entry_px * (1 + p.tp_pct / 100) if p.tp_pct > 0 else None
                if sl is not None and lo[i] <= sl:   exit_px = sl
                elif tp is not None and hi[i] >= tp: exit_px = tp
            else:
                sl = entry_px * (1 + p.sl_pct / 100) if p.sl_pct > 0 else None
                tp = entry_px * (1 - p.tp_pct / 100) if p.tp_pct > 0 else None
                if sl is not None and hi[i] >= sl:   exit_px = sl
                elif tp is not None and lo[i] <= tp: exit_px = tp

            if exit_px is not None:
                eq *= (1.0 + pos * (exit_px / cl[i - 1] - 1.0))
                eq *= (1.0 - cost)
                gross = pos * (exit_px / entry_px - 1.0)
                trades.append(Trade(pos, entry_i, i, entry_px, exit_px, gross - 2 * cost))
                pos = 0
            else:
                eq *= (1.0 + pos * (cl[i] / cl[i - 1] - 1.0))
        equity.append(eq)

    if pos != 0 and entry_px:
        gross = pos * (cl[-1] / entry_px - 1.0)
        trades.append(Trade(pos, entry_i, n - 1, entry_px, cl[-1], gross - 2 * cost))

    res = Result(name=name, trades=trades, equity=equity, bars=n, bar_ms=bar_ms)
    res.skipped = skipped     # entries suppressed by the favorable-entry filter
    return res


def buy_hold(c_base: List[Candle]) -> Result:
    cl = [c.close for c in c_base]
    bar_ms = (c_base[1].open_time - c_base[0].open_time) if len(c_base) > 1 else INTERVAL_MS["15m"]
    eq = [1.0]
    for i in range(1, len(cl)):
        eq.append(eq[-1] * (cl[i] / cl[i - 1]))
    return Result("Buy&Hold", [], eq, len(cl), bar_ms)


# --------------------------------------------------------------------------- #
# JSON wrapper for the web UI
# --------------------------------------------------------------------------- #
def run_occ_web(c_base: List[Candle], config: dict) -> dict:
    p = OCCParams(
        ma_type=config.get("ma_type", "SMMA"),
        ma_len=int(config.get("ma_len", 8)),
        mult=max(1, int(config.get("mult", 3))),
        use_res=bool(config.get("use_res", True)),
        trade_type=config.get("trade_type", "BOTH"),
        sl_pct=float(config.get("sl_pct", 0.0)),
        tp_pct=float(config.get("tp_pct", 0.0)),
        offset_sigma=float(config.get("offset_sigma", 6.0)),
        offset_alma=float(config.get("offset_alma", 0.85)),
        mode=config.get("mode", "nonrepaint"),
        entry_filter=bool(config.get("entry_filter", False)),
        filter_tol_bps=float(config.get("filter_tol_bps", 0.0)),
    )
    fee = config.get("fee_bps", 5.0) / 10000.0
    slip = config.get("slip_bps", 1.0) / 10000.0

    res = backtest_occ(c_base, p, fee, slip, name=p.ma_type)
    bh = buy_hold(c_base)
    n = len(res.equity)
    step = max(1, n // 2000)

    def stats(r: Result) -> dict:
        pf = r.profit_factor
        return dict(
            total_return=round(r.total_return * 100, 2),
            cagr=round(r.cagr * 100, 2),
            max_drawdown=round(r.max_drawdown * 100, 2),
            sharpe=round(r.sharpe, 3),
            profit_factor=None if pf == math.inf else round(pf, 3),
            win_rate=round(r.win_rate * 100, 2),
            n_trades=r.n_trades,
            avg_win=round(r.avg_win * 100, 3),
            avg_loss=round(r.avg_loss * 100, 3),
        )

    return dict(ok=True, bars=len(c_base), ma_type=p.ma_type, mode=p.mode,
                entry_filter=p.entry_filter, skipped=getattr(res, "skipped", 0),
                strategy=stats(res), buy_hold=stats(bh),
                equity=res.equity[::step], bh_equity=bh.equity[::step],
                times=[c.open_time for c in c_base][::step])


# --------------------------------------------------------------------------- #
# Synthetic data for the offline self-test
# --------------------------------------------------------------------------- #
def _synth(n: int = 8000, seed: int = 7, step_ms: int = INTERVAL_MS["15m"]) -> List[Candle]:
    import random
    random.seed(seed)
    price, drift = 30000.0, 0.0
    closes = []
    for _ in range(n):
        drift += random.gauss(0, 2e-5)
        drift = max(-5e-4, min(5e-4, drift))
        price *= 1.0 + drift + random.gauss(0, 0.003)
        closes.append(price)
    out, t0, prev = [], 1_699_920_000_000, closes[0]   # 1d/4h/15m-aligned epoch
    for i, c in enumerate(closes):
        hi = max(prev, c) * (1 + abs(random.gauss(0, 0.001)))
        lo = min(prev, c) * (1 - abs(random.gauss(0, 0.001)))
        out.append(Candle(t0 + i * step_ms, prev, hi, lo, c, 1.0))
        prev = c
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Open-Close Cross strategy (non-repainting) backtest")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="15m", choices=list(INTERVAL_MS), help="base chart timeframe")
    ap.add_argument("--mult", type=int, default=3, help="alternate-resolution multiplier (×base)")
    ap.add_argument("--alt", choices=list(INTERVAL_MS),
                    help="strategy/alt resolution; with --refresh, derives --interval/--mult")
    ap.add_argument("--refresh", choices=list(INTERVAL_MS),
                    help="realtime sampling period (base TF); pair with --alt")
    ap.add_argument("--no-res", action="store_true", help="disable the alternate resolution")
    ap.add_argument("--ma-type", default="SMMA", choices=MA_TYPES)
    ap.add_argument("--ma-len", type=int, default=8)
    ap.add_argument("--trade-type", default="BOTH", choices=["LONG", "SHORT", "BOTH"])
    ap.add_argument("--sl-pct", type=float, default=0.0, help="stop-loss %% (0=off)")
    ap.add_argument("--tp-pct", type=float, default=0.0, help="take-profit %% (0=off)")
    ap.add_argument("--offset-sigma", type=float, default=6.0)
    ap.add_argument("--offset-alma", type=float, default=0.85)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--start", help="YYYY-MM-DD (UTC); overrides --days")
    ap.add_argument("--end", help="YYYY-MM-DD (UTC)")
    ap.add_argument("--market", choices=["futures", "spot"], default="futures")
    ap.add_argument("--fee-bps", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=float, default=1.0)
    ap.add_argument("--mode", default="nonrepaint", choices=["nonrepaint", "realtime", "lookahead"],
                    help="nonrepaint=closed alt bars only; realtime=forming alt bar each base bar "
                         "(repaint-aware, look-ahead-free); lookahead=TV-style repaint (inflated)")
    ap.add_argument("--entry-filter", action="store_true",
                    help="skip entries worse than the signal's first-paint price (repaint guard); "
                         "such signals only CLOSE an opposite position, never open a new one")
    ap.add_argument("--filter-tol-bps", type=float, default=0.0,
                    help="adverse slack allowed before an entry is skipped (bps)")
    ap.add_argument("--compare-modes", action="store_true",
                    help="run all three modes on the same MA and compare")
    ap.add_argument("--compare-ma", action="store_true", help="rank every MA type on the same data")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    # Strategy-TF + refresh-period mode: derive base interval and multiplier.
    if args.alt and args.refresh:
        alt_ms, ref_ms = INTERVAL_MS[args.alt], INTERVAL_MS[args.refresh]
        if alt_ms < ref_ms or alt_ms % ref_ms != 0:
            ap.error(f"--alt {args.alt} must be a whole multiple of --refresh {args.refresh}")
        args.interval, args.mult = args.refresh, alt_ms // ref_ms

    if args.selftest:
        print("# SELF-TEST on synthetic data (no network)\n")
        c = _synth(step_ms=INTERVAL_MS[args.interval])
    else:
        from tsi_signal.data import (FUTURES_BASE_URL, FUTURES_KLINES_PATH,
                                      SPOT_BASE_URL, SPOT_KLINES_PATH)
        base, path = ((FUTURES_BASE_URL, FUTURES_KLINES_PATH) if args.market == "futures"
                      else (SPOT_BASE_URL, SPOT_KLINES_PATH))
        if args.start:
            s = int(datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
            e = (int(datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
                 if args.end else None)
        else:
            e = None
            s = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp() * 1000)
        print(f"# Fetching {args.symbol} {args.interval} from {args.market} …")
        try:
            c = fetch_klines_range(args.symbol, args.interval, s, e, base_url=base, path=path)
        except Exception as exc:
            print(f"ERROR fetching data: {type(exc).__name__}: {exc}", file=sys.stderr)
            print("This host probably can't reach Binance (geo-block). Run on your Oracle VM.", file=sys.stderr)
            return 1

    if len(c) < 300:
        print(f"WARNING: only {len(c)} bars.", file=sys.stderr)

    alt = "off" if args.no_res else f"{args.interval}×{args.mult}"
    print(f"# {len(c)} × {args.interval} bars ({len(c)*INTERVAL_MS[args.interval]/86400000:.0f} days). "
          f"alt-res={alt}, fee={args.fee_bps}bps/side.\n")

    fee, slip = args.fee_bps / 10000.0, args.slippage_bps / 10000.0
    results = [buy_hold(c)]

    def mkp(ma_type, mode):
        return OCCParams(ma_type=ma_type, ma_len=args.ma_len, mult=args.mult, use_res=not args.no_res,
                         trade_type=args.trade_type, sl_pct=args.sl_pct, tp_pct=args.tp_pct,
                         offset_sigma=args.offset_sigma, offset_alma=args.offset_alma, mode=mode,
                         entry_filter=args.entry_filter, filter_tol_bps=args.filter_tol_bps)

    if args.compare_modes:
        for mode in ("lookahead", "realtime", "nonrepaint"):
            results.append(backtest_occ(c, mkp(args.ma_type, mode), fee, slip, name=f"{args.ma_type}/{mode}"))
        tail = ("\nlookahead = TV-style repaint (uses each alt bar's FINAL value early = peeks "
                "into the future → inflated, NOT achievable live).\nrealtime  = forming alt bar "
                "re-evaluated every base bar (repaint-aware but look-ahead-free → achievable).\n"
                "nonrepaint= closed alt bars only (laggy, conservative). Trust realtime as your "
                "honest live expectation; the lookahead↔realtime gap is the repaint fiction.")
    elif args.compare_ma:
        rows = [backtest_occ(c, mkp(mt, args.mode), fee, slip, name=mt) for mt in MA_TYPES]
        rows.sort(key=lambda r: r.sharpe, reverse=True)
        results += rows
        tail = (f"\nMode={args.mode}. RET=total return, MDD=max drawdown, PF=profit factor. "
                "(--compare-modes shows the repaint spread for one MA.)")
    else:
        results.append(backtest_occ(c, mkp(args.ma_type, args.mode), fee, slip,
                                    name=f"{args.ma_type}{args.ma_len}/{args.mode}"))
        tail = (f"\nMode={args.mode}. realtime = repaint-aware but look-ahead-free (re-evaluated "
                "each base bar). Compare vs Buy&Hold; pick by Sharpe + shallow MDD.")

    print(format_table(results))
    print(tail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
