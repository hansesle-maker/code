#!/usr/bin/env python3
"""CLI entry point for the TSI signal engine — the thing you run every 4h.

Examples
--------
    # Offline demo (no network, synthetic data) to see the output format:
    python run.py --demo

    # Live run against Binance (needs network access to api.binance.com):
    python run.py --symbols config/symbols.csv --out signals.csv

    # Use Binance USDⓈ-M futures klines instead of spot:
    python run.py --symbols config/symbols.csv --market futures
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from functools import partial
from typing import List

from tsi_signal import (
    Candle,
    SignalParams,
    SymbolConfig,
    fetch_klines,
    format_table,
    load_symbols,
    run_engine,
    synthetic_candles,
    to_csv,
    trend_closes,
)
from tsi_signal.data import INTERVAL_MS, MARKETS, SYNTH_START_MS

# A shared, manual RS start time for the demo (bar 100 of the synthetic series).
_DEMO_REF_MS = SYNTH_START_MS + 100 * INTERVAL_MS["4h"]


def _demo_symbols() -> List[SymbolConfig]:
    r = _DEMO_REF_MS
    return [
        SymbolConfig("BTCUSDT", "crypto", current_position=0.0, ref_time_ms=r),     # benchmark -> LONG
        SymbolConfig("ETHUSDT", "crypto", current_position=0.0, ref_time_ms=r),     # +4 -> LONG 100%
        SymbolConfig("LINKUSDT", "crypto", current_position=0.0, ref_time_ms=r),    # +3 -> LONG 60%
        SymbolConfig("SOLUSDT", "crypto", current_position=500.0, ref_time_ms=r),   # -4 -> SWITCH_TO_SHORT
        SymbolConfig("XRPUSDT", "crypto", current_position=0.0, ref_time_ms=r),     # 1h rolled over -> HOLD
        SymbolConfig("ADAUSDT", "crypto", current_position=-1000.0, ref_time_ms=r), # 4h not confirmed -> EXIT
    ]


def _demo_series(regime: float, recent: float, n_regime: int = 200, n_recent: int = 14) -> List[float]:
    """Two-phase path: a long ``regime`` leg sets the TSI's side of ZERO, a
    short ``recent`` leg sets the TSI's side of its SIGNAL line. Together they
    place the TSI in a chosen +2/+1/-1/-2 state (deterministic, reproducible)."""
    base = trend_closes(n_regime, start=100.0, drift=regime, ripple=0.004)
    tail = [base[-1] * (1.0 + recent * i) for i in range(1, n_recent + 1)]
    return base + tail


def _demo_fetch(symbol: str, interval: str, limit: int) -> List[Candle]:
    """Deterministic synthetic candles showing the TSI-state model + sizing:

      ETH  4h +2 & 1h +2  -> conv +4 -> LONG 100%  (ENTER_LONG)
      LINK 4h +2 & 1h +1  -> conv +3 -> LONG  60%  (ENTER_LONG, partial)
      SOL  4h -2 & 1h -2  -> conv -4 -> SHORT 100% (SWITCH_TO_SHORT)
      XRP  4h +2 & 1h -1  -> 1h rolled over        -> FLAT (HOLD)
      ADA  4h +1 (below zero, not confirmed)        -> FLAT (EXIT)
    BTC is the benchmark (gate skipped). (regime, recent) slopes per state:
    +2 (+,+)  +1 (-,+)  -1 (+,-)  -2 (-,-).
    """
    # 4h leg per symbol; magnitudes also set BTC-relative strength.
    p4 = {
        "BTCUSDT": (0.0010, 0.003),   # benchmark, +2
        "ETHUSDT": (0.0018, 0.004),   # stronger than BTC, +2
        "LINKUSDT": (0.0018, 0.004),  # stronger than BTC, +2
        "SOLUSDT": (-0.0018, -0.004),  # weaker than BTC, -2
        "XRPUSDT": (0.0018, 0.004),   # stronger than BTC, +2
        "ADAUSDT": (-0.0040, 0.004),  # weaker than BTC, +1 (still below zero)
    }
    # 1h leg overrides (where it should differ from the 4h pattern).
    p1 = {
        "LINKUSDT": (-0.0040, 0.004),  # 1h +1 (below zero but turning up)
        "XRPUSDT": (0.0040, -0.002),   # 1h -1 (above zero but rolling over)
    }
    regime, recent = (p1.get(symbol) if interval == "1h" else None) or p4.get(symbol, (0.0, 0.0))
    return synthetic_candles(_demo_series(regime, recent), interval=interval)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TSI long/short signal engine")
    parser.add_argument("--symbols", help="CSV with symbol,asset_class,current_position[,target_notional]")
    parser.add_argument("--out", help="write the to-be table to this CSV path")
    parser.add_argument("--demo", action="store_true", help="run offline with synthetic data")
    parser.add_argument("--market", choices=["spot", "futures"], default="futures",
                        help="Binance market for klines (default: futures)")
    parser.add_argument("--benchmark", default="BTCUSDT")
    parser.add_argument("--default-notional", type=float, default=1000.0)
    parser.add_argument("--limit", type=int, default=1000,
                        help="klines per request (warm-up + RS start-time window)")
    parser.add_argument("--aggressive", action="store_true",
                        help="enter on 4h TSI>signal even below zero (state +1), not only confirmed +2")
    parser.add_argument("--hysteresis", action="store_true",
                        help="hold an existing position through weak states; exit only on reversal/gate flip")
    parser.add_argument("--long-only", action="store_true", help="never take short positions")
    parser.add_argument("--short-only", action="store_true", help="never take long positions")
    parser.add_argument("--require-zero-1h", action="store_true",
                        help="1h must also be on the correct side of zero (stricter timing)")
    parser.add_argument("--require-ref", action="store_true",
                        help="block signals for symbols that have no manual RS start time")
    args = parser.parse_args(argv)

    params = SignalParams(
        benchmark=args.benchmark,
        require_zero_4h=not args.aggressive,
        require_zero_1h=args.require_zero_1h,
        hysteresis=args.hysteresis,
        long_only=args.long_only,
        short_only=args.short_only,
        require_ref=args.require_ref,
    )

    if args.demo:
        symbols = _demo_symbols()
        fetch = _demo_fetch
    else:
        if not args.symbols:
            parser.error("--symbols is required unless --demo is given")
        symbols = load_symbols(args.symbols)
        base_url, path = MARKETS[args.market]
        fetch = partial(fetch_klines, base_url=base_url, path=path)

    try:
        rows = run_engine(symbols, fetch, params, default_notional=args.default_notional,
                          kline_limit=args.limit)
    except Exception as exc:  # benchmark fetch / data source failure
        host = MARKETS[args.market][0]
        print(f"ERROR: failed to fetch market data: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"Hint: {host} may be blocked by the network/region. Run locally, "
              "or check Binance access for your region/host.", file=sys.stderr)
        return 1

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"# TSI signal run @ {stamp}  (benchmark={params.benchmark}, "
          f"{'DEMO' if args.demo else args.market})\n")
    print(format_table(rows))
    print("\nGATE = direction allowed by BTC-relative strength. St4h/St1h = TSI state")
    print("(+2 up & >0, +1 up & <0, -1 down & >0, -2 down & <0). CONV = St4h+St1h -> SIZE%.")
    print("ACTION = exchange action: ENTER_*/SWITCH_*/ADD/REDUCE/EXIT/HOLD.")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(to_csv(rows))
        print(f"\nWrote {len(rows)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
