#!/usr/bin/env python3
"""Backtest CLI for the TSI signal strategy.

Examples
--------
    # Offline demo: compare Confirmed/Aggressive x gate on synthetic data
    python backtest.py --demo

    # Live, single configuration (needs Binance access):
    python backtest.py --symbols config/symbols.csv

    # Live, compare the four variants on your watch-list:
    python backtest.py --symbols config/symbols.csv --compare
"""
from __future__ import annotations

import argparse
import math
import sys
from functools import partial
from typing import Dict, List, Tuple

from tsi_signal import SignalParams, fetch_klines, load_symbols, synthetic_candles
from tsi_signal.backtest import (
    BacktestParams,
    BacktestResult,
    backtest_symbol,
    combine_portfolio,
    format_results,
)
from tsi_signal.data import MARKETS


# --------------------------------------------------------------------------- #
# synthetic multi-regime data for the offline demo
# --------------------------------------------------------------------------- #
def _closes(drifts: List[float], start: float = 100.0, ripple: float = 0.006, period: float = 23.0):
    c = [start]
    for d in drifts:
        c.append(c[-1] * (1.0 + d))
    return [v * (1.0 + ripple * math.sin(2.0 * math.pi * i / period)) for i, v in enumerate(c)]


def _blocks(spec: List[Tuple[float, int]]) -> List[float]:
    out: List[float] = []
    for drift, n in spec:
        out += [drift] * n
    return out


def _chop(amp: float, n: int) -> List[float]:
    return [amp if i % 2 == 0 else -amp for i in range(n)]


def _to_1h(drifts_4h: List[float]) -> List[float]:
    out: List[float] = []
    for d in drifts_4h:
        out += [d / 4.0] * 4
    return out


def _demo_fetch():
    # symbol: strong trends; BTC: milder (so the rolling RS gate varies).
    sym4 = _blocks([(0.004, 200), (-0.004, 200), (0.003, 200)]) + _chop(0.006, 200)
    btc4 = _blocks([(0.002, 200), (-0.002, 200), (0.0015, 200)]) + _chop(0.003, 200)
    candles = {
        ("ALTUSDT", "4h"): synthetic_candles(_closes(sym4), "4h"),
        ("ALTUSDT", "1h"): synthetic_candles(_closes(_to_1h(sym4), ripple=0.003, period=51), "1h"),
        ("BTCUSDT", "4h"): synthetic_candles(_closes(btc4), "4h"),
        ("BTCUSDT", "1h"): synthetic_candles(_closes(_to_1h(btc4), ripple=0.003, period=51), "1h"),
    }

    def fetch(symbol: str, interval: str, limit: int):
        return candles[(symbol, interval)]

    return fetch


_VARIANTS = [
    ("confirmed+gate", dict(), dict(rs_gate="rolling")),
    ("confirmed+nogate", dict(), dict(rs_gate="off")),
    ("aggressive+gate", dict(require_zero_4h=False), dict(rs_gate="rolling")),
    ("aggressive+nogate", dict(require_zero_4h=False), dict(rs_gate="off")),
]


def _variant_params(sig_over: dict, bt_over: dict, args) -> Tuple[SignalParams, BacktestParams]:
    sig = SignalParams(benchmark=args.benchmark, require_zero_1h=args.require_zero_1h, **sig_over)
    bt = BacktestParams(fee_rate=args.fee_bps / 10000.0, rs_lookback=args.rs_lookback, **bt_over)
    return sig, bt


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest the TSI signal strategy")
    ap.add_argument("--symbols", help="watch-list CSV (symbol column)")
    ap.add_argument("--demo", action="store_true", help="offline synthetic multi-regime demo")
    ap.add_argument("--compare", action="store_true", help="compare the four variants")
    ap.add_argument("--market", choices=["spot", "futures"], default="futures")
    ap.add_argument("--benchmark", default="BTCUSDT")
    ap.add_argument("--limit", type=int, default=1500, help="klines per request (history length)")
    ap.add_argument("--aggressive", action="store_true", help="single run: 4h state +1 also qualifies")
    ap.add_argument("--no-gate", action="store_true", help="single run: disable the RS gate")
    ap.add_argument("--require-zero-1h", action="store_true")
    ap.add_argument("--fee-bps", type=float, default=5.0, help="cost per trade in basis points")
    ap.add_argument("--rs-lookback", type=int, default=30, help="rolling RS lookback (4h bars)")
    ap.add_argument("--equity-csv", help="write the portfolio equity curve to this CSV")
    args = ap.parse_args(argv)

    # --- assemble the data source -----------------------------------------
    if args.demo:
        symbols = ["ALTUSDT"]
        fetch = _demo_fetch()
        benchmark = "BTCUSDT"
    else:
        if not args.symbols:
            ap.error("--symbols is required unless --demo is given")
        symbols = [s.symbol for s in load_symbols(args.symbols)]
        base_url, path = MARKETS[args.market]
        fetch = partial(fetch_klines, base_url=base_url, path=path)
        benchmark = args.benchmark

    # fetch every series once, then run variants offline against the cache
    try:
        cache: Dict[Tuple[str, str], list] = {}

        def cached(symbol: str, interval: str, limit: int):
            key = (symbol, interval)
            if key not in cache:
                cache[key] = fetch(symbol, interval, limit)
            return cache[key]

        for sym in [benchmark, *symbols]:
            cached(sym, "4h", args.limit)
            cached(sym, "1h", args.limit)
    except Exception as exc:
        host = MARKETS[args.market][0] if not args.demo else "(demo)"
        print(f"ERROR: failed to fetch market data: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"Hint: {host} may be blocked by the network/region. Run locally.", file=sys.stderr)
        return 1

    bench_by_time = {c.open_time: c.close for c in cached(benchmark, "4h", args.limit)}

    def run_variant(label: str, sig: SignalParams, bt: BacktestParams):
        results = [
            backtest_symbol(sym, cached(sym, "4h", args.limit), cached(sym, "1h", args.limit),
                            bench_by_time, sig, bt, label=label)
            for sym in symbols
        ]
        return results, combine_portfolio(results, bt, label=label)

    # --- run --------------------------------------------------------------
    print(f"# Backtest  (benchmark={benchmark}, {'DEMO' if args.demo else args.market}, "
          f"fee={args.fee_bps}bps)\n")

    if args.compare or args.demo:
        rows: List[BacktestResult] = []
        for label, sig_over, bt_over in _VARIANTS:
            sig, bt = _variant_params(sig_over, bt_over, args)
            results, portfolio = run_variant(label, sig, bt)
            # demo has one symbol -> show that symbol; live -> show portfolio
            rows.append(results[0] if args.demo else portfolio)
        scope = symbols[0] if args.demo else "PORTFOLIO (equal-weight)"
        print(format_results(rows, f"Variant comparison — {scope}"))
        print("\nRET=total return, CAGR=annualised, MDD=max drawdown, B&H=buy&hold,")
        print("EXP=avg |exposure|, WIN=winning trades. Pick by Sharpe + MDD, not RET alone.")
        if args.demo:
            print("NOTE: synthetic idealised data — Sharpe/CAGR here are unrealistically high. "
                  "Use it to read the engine and compare variants, not as live expectations.")
    else:
        sig_over = dict(require_zero_4h=False) if args.aggressive else dict()
        bt_over = dict(rs_gate="off") if args.no_gate else dict(rs_gate="rolling")
        label = ("aggressive" if args.aggressive else "confirmed") + ("+nogate" if args.no_gate else "+gate")
        sig, bt = _variant_params(sig_over, bt_over, args)
        results, portfolio = run_variant(label, sig, bt)
        print(format_results([*results, portfolio]))
        if args.equity_csv:
            with open(args.equity_csv, "w", encoding="utf-8") as fh:
                fh.write("open_time,equity\n")
                for t, e in zip(portfolio.times, portfolio.equity[1:]):
                    fh.write(f"{t},{e:.6f}\n")
            print(f"\nWrote portfolio equity ({len(portfolio.times)} pts) to {args.equity_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
