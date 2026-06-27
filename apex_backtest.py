#!/usr/bin/env python3
"""
APEX Multi-Confluence Scalper — Grid Search Backtester
======================================================
Periods: 2024, 2025, 2026-YTD  |  Symbol: BTCUSDT 1h (Binance Futures)

Usage
-----
  # Real data (requires Binance Futures access):
  python apex_backtest.py --symbol BTCUSDT --interval 1h

  # Synthetic BTC-like data (no internet needed):
  python apex_backtest.py --synthetic

  # Limit grid size for a quick run:
  python apex_backtest.py --synthetic --top 10

Fixed parameters (not searched)
--------------------------------
  stRsiK=3, stRsiD=3, stRsiOB=80, stRsiOS=20
  slMult=1.5, tp1Mult=1.5, tp2Mult=3.0, tp1Pct=50%
  riskPct=1.0, useBE=True, atrLen=14, commission=0.05%

Grid parameters (576 combinations)
------------------------------------
  emaFast  : 9, 13
  emaSlow  : 21, 34
  stLen    : 7, 10, 14
  stMult   : 2.0, 3.0
  stRsiLen : 10, 14
  volLen   : 14, 20
  volMult  : 1.2, 1.5
  minScore : 4, 5, 6
  minBars  : 3, 5
"""

from __future__ import annotations
import sys, time, argparse, itertools, warnings
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple, Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────────────
# PERIODS
# ─────────────────────────────────────────────────────────────────────────────
PERIODS = {
    "2024": ("2024-01-01", "2025-01-01"),
    "2025": ("2025-01-01", "2026-01-01"),
    "2026": ("2026-01-01", "2026-06-28"),  # YTD
}

# ─────────────────────────────────────────────────────────────────────────────
# GRID
# ─────────────────────────────────────────────────────────────────────────────
GRID = {
    "emaFast" : [9, 13],
    "emaSlow" : [21, 34],
    "stLen"   : [7, 10, 14],
    "stMult"  : [2.0, 3.0],
    "stRsiLen": [10, 14],
    "volLen"  : [14, 20],
    "volMult" : [1.2, 1.5],
    "minScore": [4, 5, 6],
    "minBars" : [3, 5],
}

FIXED = {
    "stRsiK": 3, "stRsiD": 3, "stRsiOB": 80, "stRsiOS": 20,
    "slMult": 1.5, "tp1Mult": 1.5, "tp2Mult": 3.0, "tp1Pct": 0.50,
    "useBE": True, "atrLen": 14, "commission": 0.0005,
}

def all_combos():
    keys = list(GRID.keys())
    for vals in itertools.product(*GRID.values()):
        p = dict(zip(keys, vals))
        p.update(FIXED)
        if p["emaFast"] >= p["emaSlow"]:   # skip invalid
            continue
        yield p

# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCHING (Binance Futures)
# ─────────────────────────────────────────────────────────────────────────────
def _ms(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)

def fetch_binance(symbol: str, interval: str, start: str, end: str) -> pd.DataFrame:
    import requests
    base = "https://fapi.binance.com/fapi/v1/klines"
    step = {"1h": 3_600_000, "4h": 14_400_000}[interval]
    start_ms, end_ms = _ms(start), _ms(end)
    rows = []
    cur = start_ms
    while cur < end_ms:
        r = requests.get(base, params={
            "symbol": symbol, "interval": interval,
            "startTime": cur, "endTime": end_ms, "limit": 1000,
        }, timeout=20)
        r.raise_for_status()
        raw = r.json()
        if not raw:
            break
        rows.extend(raw)
        last = int(raw[-1][0])
        if last >= end_ms - step:
            break
        cur = last + step
        time.sleep(0.05)

    seen = {}
    for k in rows:
        seen[int(k[0])] = k
    rows = sorted(seen.values(), key=lambda x: int(x[0]))
    rows = [r for r in rows if start_ms <= int(r[0]) < end_ms]

    df = pd.DataFrame(rows, columns=[
        "ts","open","high","low","close","volume",
        "close_time","qav","trades","tbbav","tbqav","ignore"
    ])
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df["datetime"] = pd.to_datetime(df["ts"].astype(int), unit="ms", utc=True)
    return df.set_index("datetime").sort_index()[["open","high","low","close","volume"]]

# ─────────────────────────────────────────────────────────────────────────────
# SYNTHETIC BTC DATA
# ─────────────────────────────────────────────────────────────────────────────
def gen_synthetic(start: str, end: str, seed: int = 42) -> pd.DataFrame:
    """Geometric Brownian Motion with momentum & volume spikes — BTC-like."""
    rng = np.random.default_rng(seed + hash(start) % 1000)
    s_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    e_dt = datetime.strptime(end,   "%Y-%m-%d").replace(tzinfo=timezone.utc)
    n = int((e_dt - s_dt).total_seconds() / 3600)

    # BTC approximate starting prices by year
    start_px = {"2024": 42_000.0, "2025": 98_000.0, "2026": 102_000.0}
    drift_yr  = {"2024":  1.4,     "2025": -0.1,     "2026":  0.3}
    vol_yr    = {"2024":  0.72,    "2025":  0.80,    "2026":  0.65}

    yr = start[:4]
    S0 = start_px.get(yr, 50_000.0)
    mu = np.log(1 + drift_yr.get(yr, 0)) / 8760   # per hour
    sigma = vol_yr.get(yr, 0.70) / np.sqrt(8760)   # per hour

    # GBM with momentum (AR-1 on returns)
    eps = rng.standard_normal(n)
    mom = np.zeros(n)
    phi = 0.15  # momentum factor
    for i in range(1, n):
        mom[i] = phi * mom[i-1] + eps[i]
    log_ret = mu + sigma * mom
    prices  = S0 * np.exp(np.cumsum(log_ret))

    # OHLC: add intrabar noise
    hl_range = prices * sigma * rng.gamma(1.5, 1, n) * 2
    open_off  = hl_range * rng.uniform(-0.3, 0.3, n)
    opens  = prices * (1 + rng.normal(0, sigma * 0.3, n))
    highs  = np.maximum(opens, prices) + hl_range * rng.uniform(0, 0.5, n)
    lows   = np.minimum(opens, prices) - hl_range * rng.uniform(0, 0.5, n)

    # Volume: spiky distribution
    vol_base = 1_000 + prices * 0.05
    vol = vol_base * rng.gamma(1.2, 1, n)
    vol *= (1 + 2.0 * (vol > np.percentile(vol, 90)).astype(float))  # volume spikes

    idx = pd.date_range(start=s_dt, periods=n, freq="h", tz="UTC")
    return pd.DataFrame({
        "open":   opens,
        "high":   highs,
        "low":    lows,
        "close":  prices,
        "volume": vol,
    }, index=idx)

# ─────────────────────────────────────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────────────────────────────────────
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=1).mean()

def calc_atr(high, low, close, n):
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def calc_rsi(close, n=14):
    d = close.diff()
    ag = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    al = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def calc_supertrend(high, low, close, n, mult):
    atr_v = calc_atr(high, low, close, n).values
    hl2   = ((high + low) / 2).values
    cl    = close.values
    m     = len(cl)

    fu = np.full(m, np.nan)
    fl = np.full(m, np.nan)
    dr = np.ones(m, dtype=int)   # 1 = bear, -1 = bull

    for i in range(1, m):
        if np.isnan(atr_v[i]):
            fu[i] = fu[i-1] if not np.isnan(fu[i-1]) else hl2[i] + mult * 1
            fl[i] = fl[i-1] if not np.isnan(fl[i-1]) else hl2[i] - mult * 1
            dr[i] = dr[i-1]
            continue
        bu = hl2[i] + mult * atr_v[i]
        bl = hl2[i] - mult * atr_v[i]
        fu_p = fu[i-1] if not np.isnan(fu[i-1]) else bu
        fl_p = fl[i-1] if not np.isnan(fl[i-1]) else bl
        fu[i] = bu if bu < fu_p or cl[i-1] > fu_p else fu_p
        fl[i] = bl if bl > fl_p or cl[i-1] < fl_p else fl_p
        if dr[i-1] == 1:
            dr[i] = -1 if cl[i] > fu[i] else 1
        else:
            dr[i] =  1 if cl[i] < fl[i] else -1

    return pd.Series(dr, index=close.index)

def calc_stochrsi(close, rsi_n, k_sm, d_sm):
    rsi_v = calc_rsi(close, rsi_n)
    lo = rsi_v.rolling(rsi_n, min_periods=1).min()
    hi = rsi_v.rolling(rsi_n, min_periods=1).max()
    raw = np.where(hi != lo, (rsi_v - lo) / (hi - lo) * 100, 50)
    k = pd.Series(raw, index=close.index).rolling(k_sm, min_periods=1).mean()
    d = k.rolling(d_sm, min_periods=1).mean()
    return k, d

def calc_vwap(df: pd.DataFrame) -> pd.Series:
    hlc3 = (df["high"] + df["low"] + df["close"]) / 3
    dates = df.index.date
    pv = hlc3 * df["volume"]
    cum_pv  = pv.groupby(dates).cumsum()
    cum_vol = df["volume"].groupby(dates).cumsum()
    return cum_pv / cum_vol.replace(0, np.nan)

# ─────────────────────────────────────────────────────────────────────────────
# SIGNALS
# ─────────────────────────────────────────────────────────────────────────────
def compute_signals(df: pd.DataFrame, p: dict) -> Tuple[np.ndarray, np.ndarray]:
    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    vol   = df["volume"]

    ef = ema(close, p["emaFast"])
    es = ema(close, p["emaSlow"])

    st_dir = calc_supertrend(high, low, close, p["stLen"], p["stMult"])
    st_bull = (st_dir < 0).astype(int)
    st_bear = (st_dir > 0).astype(int)

    k, d = calc_stochrsi(close, p["stRsiLen"], p["stRsiK"], p["stRsiD"])
    k_up   = ((k > d) & (k.shift() <= d.shift()) & (k < p["stRsiOB"])).astype(int)
    k_down = ((k < d) & (k.shift() >= d.shift()) & (k > p["stRsiOS"])).astype(int)

    vol_ma = sma(vol, p["volLen"])
    vol_ok = (vol > vol_ma * p["volMult"]).astype(int)

    vwap   = calc_vwap(df)
    abv    = (close > vwap).astype(int)
    blw    = (close < vwap).astype(int)

    rsi_v  = calc_rsi(close)
    rv     = rsi_v - rsi_v.shift(3)
    rise   = (rv > 0).astype(int)
    fall   = (rv < 0).astype(int)

    ls = (ef > es).astype(int) + st_bull + k_up   + vol_ok + abv + rise
    ss = (ef < es).astype(int) + st_bear + k_down + vol_ok + blw + fall

    # Higher score wins; tie → long
    ms = p["minScore"]
    long_raw  = ((ls >= ms) & (ls >= ss)).values
    short_raw = ((ss >= ms) & (ss >  ls)).values
    atr_v     = calc_atr(high, low, close, p["atrLen"]).values

    return long_raw, short_raw, atr_v

# ─────────────────────────────────────────────────────────────────────────────
# SIMULATION
# ─────────────────────────────────────────────────────────────────────────────
def simulate(df: pd.DataFrame, long_raw, short_raw, atr_v, p) -> List[float]:
    SLM  = p["slMult"];   TP1M = p["tp1Mult"]; TP2M = p["tp2Mult"]
    TP1P = p["tp1Pct"];   USE_BE = p["useBE"]; MB   = p["minBars"]
    COM  = p["commission"]

    cl = df["close"].values
    hi = df["high"].values
    lo = df["low"].values

    n = len(cl)
    trades: List[float] = []

    in_trade = False
    direction = 0
    entry_px = sl_px = tp1_px = tp2_px = 0.0
    remaining = 1.0
    be_active = False
    partial   = 0.0
    bars_since = MB   # start ready

    WARM = max(60, p["emaFast"] + p["emaSlow"] + p["stRsiLen"] + 10)

    for i in range(WARM, n):
        h, l, c = hi[i], lo[i], cl[i]

        if in_trade:
            if direction == 1:          # ── LONG ──
                sl_now   = entry_px if be_active else sl_px
                tp1_hit  = (h >= tp1_px) and (not be_active)
                sl_hit   = l <= sl_now

                if sl_hit and not tp1_hit:
                    pnl = partial + remaining * ((sl_now - entry_px) / entry_px - COM)
                    trades.append(pnl); in_trade = False; bars_since = 0; continue

                if tp1_hit:
                    qty = remaining * TP1P
                    partial += qty * ((tp1_px - entry_px) / entry_px - COM)
                    remaining -= qty
                    be_active = USE_BE
                    if sl_hit:  # both same bar → SL after TP1 for remainder
                        sl_now2 = entry_px if be_active else sl_px
                        pnl = partial + remaining * ((sl_now2 - entry_px) / entry_px - COM)
                        trades.append(pnl); in_trade = False; bars_since = 0; continue

                if be_active:
                    if h >= tp2_px:
                        pnl = partial + remaining * ((tp2_px - entry_px) / entry_px - COM)
                        trades.append(pnl); in_trade = False; bars_since = 0; continue
                    if l <= entry_px:   # BE stop
                        pnl = partial + remaining * (0 - COM)
                        trades.append(pnl); in_trade = False; bars_since = 0; continue

            else:                       # ── SHORT ──
                sl_now   = entry_px if be_active else sl_px
                tp1_hit  = (l <= tp1_px) and (not be_active)
                sl_hit   = h >= sl_now

                if sl_hit and not tp1_hit:
                    pnl = partial + remaining * ((entry_px - sl_now) / entry_px - COM)
                    trades.append(pnl); in_trade = False; bars_since = 0; continue

                if tp1_hit:
                    qty = remaining * TP1P
                    partial += qty * ((entry_px - tp1_px) / entry_px - COM)
                    remaining -= qty
                    be_active = USE_BE
                    if sl_hit:
                        sl_now2 = entry_px if be_active else sl_px
                        pnl = partial + remaining * ((entry_px - sl_now2) / entry_px - COM)
                        trades.append(pnl); in_trade = False; bars_since = 0; continue

                if be_active:
                    if l <= tp2_px:
                        pnl = partial + remaining * ((entry_px - tp2_px) / entry_px - COM)
                        trades.append(pnl); in_trade = False; bars_since = 0; continue
                    if h >= entry_px:
                        pnl = partial + remaining * (0 - COM)
                        trades.append(pnl); in_trade = False; bars_since = 0; continue
        else:
            bars_since += 1

        # ── ENTRY (process_orders_on_close → fill this bar's close) ──
        if not in_trade and bars_since >= MB:
            av = atr_v[i]
            if np.isnan(av) or av == 0:
                continue
            if long_raw[i]:
                in_trade = True; direction = 1; entry_px = c
                sl_px  = c - av * SLM;  tp1_px = c + av * TP1M;  tp2_px = c + av * TP2M
                remaining = 1.0; be_active = False; partial = 0.0; bars_since = 0
            elif short_raw[i]:
                in_trade = True; direction = -1; entry_px = c
                sl_px  = c + av * SLM;  tp1_px = c - av * TP1M;  tp2_px = c - av * TP2M
                remaining = 1.0; be_active = False; partial = 0.0; bars_since = 0

    # Close at last bar
    if in_trade:
        lc = cl[-1]
        if direction == 1:
            pnl = partial + remaining * ((lc - entry_px) / entry_px - COM)
        else:
            pnl = partial + remaining * ((entry_px - lc) / entry_px - COM)
        trades.append(pnl)

    return trades

# ─────────────────────────────────────────────────────────────────────────────
# METRICS
# ─────────────────────────────────────────────────────────────────────────────
def metrics(trades: List[float]) -> dict:
    if not trades:
        return dict(n=0, wr=0, pnl=0, pf=0, avgW=0, avgL=0, maxDD=0, score=0)
    wins   = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    wr  = len(wins) / len(trades) * 100
    pnl = sum(trades) * 100
    pf  = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 99.0
    avgW = np.mean(wins)   * 100 if wins   else 0
    avgL = np.mean(losses) * 100 if losses else 0
    cum  = np.cumsum(trades)
    peak = np.maximum.accumulate(cum)
    maxDD = (cum - peak).min() * 100
    # Composite score: penalise small n and big DD
    score = pnl * min(pf, 5) * (1 + wr / 100) / (1 + abs(maxDD))
    return dict(n=len(trades), wr=round(wr,1), pnl=round(pnl,2),
                pf=round(pf,2), avgW=round(avgW,3), avgL=round(avgL,3),
                maxDD=round(maxDD,2), score=round(score,4))

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def run(use_synthetic: bool, symbol: str, interval: str, top_n: int):
    combos = list(all_combos())
    print(f"Grid: {len(combos)} combinations × {len(PERIODS)} periods")

    # Load data for each period
    period_dfs: Dict[str, pd.DataFrame] = {}
    for name, (start, end) in PERIODS.items():
        print(f"  Loading {name} ({start} → {end}) ...", end=" ", flush=True)
        if use_synthetic:
            df = gen_synthetic(start, end, seed=int(name))
            print(f"[SYNTHETIC] {len(df)} bars")
        else:
            df = fetch_binance(symbol, interval, start, end)
            print(f"[BINANCE] {len(df)} bars")
        period_dfs[name] = df

    print(f"\nRunning backtest...", flush=True)
    t0 = time.time()

    all_rows = []
    for ci, p in enumerate(combos):
        if (ci + 1) % 100 == 0:
            elapsed = time.time() - t0
            rate = (ci + 1) / elapsed
            eta = (len(combos) - ci - 1) / rate
            print(f"  {ci+1}/{len(combos)}  {elapsed:.0f}s elapsed  ETA {eta:.0f}s", flush=True)

        row = {
            "emaFast":  p["emaFast"],
            "emaSlow":  p["emaSlow"],
            "stLen":    p["stLen"],
            "stMult":   p["stMult"],
            "stRsiLen": p["stRsiLen"],
            "volLen":   p["volLen"],
            "volMult":  p["volMult"],
            "minScore": p["minScore"],
            "minBars":  p["minBars"],
        }

        for name, df in period_dfs.items():
            lr, sr, av = compute_signals(df, p)
            tr = simulate(df, lr, sr, av, p)
            m  = metrics(tr)
            for k, v in m.items():
                row[f"{name}_{k}"] = v

        # Cross-period composite (sum of scores)
        row["total_score"] = sum(row.get(f"{yr}_score", 0) for yr in PERIODS)
        row["total_pnl"]   = sum(row.get(f"{yr}_pnl",   0) for yr in PERIODS)
        all_rows.append(row)

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s  ({elapsed/len(combos)*1000:.1f}ms/combo)")

    df_res = pd.DataFrame(all_rows).sort_values("total_score", ascending=False)

    # ── Save CSV ──────────────────────────────────────────────────────────────
    out_csv = "apex_grid_results.csv"
    df_res.to_csv(out_csv, index=False)
    print(f"\nFull results → {out_csv}")

    # ── Print tables per period ───────────────────────────────────────────────
    PARAMS = ["emaFast","emaSlow","stLen","stMult","stRsiLen","volLen","volMult","minScore","minBars"]

    for yr in list(PERIODS.keys()) + ["total"]:
        sort_col = f"{yr}_score" if yr != "total" else "total_score"
        if sort_col not in df_res.columns and yr == "total":
            sort_col = "total_score"
        sub = df_res.sort_values(sort_col, ascending=False).head(top_n).copy()

        if yr == "total":
            stat_cols = ["total_pnl", "total_score"]
            for p_yr in PERIODS:
                stat_cols += [f"{p_yr}_n", f"{p_yr}_wr", f"{p_yr}_pnl", f"{p_yr}_pf", f"{p_yr}_maxDD"]
        else:
            stat_cols = [f"{yr}_n", f"{yr}_wr", f"{yr}_pnl", f"{yr}_pf", f"{yr}_avgW", f"{yr}_avgL", f"{yr}_maxDD"]

        print(f"\n{'='*120}")
        if yr == "total":
            print(f"  TOP {top_n}  ─  ALL PERIODS COMBINED (ranked by composite score)")
        else:
            print(f"  TOP {top_n}  ─  {yr}")
        print(f"{'='*120}")

        disp = sub[PARAMS + stat_cols].reset_index(drop=True)
        disp.index += 1
        # Rename for display
        rename = {
            f"{yr}_n": "trades", f"{yr}_wr": "winRate%",
            f"{yr}_pnl": "PnL%",  f"{yr}_pf": "PF",
            f"{yr}_avgW": "avgW%", f"{yr}_avgL": "avgL%",
            f"{yr}_maxDD": "maxDD%",
            "total_pnl": "totalPnL%", "total_score": "score",
        }
        for p_yr in PERIODS:
            rename.update({
                f"{p_yr}_n":   f"{p_yr}_n",
                f"{p_yr}_wr":  f"{p_yr}_wr%",
                f"{p_yr}_pnl": f"{p_yr}_PnL%",
                f"{p_yr}_pf":  f"{p_yr}_PF",
                f"{p_yr}_maxDD": f"{p_yr}_DD%",
            })
        disp = disp.rename(columns=rename)
        with pd.option_context("display.max_columns", None, "display.width", 200,
                               "display.float_format", "{:.2f}".format):
            print(disp.to_string())

    # ── Worst combos ─────────────────────────────────────────────────────────
    print(f"\n{'='*120}")
    print(f"  BOTTOM 5  ─  AVOID (worst total score)")
    print(f"{'='*120}")
    worst = df_res.tail(5)[PARAMS + ["total_pnl","total_score"]].reset_index(drop=True)
    worst.index += 1
    print(worst.to_string())

    # ── Parameter sensitivity ─────────────────────────────────────────────────
    print(f"\n{'='*120}")
    print(f"  PARAMETER SENSITIVITY (mean total PnL% by value)")
    print(f"{'='*120}")
    for param in PARAMS:
        grp = df_res.groupby(param)["total_pnl"].mean().round(2)
        vals = "  |  ".join(f"{k}: {v:+.2f}%" for k, v in grp.items())
        print(f"  {param:10s}  {vals}")

    if use_synthetic:
        print("\n" + "!"*80)
        print("  WARNING: Results are based on SYNTHETIC BTC-like data.")
        print("  Run with real Binance data locally:")
        print("    python apex_backtest.py --symbol BTCUSDT --interval 1h")
        print("!"*80)

# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true",
                    help="Use synthetic BTC-like data instead of Binance")
    ap.add_argument("--symbol",   default="BTCUSDT")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--top",      type=int, default=20,
                    help="How many top results to print per period")
    args = ap.parse_args()
    run(use_synthetic=args.synthetic,
        symbol=args.symbol,
        interval=args.interval,
        top_n=args.top)
