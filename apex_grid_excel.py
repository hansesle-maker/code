#!/usr/bin/env python3
"""
APEX Multi-Confluence Scalper — Full Parameter Grid Search → Excel
===================================================================
BTCUSDT 1h data  |  Periods: 2024 / 2025 / 2026-YTD

Speed optimisations
  • Numba JIT for ATR, Supertrend, Simulation (≈10–30× vs pure Python)
  • Indicator pre-cache: each unique (stLen, stMult, stRsiLen, volLen) set is
    computed once per period, not once per combo
  • 4-process pool for signal combination + simulation

Grid (~9 500 valid combos)
  emaFast  : 5, 9, 13, 21
  emaSlow  : 21, 34, 55
  stLen    : 7, 10, 14, 20
  stMult   : 1.5, 2.0, 2.5, 3.0, 3.5
  stRsiLen : 7, 10, 14
  stRsiK   : 3, 5
  volLen   : 14, 20, 30
  volMult  : 1.1, 1.2, 1.5, 2.0
  minScore : 4, 5, 6
  minBars  : 3, 5, 8, 12

Fixed
  stRsiD=3, OB=80, OS=20, SL×1.5, TP1×1.5, TP2×3.0, TP1-close=50%
  commission=0.05%, useBE=True, atrLen=14
"""

from __future__ import annotations
import sys, time, itertools, warnings, pickle
from multiprocessing import Pool, cpu_count
from datetime import timezone
from typing import Dict, List, Tuple, Optional

import numpy as np
import pandas as pd
from numba import njit
import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
CSV_PATH = "/root/.claude/uploads/8ed44bb4-cef8-5fe0-8472-6b910828669f/9bf1c6f2-BINANCE_BTCUSDT.P_60.csv"
OUTPUT   = "apex_grid_results_1h.xlsx"

GRID = {
    "emaFast" : [5, 9, 13, 21],
    "emaSlow" : [21, 34, 55],
    "stLen"   : [7, 10, 14, 20],
    "stMult"  : [1.5, 2.0, 2.5, 3.0, 3.5],
    "stRsiLen": [7, 10, 14],
    "stRsiK"  : [3, 5],
    "volLen"  : [14, 20, 30],
    "volMult" : [1.1, 1.2, 1.5, 2.0],
    "minScore": [4, 5, 6],
    "minBars" : [3, 5, 8, 12],
}

FIXED = dict(
    stRsiD=3, stRsiOB=80, stRsiOS=20,
    slMult=1.5, tp1Mult=1.5, tp2Mult=3.0, tp1Pct=0.50,
    useBE=True, atrLen=14, commission=0.0005,
)

PERIODS = {
    "2024": ("2024-01-01", "2025-01-01"),
    "2025": ("2025-01-01", "2026-01-01"),
    "2026": ("2026-01-01", "2026-06-28"),
}
PERIOD_NAMES = list(PERIODS.keys())
PARAM_COLS   = list(GRID.keys())
WARMUP_DAYS  = 60   # extra history before each period for indicator warm-up

# ─────────────────────────────────────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────────────────────────────────────
def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower().replace(" ","_") for c in df.columns]
    df["dt"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("dt").sort_index()
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[["open","high","low","close","volume"]].dropna()

def split_periods(full: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    out = {}
    for name, (start, end) in PERIODS.items():
        s = pd.Timestamp(start, tz="UTC") - pd.Timedelta(days=WARMUP_DAYS)
        e = pd.Timestamp(end,   tz="UTC")
        out[name] = full[(full.index >= s) & (full.index < e)].copy()
    return out

# ─────────────────────────────────────────────────────────────────────────────
# NUMBA JIT INDICATORS
# ─────────────────────────────────────────────────────────────────────────────
@njit(cache=True)
def _atr_jit(high, low, close, n):
    m = len(close)
    tr  = np.empty(m)
    atr = np.empty(m)
    tr[0] = high[0] - low[0]
    for i in range(1, m):
        tr[i] = max(high[i]-low[i],
                    abs(high[i]-close[i-1]),
                    abs(low[i]-close[i-1]))
    alpha = 1.0 / n
    atr[0] = tr[0]
    for i in range(1, m):
        atr[i] = alpha * tr[i] + (1.0 - alpha) * atr[i-1]
    return atr

@njit(cache=True)
def _supertrend_dir_jit(high, low, close, atr_v, mult):
    m   = len(close)
    hl2 = (high + low) * 0.5
    fu  = np.full(m, np.nan)
    fl  = np.full(m, np.nan)
    dr  = np.ones(m, dtype=np.int8)
    for i in range(1, m):
        av = atr_v[i]
        if np.isnan(av):
            fu[i] = fu[i-1] if not np.isnan(fu[i-1]) else hl2[i]+mult
            fl[i] = fl[i-1] if not np.isnan(fl[i-1]) else hl2[i]-mult
            dr[i] = dr[i-1]; continue
        bu = hl2[i] + mult*av;  bl = hl2[i] - mult*av
        fup = fu[i-1] if not np.isnan(fu[i-1]) else bu
        flp = fl[i-1] if not np.isnan(fl[i-1]) else bl
        fu[i] = bu if (bu < fup or close[i-1] > fup) else fup
        fl[i] = bl if (bl > flp or close[i-1] < flp) else flp
        if   dr[i-1] ==  1 and close[i] > fu[i]: dr[i] = np.int8(-1)
        elif dr[i-1] == -1 and close[i] < fl[i]: dr[i] = np.int8( 1)
        else:                                      dr[i] = dr[i-1]
    return dr

@njit(cache=True)
def _simulate_jit(cl, hi, lo, atr_v, long_raw, short_raw,
                  SLM, TP1M, TP2M, TP1P, USE_BE, MB, COM, WARM):
    n = len(cl)
    buf = np.zeros(n // 2 + 4)
    nt  = 0
    in_trade = False; direction = 0
    epx = slp = tp1 = tp2 = 0.0
    rem = 1.0; bea = False; par = 0.0
    bs  = np.int32(MB)

    for i in range(WARM, n):
        h = hi[i]; l = lo[i]; c = cl[i]

        if in_trade:
            if direction == 1:
                sln = epx if bea else slp
                t1h = (h >= tp1) and (not bea)
                slh = l <= sln
                if slh and not t1h:
                    buf[nt] = par + rem*((sln-epx)/epx - COM); nt+=1
                    in_trade=False; bs=0; continue
                if t1h:
                    q = rem*TP1P
                    par += q*((tp1-epx)/epx - COM); rem -= q; bea = USE_BE
                    if slh:
                        sn2 = epx if bea else slp
                        buf[nt] = par + rem*((sn2-epx)/epx - COM); nt+=1
                        in_trade=False; bs=0; continue
                if bea:
                    if h >= tp2:
                        buf[nt] = par + rem*((tp2-epx)/epx - COM); nt+=1
                        in_trade=False; bs=0; continue
                    if l <= epx:
                        buf[nt] = par + rem*(-COM); nt+=1
                        in_trade=False; bs=0; continue
            else:
                sln = epx if bea else slp
                t1h = (l <= tp1) and (not bea)
                slh = h >= sln
                if slh and not t1h:
                    buf[nt] = par + rem*((epx-sln)/epx - COM); nt+=1
                    in_trade=False; bs=0; continue
                if t1h:
                    q = rem*TP1P
                    par += q*((epx-tp1)/epx - COM); rem -= q; bea = USE_BE
                    if slh:
                        sn2 = epx if bea else slp
                        buf[nt] = par + rem*((epx-sn2)/epx - COM); nt+=1
                        in_trade=False; bs=0; continue
                if bea:
                    if l <= tp2:
                        buf[nt] = par + rem*((epx-tp2)/epx - COM); nt+=1
                        in_trade=False; bs=0; continue
                    if h >= epx:
                        buf[nt] = par + rem*(-COM); nt+=1
                        in_trade=False; bs=0; continue
        else:
            bs += 1

        if not in_trade and bs >= MB:
            av = atr_v[i]
            if av <= 0.0 or np.isnan(av): continue
            if long_raw[i]:
                in_trade=True; direction=1; epx=c
                slp=c-av*SLM; tp1=c+av*TP1M; tp2=c+av*TP2M
                rem=1.0; bea=False; par=0.0; bs=0
            elif short_raw[i]:
                in_trade=True; direction=-1; epx=c
                slp=c+av*SLM; tp1=c-av*TP1M; tp2=c-av*TP2M
                rem=1.0; bea=False; par=0.0; bs=0

    if in_trade:
        lc = cl[-1]
        pnl = (par + rem*((lc-epx if direction==1 else epx-lc)/epx - COM))
        buf[nt] = pnl; nt += 1

    return buf[:nt]

# ─────────────────────────────────────────────────────────────────────────────
# INDICATOR PRE-CACHE (per period)
# ─────────────────────────────────────────────────────────────────────────────
def _ema_np(s, n):
    return s.ewm(span=n, adjust=False).mean().values

def _sma_np(s, n):
    return s.rolling(n, min_periods=1).mean().values

def _rsi_np(close_arr, n):
    d = np.diff(close_arr, prepend=close_arr[0])
    ag = np.empty(len(close_arr)); al = np.empty(len(close_arr))
    ag[0] = max(d[0], 0); al[0] = max(-d[0], 0)
    a = 1.0/n
    for i in range(1, len(d)):
        ag[i] = a*max(d[i],0) + (1-a)*ag[i-1]
        al[i] = a*max(-d[i],0) + (1-a)*al[i-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(al==0, 1e9, ag/al)
    return 100 - 100/(1+rs)

def build_cache(df: pd.DataFrame) -> dict:
    """Pre-compute all unique indicator arrays for one period slice."""
    hi = df["high"].values; lo = df["low"].values
    cl = df["close"].values; vo = df["volume"].values
    n  = len(cl)

    cache: dict = {"cl": cl, "hi": hi, "lo": lo, "vo": vo, "n": n}

    # ── EMA ──────────────────────────────────────────────────────────────────
    cache["ema"] = {}
    for p in set(GRID["emaFast"]) | set(GRID["emaSlow"]):
        cache["ema"][p] = _ema_np(df["close"], p)

    # ── ATR (per stLen, fixed atrLen=14 for SL/TP) ────────────────────────
    cache["atr14"] = _atr_jit(hi, lo, cl, 14)  # for SL/TP sizing
    cache["atr_st"] = {}
    for sL in GRID["stLen"]:
        cache["atr_st"][sL] = _atr_jit(hi, lo, cl, sL)

    # ── Supertrend direction ──────────────────────────────────────────────
    cache["st_dir"] = {}
    for sL in GRID["stLen"]:
        atr_v = cache["atr_st"][sL]
        for sM in GRID["stMult"]:
            cache["st_dir"][(sL, sM)] = _supertrend_dir_jit(hi, lo, cl, atr_v, sM)

    # ── Stochastic RSI ──────────────────────────────────────────────────
    cache["k_up"]   = {}
    cache["k_down"] = {}
    rsiOB = FIXED["stRsiOB"]; rsiOS = FIXED["stRsiOS"]; D = FIXED["stRsiD"]
    for rL in GRID["stRsiLen"]:
        rsi_v = _rsi_np(cl, rL)
        rsi_s = pd.Series(rsi_v)
        rlo = rsi_s.rolling(rL, min_periods=1).min().values
        rhi = rsi_s.rolling(rL, min_periods=1).max().values
        raw = np.where(rhi!=rlo, (rsi_v-rlo)/(rhi-rlo)*100, 50)
        for kS in GRID["stRsiK"]:
            k = pd.Series(raw).rolling(kS, min_periods=1).mean().values
            d = pd.Series(k).rolling(D,  min_periods=1).mean().values
            k_shift = np.roll(k, 1); k_shift[0] = k[0]
            d_shift = np.roll(d, 1); d_shift[0] = d[0]
            ku = (k > d) & (k_shift <= d_shift) & (k < rsiOB)
            kd = (k < d) & (k_shift >= d_shift) & (k > rsiOS)
            cache["k_up"]  [(rL, kS)] = ku
            cache["k_down"][(rL, kS)] = kd

    # ── Volume MA ─────────────────────────────────────────────────────────
    cache["vol_ma"] = {}
    for vL in GRID["volLen"]:
        cache["vol_ma"][vL] = _sma_np(df["volume"], vL)

    # ── VWAP (daily reset, UTC) ──────────────────────────────────────────
    hlc3  = (df["high"] + df["low"] + df["close"]) / 3
    dates = df.index.date
    pv    = hlc3 * df["volume"]
    cum_pv  = pv.groupby(dates).cumsum().values
    cum_vol = df["volume"].groupby(dates).cumsum().values
    cache["vwap"] = np.where(cum_vol > 0, cum_pv/cum_vol, cl)

    # ── RSI velocity (3-bar, fixed rsiLen=14) ────────────────────────────
    rsi14 = _rsi_np(cl, 14)
    rsi_vel = rsi14 - np.roll(rsi14, 3); rsi_vel[:3] = 0
    cache["rising_mom"]  = rsi_vel > 0
    cache["falling_mom"] = rsi_vel < 0
    cache["above_vwap"]  = cl > cache["vwap"]
    cache["below_vwap"]  = cl < cache["vwap"]

    return cache

# ─────────────────────────────────────────────────────────────────────────────
# PER-COMBO SIGNAL + SIMULATION
# ─────────────────────────────────────────────────────────────────────────────
def run_combo(args):
    """Worker: given a list of combos + caches, return rows of metrics."""
    combos, caches = args
    SLM  = FIXED["slMult"]; TP1M = FIXED["tp1Mult"]; TP2M = FIXED["tp2Mult"]
    TP1P = FIXED["tp1Pct"]; USE_BE = FIXED["useBE"];  COM  = FIXED["commission"]
    ms_map = FIXED  # alias
    rows = []
    # Warm numba up on first call (JIT compilation)
    _dummy_cl = np.ones(10); _dummy_b = np.zeros(10, dtype=np.bool_)
    _simulate_jit(_dummy_cl,_dummy_cl,_dummy_cl,_dummy_cl,_dummy_b,_dummy_b,
                  1.5,1.5,3.0,0.5,True,3,0.0005,5)

    for p in combos:
        ef=p["emaFast"]; es=p["emaSlow"]; sL=p["stLen"]; sM=p["stMult"]
        rL=p["stRsiLen"]; kS=p["stRsiK"]; vL=p["volLen"]; vM=p["volMult"]
        ms=p["minScore"]; mb=p["minBars"]

        row = {k: p[k] for k in PARAM_COLS}

        for yr, cache in caches.items():
            cl  = cache["cl"]; hi = cache["hi"]; lo = cache["lo"]
            atr = cache["atr14"]
            n   = cache["n"]

            # ── Signals ──────────────────────────────────────────────────
            ef_a = cache["ema"][ef]; es_a = cache["ema"][es]
            ema_l = (ef_a > es_a).astype(np.int8)
            ema_s = (ef_a < es_a).astype(np.int8)

            st = cache["st_dir"][(sL, sM)]
            st_bull = (st < 0).astype(np.int8)
            st_bear = (st > 0).astype(np.int8)

            k_up   = cache["k_up"]  [(rL, kS)].astype(np.int8)
            k_down = cache["k_down"][(rL, kS)].astype(np.int8)

            vol_ok = (cache["vo"] > cache["vol_ma"][vL] * vM).astype(np.int8)

            abv = cache["above_vwap"].astype(np.int8)
            blw = cache["below_vwap"].astype(np.int8)
            rim = cache["rising_mom"].astype(np.int8)
            flm = cache["falling_mom"].astype(np.int8)

            ls = ema_l + st_bull + k_up   + vol_ok + abv + rim
            ss = ema_s + st_bear + k_down + vol_ok + blw + flm

            long_raw  = (ls >= ms) & (ls >= ss)
            short_raw = (ss >= ms) & (ss >  ls)

            WARM = min(max(ef, es, sL, rL*2, vL, 60), n - 10)

            trades = _simulate_jit(
                cl, hi, lo, atr,
                long_raw.astype(np.bool_), short_raw.astype(np.bool_),
                SLM, TP1M, TP2M, TP1P, USE_BE, mb, COM, WARM
            )

            # ── Metrics ──────────────────────────────────────────────────
            if len(trades) == 0:
                m = dict(n=0, wr=0.0, pnl=0.0, pf=0.0, avgW=0.0, avgL=0.0,
                         maxDD=0.0, sharpe=0.0)
            else:
                wins   = trades[trades > 0]
                losses = trades[trades <= 0]
                wr   = len(wins)/len(trades)*100
                pnl  = float(trades.sum()*100)
                pf   = (float(wins.sum())/float(-losses.sum())
                        if len(losses)>0 and losses.sum()<0 else 99.0)
                avgW = float(wins.mean()*100)   if len(wins)   else 0.0
                avgL = float(losses.mean()*100) if len(losses) else 0.0
                cum  = np.cumsum(trades)
                peak = np.maximum.accumulate(cum)
                dd   = float((cum-peak).min()*100)
                # Annualised Sharpe (assuming each trade ~4h average hold on 1h data)
                shr  = (float(trades.mean())/float(trades.std()+1e-9)
                        * np.sqrt(252*6) if len(trades)>1 else 0.0)
                m = dict(n=len(trades), wr=round(wr,1), pnl=round(pnl,2),
                         pf=round(min(pf,99.0),2), avgW=round(avgW,3),
                         avgL=round(avgL,3), maxDD=round(dd,2), sharpe=round(shr,2))

            for k, v in m.items():
                row[f"{yr}_{k}"] = v

        # Composite score weighted across years (2026 gets 0.5 weight = partial year)
        row["total_pnl"]   = round(sum(row.get(f"{yr}_pnl",0) for yr in PERIOD_NAMES), 2)
        row["total_trades"]= sum(row.get(f"{yr}_n",0) for yr in PERIOD_NAMES)
        w = {"2024": 1.0, "2025": 1.0, "2026": 0.5}
        denom = sum(w[yr]*(1+abs(row.get(f"{yr}_maxDD",0))) for yr in PERIOD_NAMES)
        row["score"] = round(
            sum(w[yr]*row.get(f"{yr}_pnl",0)*min(row.get(f"{yr}_pf",0),5)
                *(1+row.get(f"{yr}_wr",0)/100) for yr in PERIOD_NAMES) / denom, 4)

        rows.append(row)
    return rows

# ─────────────────────────────────────────────────────────────────────────────
# EXCEL EXPORT
# ─────────────────────────────────────────────────────────────────────────────
HDR_FILL  = PatternFill("solid", fgColor="1A1A2E")
HDR_FONT  = Font(color="FFFFFF", bold=True, size=9)
GRP_FILLS = {
    "2024": PatternFill("solid", fgColor="E3F0FF"),
    "2025": PatternFill("solid", fgColor="E3FFE8"),
    "2026": PatternFill("solid", fgColor="FFF8E3"),
}
ALT_FILL  = PatternFill("solid", fgColor="F7F7F7")
POS_FONT  = Font(color="0B6623")
NEG_FONT  = Font(color="CC0000")
THIN      = Side(style="thin", color="CCCCCC")
BORDER    = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER    = Alignment(horizontal="center", vertical="center")
RIGHT     = Alignment(horizontal="right")

METRIC_COLS = ["n","wr","pnl","pf","avgW","avgL","maxDD","sharpe"]
METRIC_HDRS = ["Trades","Win%","PnL%","PF","avgWin%","avgLoss%","maxDD%","Sharpe"]

def _col_defs(period_names):
    """Return list of (header, col_key, width) for all columns."""
    cols = []
    for pk in PARAM_COLS:
        w = 8 if pk not in ("emaFast","emaSlow") else 8
        cols.append((pk, pk, w))
    for yr in period_names:
        for mc, mh in zip(METRIC_COLS, METRIC_HDRS):
            cols.append((f"{yr} {mh}", f"{yr}_{mc}", 9))
    cols += [("TotalPnL%","total_pnl",10),
             ("TotalTrades","total_trades",10),
             ("Score","score",9)]
    return cols

def _write_sheet(ws, df_sub, period_names, title_row=True):
    col_defs = _col_defs(period_names)

    if title_row:
        ws.row_dimensions[1].height = 30
        # Year group headers
        col_idx = len(PARAM_COLS) + 1
        for yr in period_names:
            start = col_idx; end = col_idx + len(METRIC_COLS) - 1
            ws.merge_cells(start_row=1, start_column=start,
                           end_row=1, end_column=end)
            c = ws.cell(1, start, yr)
            c.fill  = GRP_FILLS.get(yr, HDR_FILL)
            c.font  = Font(bold=True, size=10)
            c.alignment = CENTER
            col_idx += len(METRIC_COLS)
        # Empty param header
        ws.merge_cells(start_row=1, start_column=1,
                       end_row=1, end_column=len(PARAM_COLS))
        c = ws.cell(1, 1, "Parameters")
        c.fill = HDR_FILL; c.font = HDR_FONT; c.alignment = CENTER
        # Summary cols
        for i, (h,_,w) in enumerate(col_defs[len(PARAM_COLS)+len(period_names)*len(METRIC_COLS):], 1):
            ws.merge_cells(start_row=1,
                           start_column=len(PARAM_COLS)+len(period_names)*len(METRIC_COLS)+i,
                           end_row=1,
                           end_column=len(PARAM_COLS)+len(period_names)*len(METRIC_COLS)+i)
        hdr_row = 2
    else:
        hdr_row = 1

    # Column headers
    for ci, (h, key, w) in enumerate(col_defs, 1):
        c = ws.cell(hdr_row, ci, h)
        c.fill = HDR_FILL; c.font = HDR_FONT; c.alignment = CENTER
        ws.column_dimensions[get_column_letter(ci)].width = w

    ws.freeze_panes = ws.cell(hdr_row+1, 1)
    ws.auto_filter.ref = (f"A{hdr_row}:"
                          f"{get_column_letter(len(col_defs))}{hdr_row}")

    for ri, (_, row) in enumerate(df_sub.iterrows(), hdr_row+1):
        fill = ALT_FILL if ri % 2 == 0 else None
        for ci, (h, key, w) in enumerate(col_defs, 1):
            val = row.get(key, "")
            c = ws.cell(ri, ci, val)
            c.alignment = RIGHT
            c.border = BORDER
            if fill:
                c.fill = fill
            # Colour PnL% columns
            if "pnl" in key or key == "total_pnl":
                if isinstance(val, (int,float)):
                    c.font = POS_FONT if val >= 0 else NEG_FONT
            c.number_format = (
                "#,##0.00" if isinstance(val, float) else
                "#,##0"    if isinstance(val, int)   else "@"
            )

def write_excel(df_all: pd.DataFrame, path: str):
    from openpyxl import Workbook
    wb = Workbook()

    sheets = [
        ("ALL_RESULTS",  df_all.sort_values("score", ascending=False)),
        ("2024_TOP",     df_all.sort_values("2024_pnl", ascending=False).head(500)),
        ("2025_TOP",     df_all.sort_values("2025_pnl", ascending=False).head(500)),
        ("2026_TOP",     df_all.sort_values("2026_pnl", ascending=False).head(500)),
        ("3YR_BEST",     df_all.sort_values("score", ascending=False).head(500)),
        ("3YR_WORST",    df_all.sort_values("score", ascending=True).head(200)),
    ]

    for i, (name, sub) in enumerate(sheets):
        ws = wb.active if i == 0 else wb.create_sheet(name)
        if i == 0: ws.title = name
        print(f"  Writing sheet [{name}] ({len(sub)} rows)…", flush=True)
        _write_sheet(ws, sub, PERIOD_NAMES, title_row=True)

    # ── Parameter Sensitivity sheet ────────────────────────────────────────
    ws_s = wb.create_sheet("PARAM_SENSITIVITY")
    ws_s.title = "PARAM_SENSITIVITY"
    yr_cols = [f"{yr}_pnl" for yr in PERIOD_NAMES] + ["total_pnl", "score"]
    yr_hdrs = [f"{yr} PnL%" for yr in PERIOD_NAMES] + ["Total PnL%", "Score"]

    # Header
    hdrs = ["Parameter", "Value"] + yr_hdrs
    for ci, h in enumerate(hdrs, 1):
        c = ws_s.cell(1, ci, h)
        c.fill = HDR_FILL; c.font = HDR_FONT; c.alignment = CENTER
        ws_s.column_dimensions[get_column_letter(ci)].width = 14

    ri = 2
    for param in PARAM_COLS:
        grp = df_all.groupby(param)[yr_cols].mean().round(2)
        for val, row_data in grp.iterrows():
            ws_s.cell(ri, 1, param)
            ws_s.cell(ri, 2, val)
            for ci, col in enumerate(yr_cols, 3):
                v = row_data[col]
                c = ws_s.cell(ri, ci, v)
                c.number_format = "#,##0.00"
                c.font = POS_FONT if v >= 0 else NEG_FONT
            ri += 1
        ri += 1  # blank row between params

    ws_s.freeze_panes = ws_s["A2"]

    wb.save(path)
    print(f"\nExcel saved → {path}")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def gen_combos():
    keys = PARAM_COLS
    for vals in itertools.product(*[GRID[k] for k in keys]):
        p = dict(zip(keys, vals))
        if p["emaFast"] < p["emaSlow"]:
            yield p

def main():
    t0 = time.time()

    # ── Load data ──────────────────────────────────────────────────────────
    print("Loading CSV…", flush=True)
    full_df = load_csv(CSV_PATH)
    print(f"  {len(full_df)} bars  ({full_df.index[0].date()} → {full_df.index[-1].date()})")
    period_dfs = split_periods(full_df)
    for yr, df in period_dfs.items():
        print(f"  {yr}: {len(df)} bars (incl. {WARMUP_DAYS}d warmup)")

    # ── Build indicator caches ─────────────────────────────────────────────
    print("\nBuilding indicator caches…", flush=True)
    caches = {}
    for yr, df in period_dfs.items():
        t1 = time.time()
        caches[yr] = build_cache(df)
        print(f"  {yr}: done in {time.time()-t1:.1f}s")

    # ── Combos ────────────────────────────────────────────────────────────
    combos = list(gen_combos())
    print(f"\nGrid: {len(combos):,} valid combos × {len(PERIODS)} periods")

    # ── JIT warm-up (compile numba in main process before forking) ─────────
    print("Compiling numba JIT functions…", end=" ", flush=True)
    dc = np.ones(200, dtype=np.float64)
    db = np.zeros(200, dtype=np.bool_)
    _atr_jit(dc, dc*0.9, dc, 14)
    _supertrend_dir_jit(dc, dc*0.9, dc, _atr_jit(dc, dc*0.9, dc, 10), 2.0)
    _simulate_jit(dc, dc, dc*0.9, dc*0.01, db, db, 1.5,1.5,3.0,0.5,True,3,0.0005,20)
    print("done", flush=True)

    # ── Parallel grid search ──────────────────────────────────────────────
    N_WORKERS = min(cpu_count(), 4)
    chunk_size = max(1, len(combos) // (N_WORKERS * 8))
    chunks = [(combos[i:i+chunk_size], caches)
              for i in range(0, len(combos), chunk_size)]

    print(f"Running backtest ({N_WORKERS} workers, chunk={chunk_size})…", flush=True)
    t1 = time.time()

    all_rows = []
    done = 0
    with Pool(N_WORKERS) as pool:
        for batch in pool.imap_unordered(run_combo, chunks):
            all_rows.extend(batch)
            done += len(batch)
            elapsed = time.time() - t1
            rate = done / elapsed if elapsed > 0 else 1
            eta  = (len(combos) - done) / rate
            print(f"  {done:>6,}/{len(combos):,}  {elapsed:.0f}s  ETA {eta:.0f}s",
                  end="\r", flush=True)

    print(f"\nDone in {time.time()-t0:.1f}s total", flush=True)

    # ── Build DataFrame & save CSV ─────────────────────────────────────────
    df_all = pd.DataFrame(all_rows)
    df_all.to_csv("apex_grid_results_1h.csv", index=False)
    print(f"CSV saved → apex_grid_results_1h.csv  ({len(df_all):,} rows)")

    # ── Print quick summary ────────────────────────────────────────────────
    print("\n── TOP 10 OVERALL (by composite score) ─────────────────────")
    disp_cols = PARAM_COLS + ["2024_pnl","2024_wr","2025_pnl","2025_wr",
                               "2026_pnl","2026_wr","total_pnl","score"]
    top10 = df_all.sort_values("score", ascending=False).head(10)[disp_cols]
    top10.index = range(1, 11)
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", "{:.2f}".format)
    print(top10.to_string())

    print("\n── PARAM SENSITIVITY (mean total_pnl%) ──────────────────────")
    for param in PARAM_COLS:
        grp = df_all.groupby(param)["total_pnl"].mean().round(2)
        vals = "  |  ".join(f"{k}: {v:+.1f}%" for k, v in grp.items())
        print(f"  {param:10s}  {vals}")

    # ── Excel ─────────────────────────────────────────────────────────────
    print(f"\nWriting Excel ({OUTPUT})…")
    write_excel(df_all, OUTPUT)

    print(f"\nTotal elapsed: {time.time()-t0:.1f}s")

if __name__ == "__main__":
    main()
