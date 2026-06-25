#!/usr/bin/env python3
"""
Cardwell RSI Trade Navigator — Backtester
Replicates the Pine Script indicator logic for 15m, 1h, and 4h timeframes.

Usage
-----
    python cardwell_backtest.py BTC-USD
    python cardwell_backtest.py AAPL --ma-type LLAMA --use-chop
    python cardwell_backtest.py BTC-USD --use-htf --trail-stop --no-plot

Trade simulation (matching Pine Script behaviour)
-------------------------------------------------
  • Entry at bar close on signal bar (crossover detected 1 bar prior)
  • 1/3 position exits at each of TP1 / TP2 / TP3
  • SL closes remaining open position
  • New signal overrides and force-closes any open trade at current close
  • Trail-stop: SL moves to entry breakeven once TP1 is touched
"""

from __future__ import annotations

import argparse
import sys
import warnings
from dataclasses import dataclass
from typing import Optional

import matplotlib
matplotlib.use("Agg")          # headless — saves to PNG
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────────────
# Parameters
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Params:
    rsi_len:     int   = 14
    fast_len:    int   = 9
    slow_len:    int   = 45
    ma_type:     str   = "RMA"   # RMA | LLAMA | Kalman
    atr_len:     int   = 14
    atr_mult_sl: float = 1.5
    rr1:         float = 1.0
    rr2:         float = 2.0
    rr3:         float = 3.0
    use_htf:     bool  = False
    use_chop:    bool  = False
    adx_len:     int   = 14
    adx_min:     float = 20.0
    trail_stop:  bool  = False


TIMEFRAMES = ["15m", "1h", "4h"]

# HTF to use when base TF is 15m / 1h / 4h
HTF_MAP = {"15m": "4h", "1h": "4h", "4h": "1d"}

_C = dict(
    bull="#00e676", bear="#ff5252", neutral="#888888",
    bg="#0d0d1a", panel="#111122", text="#cccccc", grid="#1e1e33",
    tp1="#0e5c4a",  tp2="#0e6e52", tp3="#12a382",
    sl="#7a1f1f",   entry="#1f3a5c",
)


# ─────────────────────────────────────────────────────────────────────────────
# Technical indicators  (matching Pine Script semantics)
# ─────────────────────────────────────────────────────────────────────────────

def _rma(s: pd.Series, n: int) -> pd.Series:
    """Pine ta.rma — Wilder smoothing, alpha = 1/n."""
    return s.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()


def _rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    return 100.0 - 100.0 / (1.0 + _rma(d.clip(lower=0), n) / _rma((-d).clip(lower=0), n))


def _atr(h: pd.Series, l: pd.Series, c: pd.Series, n: int) -> pd.Series:
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return _rma(tr, n)


def _adx(h: pd.Series, l: pd.Series, c: pd.Series, n: int):
    """Returns (plus_di, minus_di, adx)."""
    up, dn = h.diff(), -l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr_s = _atr(h, l, c, n)
    pdi = _rma(pd.Series(pdm, index=h.index), n) / atr_s * 100
    ndi = _rma(pd.Series(ndm, index=h.index), n) / atr_s * 100
    dx  = (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan) * 100
    return pdi, ndi, _rma(dx, n)


def _llama(s: pd.Series, n: int) -> pd.Series:
    """Pine ta.linreg endpoint."""
    def _lr(x):
        m, b = np.polyfit(np.arange(len(x)), x, 1)
        return m * (len(x) - 1) + b
    return s.rolling(n).apply(_lr, raw=True)


def _kalman(s: pd.Series, q: float = 0.001, r: float = 0.01) -> pd.Series:
    """Scalar Kalman filter matching Pine Script implementation."""
    vals = s.values.astype(float)
    x, p = np.empty_like(vals), np.empty_like(vals)
    first_valid = np.argmax(~np.isnan(vals))
    x[first_valid] = vals[first_valid]
    p[first_valid] = 1.0
    for i in range(first_valid + 1, len(vals)):
        if np.isnan(vals[i]):
            x[i], p[i] = x[i - 1], p[i - 1] + q
        else:
            pp = p[i - 1] + q
            k  = pp / (pp + r)
            x[i] = x[i - 1] + k * (vals[i] - x[i - 1])
            p[i] = (1.0 - k) * pp
    x[:first_valid] = np.nan
    return pd.Series(x, index=s.index)


def _get_ma(rsi_val: pd.Series, fast_n: int, slow_n: int, ma_type: str):
    if ma_type == "Kalman":
        fast = _kalman(rsi_val, q=0.001, r=0.01)
        slow = _kalman(rsi_val, q=0.001, r=0.01 * slow_n / fast_n)
    elif ma_type == "LLAMA":
        fast = _llama(rsi_val, fast_n)
        slow = _llama(rsi_val, slow_n)
    else:
        fast = _rma(rsi_val, fast_n)
        slow = _rma(rsi_val, slow_n)
    return fast, slow


# ─────────────────────────────────────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_raw(symbol: str, interval: str, period: str) -> pd.DataFrame:
    raw = yf.download(symbol, period=period, interval=interval,
                      auto_adjust=True, progress=False, multi_level_index=False)
    if raw.empty:
        raise ValueError(f"No data returned for {symbol} @ {interval}")
    raw.columns = [c.lower() for c in raw.columns]
    return raw[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])


def fetch_ohlcv(symbol: str, tf: str) -> pd.DataFrame:
    """Download and resample OHLCV to the requested timeframe."""
    if tf == "15m":
        df = _fetch_raw(symbol, "15m", "60d")
    elif tf == "1h":
        df = _fetch_raw(symbol, "1h", "730d")
    else:  # 4h — resample from 1h
        df = _fetch_raw(symbol, "1h", "730d")
        df = df.resample("4h", closed="left", label="left").agg(
            {"open": "first", "high": "max", "low": "min",
             "close": "last", "volume": "sum"}
        ).dropna(subset=["close"])
    return df


def fetch_htf(symbol: str, tf: str) -> Optional[pd.DataFrame]:
    """Fetch the higher timeframe data for the HTF filter."""
    htf = HTF_MAP.get(tf)
    if htf is None:
        return None
    try:
        if htf == "4h":
            raw = _fetch_raw(symbol, "1h", "730d")
            return raw.resample("4h", closed="left", label="left").agg(
                {"open": "first", "high": "max", "low": "min",
                 "close": "last", "volume": "sum"}
            ).dropna(subset=["close"])
        else:
            return _fetch_raw(symbol, "1d", "max")
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic demo data  (multi-regime OHLCV)
# ─────────────────────────────────────────────────────────────────────────────

_TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400}

# Pre-defined market regime sequences (drift_per_bar, noise_sigma, n_bars)
_REGIMES = [
    ( 0.0030, 0.010, 120),   # bull trend
    ( 0.0005, 0.018, 80),    # ranging / choppy
    (-0.0025, 0.012, 100),   # bear trend
    ( 0.0000, 0.022, 60),    # high-vol chop
    ( 0.0020, 0.009, 150),   # slow bull
    (-0.0015, 0.011, 90),    # mild bear
    ( 0.0010, 0.015, 80),    # low-vol bull
    ( 0.0000, 0.020, 70),    # chop
    ( 0.0035, 0.013, 120),   # strong bull
    (-0.0030, 0.014, 100),   # strong bear
]


def make_synthetic_ohlcv(tf: str, seed: int = 42) -> pd.DataFrame:
    """
    Generate realistic multi-regime OHLCV data that exercises both bull and
    bear RSI crossover signals. Each regime block drives the RSI into
    overbought / oversold territory to trigger the crossovers.
    """
    rng   = np.random.default_rng(seed)
    start = pd.Timestamp("2023-01-01", tz="UTC")
    freq  = _TF_SECONDS.get(tf, 3600)

    closes   = [100.0]
    regimes  = _REGIMES * 3   # ~3000 bars total

    for drift, sigma, n in regimes:
        for _ in range(n):
            ret = drift + sigma * rng.standard_normal()
            closes.append(closes[-1] * (1.0 + ret))

    n_bars = len(closes) - 1
    opens  = np.array([closes[i] for i in range(n_bars)])
    clss   = np.array([closes[i + 1] for i in range(n_bars)])

    # Intrabar extremes: add a random portion of the bar range
    bar_range = np.abs(clss - opens)
    extra     = bar_range * rng.uniform(0.1, 0.7, size=n_bars)
    highs     = np.maximum(opens, clss) + extra
    lows      = np.minimum(opens, clss) - extra * rng.uniform(0.4, 1.0, size=n_bars)
    lows      = np.maximum(lows, 0.01)

    volumes   = rng.lognormal(10.0, 0.5, n_bars)
    ts_index  = pd.date_range(start=start, periods=n_bars, freq=f"{freq}s")

    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": clss, "volume": volumes},
        index=ts_index,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Signal generation
# ─────────────────────────────────────────────────────────────────────────────

def add_signals(df: pd.DataFrame, p: Params,
                htf_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    df = df.copy()
    rsi_val     = _rsi(df["close"], p.rsi_len)
    fast, slow  = _get_ma(rsi_val, p.fast_len, p.slow_len, p.ma_type)
    atr_val     = _atr(df["high"], df["low"], df["close"], p.atr_len)

    # Pine: rawBullCross = fast[1] > slow[1] and fast[2] <= slow[2]
    bull_raw = (fast.shift(1) > slow.shift(1)) & (fast.shift(2) <= slow.shift(2))
    bear_raw = (fast.shift(1) < slow.shift(1)) & (fast.shift(2) >= slow.shift(2))

    # ADX / choppiness filter — Pine uses adxValue[1]
    if p.use_chop:
        _, _, adx_val = _adx(df["high"], df["low"], df["close"], p.adx_len)
        chop_ok = adx_val.shift(1) >= p.adx_min
    else:
        chop_ok = pd.Series(True, index=df.index)

    # HTF trend filter
    if p.use_htf and htf_df is not None:
        htf_rsi       = _rsi(htf_df["close"], p.rsi_len)
        htf_fast, htf_slow = _get_ma(htf_rsi, p.fast_len, p.slow_len, p.ma_type)
        htf_bull = (htf_fast > htf_slow).reindex(df.index, method="ffill").fillna(False)
        htf_bear = (htf_fast < htf_slow).reindex(df.index, method="ffill").fillna(False)
    else:
        htf_bull = pd.Series(True, index=df.index)
        htf_bear = pd.Series(True, index=df.index)

    df["rsi"]         = rsi_val
    df["fast"]        = fast
    df["slow"]        = slow
    df["atr"]         = atr_val
    df["bull_signal"] = bull_raw & chop_ok & htf_bull
    df["bear_signal"] = bear_raw & chop_ok & htf_bear
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Trade simulation
# ─────────────────────────────────────────────────────────────────────────────

def simulate_trades(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    """
    Walk bar-by-bar and emit one record per completed trade.

    Within a bar:
    - If open gaps through SL → exit at open (gap loss).
    - Otherwise TPs are checked before SL (optimistic intra-bar assumption).
    - New signal on the same bar force-closes any open trade first.
    """
    records  = []
    in_trade = False
    is_long  = True
    entry = sl = tp1 = tp2 = tp3 = 0.0
    tp1_hit = tp2_hit = False
    entry_ts = None

    o_arr = df["open"].to_numpy()
    h_arr = df["high"].to_numpy()
    l_arr = df["low"].to_numpy()
    c_arr = df["close"].to_numpy()
    atr_arr  = df["atr"].to_numpy()
    bull_arr = df["bull_signal"].to_numpy()
    bear_arr = df["bear_signal"].to_numpy()
    idx = df.index

    def _pnl(exit_px, t1h, t2h, t3h, force_exit=None):
        risk = abs(entry - sl) or 1e-10
        sgn  = 1.0 if is_long else -1.0
        pnl, rem = 0.0, 1.0
        for hit, lvl in ((t1h, tp1), (t2h, tp2), (t3h, tp3)):
            if hit:
                pnl += sgn * (lvl - entry) / risk / 3
                rem -= 1 / 3
        if rem > 1e-6:
            ex = force_exit if force_exit is not None else exit_px
            pnl += sgn * (ex - entry) / risk * rem
        return round(pnl, 5)

    def _emit(exit_px, reason, t1h, t2h, t3h, exit_ts, force_exit=None):
        pnl = _pnl(exit_px, t1h, t2h, t3h, force_exit)
        records.append(dict(
            entry_time  = entry_ts,
            exit_time   = exit_ts,
            direction   = "Long" if is_long else "Short",
            entry_px    = round(entry, 8),
            exit_px     = round(exit_px if force_exit is None else force_exit, 8),
            sl_px       = round(sl, 8),
            tp1_px      = round(tp1, 8),
            tp2_px      = round(tp2, 8),
            tp3_px      = round(tp3, 8),
            exit_reason = reason,
            tp1_hit     = t1h,
            tp2_hit     = t2h,
            tp3_hit     = t3h,
            pnl_r       = pnl,
            win         = pnl > 0,
        ))

    for i in range(len(df)):
        o, h, l, c = o_arr[i], h_arr[i], l_arr[i], c_arr[i]
        ts = idx[i]

        if in_trade:
            cur_sl = sl  # may have moved to entry if trail_stop

            if is_long:
                # Gap through SL (open ≤ sl)
                if o <= cur_sl:
                    _emit(o, "SL (gap)", tp1_hit, tp2_hit, False, ts)
                    in_trade = False
                else:
                    t1, t2, t3 = tp1_hit, tp2_hit, False
                    # TP hits (checked before SL)
                    if not t1 and h >= tp1:
                        t1 = True
                        if p.trail_stop:
                            sl = entry
                    if not t2 and h >= tp2:
                        t2 = True
                    if h >= tp3:
                        t3 = True
                    # Resolve exit
                    if t3:
                        _emit(tp3, "TP3", t1, t2, t3, ts)
                        in_trade = False
                    elif l <= sl:
                        _emit(sl, "SL", t1, t2, False, ts)
                        in_trade = False
                    else:
                        tp1_hit, tp2_hit = t1, t2
            else:  # short
                if o >= cur_sl:
                    _emit(o, "SL (gap)", tp1_hit, tp2_hit, False, ts)
                    in_trade = False
                else:
                    t1, t2, t3 = tp1_hit, tp2_hit, False
                    if not t1 and l <= tp1:
                        t1 = True
                        if p.trail_stop:
                            sl = entry
                    if not t2 and l <= tp2:
                        t2 = True
                    if l <= tp3:
                        t3 = True
                    if t3:
                        _emit(tp3, "TP3", t1, t2, t3, ts)
                        in_trade = False
                    elif h >= sl:
                        _emit(sl, "SL", t1, t2, False, ts)
                        in_trade = False
                    else:
                        tp1_hit, tp2_hit = t1, t2

        # New signal overrides any open trade
        new_bull = bool(bull_arr[i])
        new_bear = bool(bear_arr[i])

        if (new_bull or new_bear) and not np.isnan(atr_arr[i]):
            if in_trade:
                _emit(c, "Signal Override", tp1_hit, tp2_hit, False, ts, force_exit=c)
            is_long = new_bull
            entry   = c
            rd      = atr_arr[i] * p.atr_mult_sl
            sl  = entry - rd if is_long else entry + rd
            tp1 = entry + rd * p.rr1 if is_long else entry - rd * p.rr1
            tp2 = entry + rd * p.rr2 if is_long else entry - rd * p.rr2
            tp3 = entry + rd * p.rr3 if is_long else entry - rd * p.rr3
            tp1_hit = tp2_hit = False
            in_trade = True
            entry_ts = ts

    return pd.DataFrame(records)


# ─────────────────────────────────────────────────────────────────────────────
# Statistics
# ─────────────────────────────────────────────────────────────────────────────

def compute_stats(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {}
    n   = len(trades)
    pnl = trades["pnl_r"]
    gw  = pnl[pnl > 0].sum()
    gl  = (-pnl[pnl < 0]).sum()
    eq  = pnl.cumsum()
    dd  = (eq - eq.cummax()).min()
    wins = int(trades["win"].sum())
    sl_mask = trades["exit_reason"].str.startswith("SL")
    return dict(
        n            = n,
        wins         = wins,
        losses       = n - wins,
        win_rate     = wins / n * 100,
        total_r      = round(pnl.sum(), 3),
        avg_r        = round(pnl.mean(), 3),
        best_r       = round(pnl.max(), 3),
        worst_r      = round(pnl.min(), 3),
        pf           = round(gw / gl, 3) if gl > 0 else float("inf"),
        max_dd_r     = round(dd, 3),
        tp1_pct      = round(trades["tp1_hit"].mean() * 100, 1),
        tp2_pct      = round(trades["tp2_hit"].mean() * 100, 1),
        tp3_pct      = round(trades["tp3_hit"].mean() * 100, 1),
        sl_pct       = round(sl_mask.mean() * 100, 1),
        override_pct = round((trades["exit_reason"] == "Signal Override").mean() * 100, 1),
        long_n       = int((trades["direction"] == "Long").sum()),
        short_n      = int((trades["direction"] == "Short").sum()),
    )


def print_stats(tf: str, s: dict, bars: int, date_range: str) -> None:
    if not s:
        print(f"  [{tf}]  0 bars — no trades generated.")
        return
    pf = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "∞"
    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║  {tf:^4}  {date_range:<57}║
║  {bars} bars{' ' * (60 - len(str(bars)) - 5)}║
╠══════════════════════════════════════════════════════════════════╣
║  Total Trades : {s['n']:<5}  (Long {s['long_n']} / Short {s['short_n']}){' ' * (28 - len(str(s['long_n'])) - len(str(s['short_n'])))}║
║  Win Rate     : {s['win_rate']:>5.1f}%  ({s['wins']}W / {s['losses']}L){' ' * (35 - len(str(s['wins'])) - len(str(s['losses'])))}║
║  Total R      : {s['total_r']:>+8.2f}R{' ' * 44}║
║  Avg R/Trade  : {s['avg_r']:>+8.2f}R{' ' * 44}║
║  Best / Worst : {s['best_r']:>+6.2f}R / {s['worst_r']:>+6.2f}R{' ' * 38}║
║  Profit Factor: {pf:<48}║
║  Max Drawdown : {s['max_dd_r']:>+8.2f}R{' ' * 44}║
╠══════════════════════════════════════════════════════════════════╣
║  TP1 hit      : {s['tp1_pct']:>5.1f}%{' ' * 50}║
║  TP2 hit      : {s['tp2_pct']:>5.1f}%{' ' * 50}║
║  TP3 hit      : {s['tp3_pct']:>5.1f}%{' ' * 50}║
║  SL exit      : {s['sl_pct']:>5.1f}%{' ' * 50}║
║  Override exit: {s['override_pct']:>5.1f}%{' ' * 50}║
╚══════════════════════════════════════════════════════════════════╝""")


# ─────────────────────────────────────────────────────────────────────────────
# Plotting
# ─────────────────────────────────────────────────────────────────────────────

def _style(ax, title=""):
    ax.set_facecolor(_C["panel"])
    ax.tick_params(colors=_C["neutral"], labelsize=7)
    for sp in ax.spines.values():
        sp.set_color(_C["grid"])
    ax.grid(color=_C["grid"], linewidth=0.4)
    if title:
        ax.set_title(title, color=_C["text"], fontsize=9, pad=4)


def _plot_price(ax, df: pd.DataFrame, trades: pd.DataFrame, tf: str):
    _style(ax, f"Price + Trades  [{tf}]")
    tail = df.tail(800)
    ax.plot(tail.index, tail["close"], color="#666", linewidth=0.5, zorder=1)

    if trades.empty:
        return

    for _, t in trades.iterrows():
        color = _C["bull"] if t["win"] else _C["bear"]
        marker = "^" if t["direction"] == "Long" else "v"
        et, xt = t["entry_time"], t["exit_time"]
        if et in df.index and xt in df.index:
            ax.axvspan(et, xt, alpha=0.07, color=color, zorder=0)
        if et in df.index:
            ep = df.loc[et, "close"]
            ax.scatter(et, ep, marker=marker, s=30, color=color, zorder=3, linewidths=0)

        # Annotate entry / SL / TP levels for last 10 trades
        recent_idx = trades.index[-10:]
        if t.name in recent_idx and et in df.index:
            atr_v = df.loc[et, "atr"]
            offset = atr_v * 0.3
            y_txt = t["entry_px"] - offset if t["direction"] == "Long" else t["entry_px"] + offset
            ax.annotate(
                f"{t['exit_reason']}\n{t['pnl_r']:+.2f}R",
                xy=(et, t["entry_px"]),
                xytext=(et, y_txt),
                color=color, fontsize=6, ha="center",
                arrowprops=dict(arrowstyle="-", color=color, lw=0.5),
            )


def _plot_equity(ax, trades: pd.DataFrame):
    _style(ax, "Equity Curve (R)")
    if trades.empty:
        ax.text(0.5, 0.5, "No trades", ha="center", va="center",
                color=_C["neutral"], transform=ax.transAxes)
        return
    eq = trades["pnl_r"].cumsum().reset_index(drop=True)
    pos_mask = eq >= 0
    ax.plot(eq.index, eq.values, color=_C["bull"] if eq.iloc[-1] >= 0 else _C["bear"],
            linewidth=1.0, zorder=2)
    ax.fill_between(eq.index, eq.values, 0,
                    where=pos_mask.values, alpha=0.15, color=_C["bull"])
    ax.fill_between(eq.index, eq.values, 0,
                    where=~pos_mask.values, alpha=0.15, color=_C["bear"])
    ax.axhline(0, color=_C["grid"], linewidth=0.7, linestyle="--")
    ax.set_xlabel("Trade #", color=_C["neutral"], fontsize=7)
    ax.set_ylabel("R", color=_C["neutral"], fontsize=7)


def _plot_dist(ax, trades: pd.DataFrame):
    _style(ax, "P&L Distribution (R)")
    if trades.empty:
        return
    bins = min(40, max(10, len(trades) // 3))
    ax.hist(trades["pnl_r"], bins=bins, color="#5c7aff", alpha=0.75,
            edgecolor=_C["bg"], linewidth=0.3)
    ax.axvline(0, color=_C["bear"], linewidth=0.8, linestyle="--")
    ax.axvline(trades["pnl_r"].mean(), color="#ffd700", linewidth=0.8,
               linestyle=":", label=f"Mean {trades['pnl_r'].mean():+.2f}R")
    ax.legend(fontsize=6, framealpha=0.3, labelcolor=_C["text"])
    ax.set_xlabel("R", color=_C["neutral"], fontsize=7)
    ax.set_ylabel("# Trades", color=_C["neutral"], fontsize=7)


def _plot_monthly(ax, trades: pd.DataFrame):
    _style(ax, "Monthly P&L (R)")
    if trades.empty:
        return
    trades = trades.copy()
    trades["month"] = pd.to_datetime(trades["exit_time"]).dt.to_period("M")
    monthly = trades.groupby("month")["pnl_r"].sum()
    colors = [_C["bull"] if v >= 0 else _C["bear"] for v in monthly.values]
    ax.bar(range(len(monthly)), monthly.values, color=colors, alpha=0.8)
    ax.axhline(0, color=_C["grid"], linewidth=0.7, linestyle="--")
    if len(monthly) <= 24:
        ax.set_xticks(range(len(monthly)))
        ax.set_xticklabels([str(m) for m in monthly.index], rotation=45,
                           fontsize=5, color=_C["neutral"])
    ax.set_ylabel("R", color=_C["neutral"], fontsize=7)


def _stat_box(ax, s: dict, tf: str):
    ax.set_facecolor(_C["bg"])
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])

    if not s:
        ax.text(0.5, 0.5, "No trades", ha="center", va="center",
                color=_C["neutral"], transform=ax.transAxes, fontsize=10)
        return

    pf = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "∞"
    lines = [
        (f"{tf}  Summary", "#ffffff", 11),
        ("", _C["neutral"], 8),
        (f"Trades   {s['n']:>5}  ({s['long_n']}L / {s['short_n']}S)", _C["text"], 8),
        (f"Win Rate {s['win_rate']:>5.1f}%  ({s['wins']}W / {s['losses']}L)", _C["text"], 8),
        ("", _C["neutral"], 8),
        (f"Total R  {s['total_r']:>+8.2f}R", _C["bull"] if s["total_r"] >= 0 else _C["bear"], 9),
        (f"Avg R    {s['avg_r']:>+8.2f}R", _C["text"], 8),
        (f"Max DD   {s['max_dd_r']:>+8.2f}R", _C["bear"], 8),
        (f"PF       {pf:>8}", _C["text"], 8),
        ("", _C["neutral"], 8),
        (f"TP1 {s['tp1_pct']:>5.1f}%  TP2 {s['tp2_pct']:>5.1f}%  TP3 {s['tp3_pct']:>5.1f}%", _C["tp3"], 7),
        (f"SL  {s['sl_pct']:>5.1f}%  Override {s['override_pct']:>5.1f}%", "#e08030", 7),
    ]
    y = 0.97
    for text, color, size in lines:
        ax.text(0.05, y, text, transform=ax.transAxes, color=color,
                fontsize=size, va="top", fontfamily="monospace")
        y -= 0.085


def plot_results(symbol: str, results: dict, out_path: str) -> None:
    n_tf = len(results)
    if n_tf == 0:
        print("No results to plot.")
        return

    fig = plt.figure(figsize=(22, 8 * n_tf), facecolor=_C["bg"])
    outer = gridspec.GridSpec(n_tf, 1, figure=fig, hspace=0.5)

    for row, (tf, (df, trades, s)) in enumerate(results.items()):
        inner = gridspec.GridSpecFromSubplotSpec(
            2, 4, subplot_spec=outer[row], hspace=0.5, wspace=0.35,
            height_ratios=[2.5, 1]
        )
        ax_price   = fig.add_subplot(inner[0, :])
        ax_equity  = fig.add_subplot(inner[1, 0])
        ax_dist    = fig.add_subplot(inner[1, 1])
        ax_monthly = fig.add_subplot(inner[1, 2])
        ax_stat    = fig.add_subplot(inner[1, 3])

        _plot_price(ax_price, df, trades, tf)
        _plot_equity(ax_equity, trades)
        _plot_dist(ax_dist, trades)
        _plot_monthly(ax_monthly, trades)
        _stat_box(ax_stat, s, tf)

    fig.suptitle(
        f"Cardwell RSI Trade Navigator — {symbol}",
        color="white", fontsize=15, y=1.005, fontweight="bold"
    )
    plt.savefig(out_path, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Chart saved → {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    ap = argparse.ArgumentParser(
        description="Cardwell RSI Trade Navigator — multi-timeframe backtester",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("symbol", nargs="?", default="BTC-USD",
                    help="yfinance ticker (e.g. BTC-USD, AAPL, ETH-USD)")
    ap.add_argument("--demo", action="store_true",
                    help="use synthetic multi-regime data (no internet required)")
    ap.add_argument("--timeframes", nargs="+", default=TIMEFRAMES,
                    choices=TIMEFRAMES, help="which TFs to test")
    ap.add_argument("--ma-type",      default="RMA", choices=["RMA", "LLAMA", "Kalman"])
    ap.add_argument("--rsi-len",      type=int,   default=14)
    ap.add_argument("--fast-len",     type=int,   default=9)
    ap.add_argument("--slow-len",     type=int,   default=45)
    ap.add_argument("--atr-len",      type=int,   default=14)
    ap.add_argument("--atr-mult",     type=float, default=1.5, dest="atr_mult_sl")
    ap.add_argument("--rr1",          type=float, default=1.0)
    ap.add_argument("--rr2",          type=float, default=2.0)
    ap.add_argument("--rr3",          type=float, default=3.0)
    ap.add_argument("--use-htf",      action="store_true",
                    help="enable HTF trend filter (4h for 15m/1h, 1d for 4h)")
    ap.add_argument("--use-chop",     action="store_true",
                    help="enable ADX / choppiness filter")
    ap.add_argument("--adx-len",      type=int,   default=14)
    ap.add_argument("--adx-min",      type=float, default=20.0)
    ap.add_argument("--trail-stop",   action="store_true",
                    help="move SL to breakeven after TP1")
    ap.add_argument("--no-plot",      action="store_true")
    ap.add_argument("--out",          default="cardwell_backtest.png",
                    help="output chart file")
    ap.add_argument("--csv",          default="",
                    help="export trade log CSV (e.g. trades.csv)")
    return ap.parse_args()


def main() -> int:
    args = parse_args()

    p = Params(
        rsi_len     = args.rsi_len,
        fast_len    = args.fast_len,
        slow_len    = args.slow_len,
        ma_type     = args.ma_type,
        atr_len     = args.atr_len,
        atr_mult_sl = args.atr_mult_sl,
        rr1         = args.rr1,
        rr2         = args.rr2,
        rr3         = args.rr3,
        use_htf     = args.use_htf,
        use_chop    = args.use_chop,
        adx_len     = args.adx_len,
        adx_min     = args.adx_min,
        trail_stop  = args.trail_stop,
    )

    symbol  = "SYNTHETIC (demo)" if args.demo else args.symbol
    results = {}
    all_trades: list[pd.DataFrame] = []

    print(f"\n{'═' * 68}")
    print(f"  Cardwell RSI Trade Navigator — Backtester")
    print(f"  Symbol: {symbol}  |  MA: {p.ma_type}  |  RSI: {p.rsi_len}"
          f"  |  Fast/Slow: {p.fast_len}/{p.slow_len}")
    print(f"  ATR×{p.atr_mult_sl} SL  |  RR {p.rr1}/{p.rr2}/{p.rr3}"
          f"  |  HTF: {'on' if p.use_htf else 'off'}"
          f"  |  ADX: {'on' if p.use_chop else 'off'}"
          f"  |  Trail: {'on' if p.trail_stop else 'off'}")
    print(f"{'═' * 68}")

    htf_cache: dict[str, Optional[pd.DataFrame]] = {}

    for tf in args.timeframes:
        if args.demo:
            print(f"\n  ► Generating synthetic [{tf}] …", end=" ", flush=True)
            # Use different seeds per TF so results differ meaningfully
            seed_map = {"15m": 42, "1h": 7, "4h": 13}
            df = make_synthetic_ohlcv(tf, seed=seed_map.get(tf, 42))
        else:
            print(f"\n  ► Fetching {symbol} [{tf}] …", end=" ", flush=True)
            try:
                df = fetch_ohlcv(symbol, tf)
            except Exception as e:
                print(f"\n  ERROR: {e}")
                continue
        print(f"{len(df)} bars  ({df.index[0].date()} → {df.index[-1].date()})")

        htf_df: Optional[pd.DataFrame] = None
        if p.use_htf and not args.demo:
            htf_key = HTF_MAP.get(tf, "")
            if htf_key not in htf_cache:
                htf_cache[htf_key] = fetch_htf(symbol, tf)
            htf_df = htf_cache[htf_key]
        elif p.use_htf and args.demo:
            htf_seed = {"15m": 99, "1h": 55, "4h": 33}
            # Simulate HTF from the same seed family but with longer bars
            htf_df = make_synthetic_ohlcv("4h", seed=htf_seed.get(tf, 99))

        df     = add_signals(df, p, htf_df)
        trades = simulate_trades(df, p)
        s      = compute_stats(trades)

        date_range = f"{df.index[0].date()} – {df.index[-1].date()}"
        print_stats(tf, s, len(df), date_range)

        if not trades.empty:
            trades["timeframe"] = tf
            all_trades.append(trades)

        results[tf] = (df, trades, s)

    # Combined summary
    if len(results) > 1:
        combined = pd.concat(all_trades) if all_trades else pd.DataFrame()
        sc = compute_stats(combined)
        if sc:
            print(f"\n{'─' * 68}")
            print(f"  ALL TIMEFRAMES COMBINED")
            pf_str = "∞" if sc["pf"] == float("inf") else f"{sc['pf']:.2f}"
            print(f"  Trades: {sc['n']}  Win%: {sc['win_rate']:.1f}%"
                  f"  Total R: {sc['total_r']:+.2f}R  PF: {pf_str}")
            print(f"{'─' * 68}")

    if args.csv and all_trades:
        csv_path = args.csv
        pd.concat(all_trades).to_csv(csv_path, index=False)
        print(f"\n  Trade log saved → {csv_path}")

    if not args.no_plot:
        print(f"\n  Generating chart …")
        plot_results(symbol, results, args.out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
