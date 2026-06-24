#!/usr/bin/env python3
"""Daily Close Comparison strategy — NON-REPAINTING / repaint-aware backtest.

A faithful, look-ahead-aware port of ChartArt's "Daily Close Comparison
Strategy" (TradingView). The rule:

    closeDiff = (today_close - yesterday_close) / yesterday_close
    buying = closeDiff >  threshold ? long
           : closeDiff < -threshold ? short
           : hold previous           (hysteresis deadband)

i.e. go long while the current higher-TF ("daily") close is above the previous
close, short while below, with a threshold deadband.

Why it repaints, and the three modes (shared with occ_strategy)
---------------------------------------------------------------
``today_close = security(tickerid, 'D', close)`` is the CURRENTLY-FORMING daily
close, which on intraday bars equals the live price. TradingView shows the day's
FINAL close on historical bars (repaint).

    - nonrepaint: today = last CLOSED daily close (updates only at day close).
    - realtime  : today = forming daily close = current price, re-checked every
                  refresh bar (repaint-aware but look-ahead-free → achievable).
    - lookahead : today = the day's FINAL close applied early (TV repaint;
                  inflated, NOT achievable live).

"전략 TF (alt)" = the comparison timeframe (Daily by default); "반영 주기
(refresh)" = how often the forming bar is re-evaluated. The shared engine
(occ_strategy.run_events) handles position/cost/SL-TP and the favorable-entry
filter identically to the OCC strategy.

Usage (run where Binance is reachable, e.g. your Oracle VM):

    python3 dcc_strategy.py --symbol BTCUSDT --alt 1d --refresh 15m --mode realtime
    python3 dcc_strategy.py --symbol BTCUSDT --alt 1d --refresh 15m --compare-modes
    python3 dcc_strategy.py --selftest
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from tsi_signal.data import Candle, fetch_klines_range
import occ_strategy as occ
from occ_strategy import INTERVAL_MS, _resample, _synth, buy_hold, run_events
from strategy_lab import Result, format_table


@dataclass
class DCCParams:
    threshold: float = 0.0            # fraction (0.001 = 0.1%); deadband half-width
    mult: int = 96                    # alt = base × mult (15m × 96 = 1d)
    use_res: bool = True
    trade_type: str = "BOTH"          # LONG | SHORT | BOTH | NONE
    sl_pct: float = 0.0
    tp_pct: float = 0.0
    mode: str = "nonrepaint"          # nonrepaint | realtime | lookahead
    entry_filter: bool = False
    filter_tol_bps: float = 0.0
    # ma_* fields are unused here but kept so run_events' duck-typing is happy.
    ma_len: int = 1


def dcc_series(c_base: List[Candle], p: DCCParams) -> Tuple[List[float], List[float]]:
    """(prev_close, cur_close) on the base grid for the chosen repaint mode.

    prev_close = previous CLOSED higher-TF bar's close (always settled).
    cur_close  = today's close, defined per mode (forming / last-closed / final).
    """
    n = len(c_base)
    if n == 0:
        return [], []
    base_step = (c_base[1].open_time - c_base[0].open_time) if n > 1 else 1
    factor = p.mult if p.use_res else 1
    alt_step = base_step * factor
    cl = [c.close for c in c_base]
    prev = [0.0] * n
    cur = [0.0] * n

    if p.mode == "lookahead":
        alt = _resample(c_base, factor)
        closes = [c.close for c in alt]
        idx = {ac.open_time // alt_step: k for k, ac in enumerate(alt)}
        last = -1
        for i, c in enumerate(c_base):
            k = idx.get(c.open_time // alt_step, last)
            last = k if k is not None else last
            if k is not None and k >= 0:
                cur[i] = closes[k]                       # current day's FINAL close (future)
                prev[i] = closes[k - 1] if k >= 1 else closes[k]
            else:
                cur[i] = prev[i] = cl[i]
        return prev, cur

    # realtime / nonrepaint share the streaming walk over completed buckets.
    done: List[float] = []
    cur_bucket: Optional[int] = None
    for i, c in enumerate(c_base):
        b = c.open_time // alt_step
        if b != cur_bucket:
            if cur_bucket is not None:
                done.append(c_base[i - 1].close)         # the bucket that just closed
            cur_bucket = b
        if p.mode == "realtime":
            cur[i] = cl[i]                                # forming daily close = live price
            prev[i] = done[-1] if done else cl[i]
        else:                                            # nonrepaint
            cur[i] = done[-1] if done else cl[i]         # last CLOSED daily close
            prev[i] = done[-2] if len(done) >= 2 else (done[-1] if done else cl[i])
    return prev, cur


def buying_series(prev: List[float], cur: List[float], thr: float) -> List[bool]:
    n = len(prev)
    out = [False] * n
    state: Optional[bool] = None
    for i in range(n):
        p0 = prev[i]
        diff = (cur[i] - p0) / p0 if p0 > 0 else 0.0
        if diff > thr:
            state = True
        elif diff < -thr:
            state = False
        elif state is None:
            state = diff >= 0
        out[i] = state
    return out


def backtest_dcc(c_base: List[Candle], p: DCCParams, fee_rate: float = 0.0005,
                 slip_rate: float = 0.0001, warmup: Optional[int] = None,
                 name: str = "DCC") -> Result:
    n = len(c_base)
    if warmup is None:
        warmup = 2 * max(1, p.mult) + 50                 # need ≥2 settled higher-TF closes
    prev, cur = dcc_series(c_base, p)
    buy = buying_series(prev, cur, p.threshold)

    long_evt = [False] * n
    short_evt = [False] * n
    if warmup < n:                                       # establish the initial side at warmup
        long_evt[warmup] = buy[warmup]
        short_evt[warmup] = not buy[warmup]
    for j in range(warmup + 1, n):                       # then act on state flips
        if buy[j] and not buy[j - 1]:
            long_evt[j] = True
        elif not buy[j] and buy[j - 1]:
            short_evt[j] = True

    return run_events(c_base, long_evt, short_evt, p, fee_rate, slip_rate, warmup, name)


# --------------------------------------------------------------------------- #
# JSON wrapper for the web UI
# --------------------------------------------------------------------------- #
def run_dcc_web(c_base: List[Candle], config: dict) -> dict:
    p = DCCParams(
        threshold=float(config.get("threshold_pct", 0.0)) / 100.0,
        mult=max(1, int(config.get("mult", 96))),
        use_res=bool(config.get("use_res", True)),
        trade_type=config.get("trade_type", "BOTH"),
        sl_pct=float(config.get("sl_pct", 0.0)),
        tp_pct=float(config.get("tp_pct", 0.0)),
        mode=config.get("mode", "nonrepaint"),
        entry_filter=bool(config.get("entry_filter", False)),
        filter_tol_bps=float(config.get("filter_tol_bps", 0.0)),
    )
    fee = config.get("fee_bps", 5.0) / 10000.0
    slip = config.get("slip_bps", 1.0) / 10000.0

    res = backtest_dcc(c_base, p, fee, slip, name="DCC")
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

    return dict(ok=True, bars=len(c_base), mode=p.mode,
                entry_filter=p.entry_filter, skipped=getattr(res, "skipped", 0),
                threshold_pct=round(p.threshold * 100, 4),
                strategy=stats(res), buy_hold=stats(bh),
                equity=res.equity[::step], bh_equity=bh.equity[::step],
                times=[c.open_time for c in c_base][::step])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Daily Close Comparison strategy (repaint-aware) backtest")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="15m", choices=list(INTERVAL_MS), help="base/refresh timeframe")
    ap.add_argument("--mult", type=int, default=96, help="alt = base × mult (15m×96 = 1d)")
    ap.add_argument("--alt", choices=list(INTERVAL_MS), help="comparison TF; with --refresh derives interval/mult")
    ap.add_argument("--refresh", choices=list(INTERVAL_MS), help="realtime sampling period (base TF)")
    ap.add_argument("--no-res", action="store_true")
    ap.add_argument("--threshold-pct", type=float, default=0.0, help="deadband half-width in %% (0.1 = 0.1%%)")
    ap.add_argument("--trade-type", default="BOTH", choices=["LONG", "SHORT", "BOTH"])
    ap.add_argument("--sl-pct", type=float, default=0.0)
    ap.add_argument("--tp-pct", type=float, default=0.0)
    ap.add_argument("--mode", default="nonrepaint", choices=["nonrepaint", "realtime", "lookahead"])
    ap.add_argument("--entry-filter", action="store_true",
                    help="skip entries worse than the signal's first-paint price (repaint guard)")
    ap.add_argument("--filter-tol-bps", type=float, default=0.0)
    ap.add_argument("--compare-modes", action="store_true")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--start", help="YYYY-MM-DD (UTC); overrides --days")
    ap.add_argument("--end", help="YYYY-MM-DD (UTC)")
    ap.add_argument("--market", choices=["futures", "spot"], default="futures")
    ap.add_argument("--fee-bps", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=float, default=1.0)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

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
    print(f"# {len(c)} × {args.interval} bars. compare-TF={alt}, "
          f"threshold={args.threshold_pct}%, fee={args.fee_bps}bps/side.\n")

    fee, slip = args.fee_bps / 10000.0, args.slippage_bps / 10000.0
    thr = args.threshold_pct / 100.0
    results = [buy_hold(c)]

    def mkp(mode):
        return DCCParams(threshold=thr, mult=args.mult, use_res=not args.no_res,
                         trade_type=args.trade_type, sl_pct=args.sl_pct, tp_pct=args.tp_pct,
                         mode=mode, entry_filter=args.entry_filter, filter_tol_bps=args.filter_tol_bps)

    if args.compare_modes:
        for mode in ("lookahead", "realtime", "nonrepaint"):
            results.append(backtest_dcc(c, mkp(mode), fee, slip, name=f"DCC/{mode}"))
        tail = ("\nlookahead = TV repaint (today's FINAL daily close used early = future). "
                "NOT achievable.\nrealtime  = forming daily close re-checked each refresh bar "
                "(repaint-aware, look-ahead-free → achievable).\nnonrepaint= only settled daily "
                "closes (acts at day close). Trust realtime as the honest live expectation.")
    else:
        results.append(backtest_dcc(c, mkp(args.mode), fee, slip, name=f"DCC/{args.mode}"))
        tail = (f"\nMode={args.mode}. realtime = repaint-aware but look-ahead-free. "
                "Compare vs Buy&Hold; pick by Sharpe + shallow MDD.")

    print(format_table(results))
    print(tail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
