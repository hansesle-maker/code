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
from tsi_signal.data import (
    FUTURES_BASE_URL,
    INTERVAL_MS,
    SPOT_BASE_URL,
    SYNTH_START_MS,
)

# A shared, manual RS start time for the demo (bar 195 of the synthetic series).
_DEMO_REF_MS = SYNTH_START_MS + 195 * INTERVAL_MS["4h"]


def _demo_symbols() -> List[SymbolConfig]:
    r = _DEMO_REF_MS
    return [
        SymbolConfig("BTCUSDT", "crypto", current_position=0.0, ref_time_ms=r),     # benchmark
        SymbolConfig("ETHUSDT", "crypto", current_position=0.0, ref_time_ms=r),     # -> ENTER_LONG
        SymbolConfig("SOLUSDT", "crypto", current_position=500.0, ref_time_ms=r),   # -> SWITCH_TO_SHORT
        SymbolConfig("XRPUSDT", "crypto", current_position=0.0, ref_time_ms=r),     # strong vs BTC, 1h<0 -> HOLD
        SymbolConfig("ADAUSDT", "crypto", current_position=-1000.0, ref_time_ms=r), # weak vs BTC, 4h up -> EXIT
    ]


def _demo_series(tail_slope: float, n_base: int = 200, n_tail: int = 20) -> List[float]:
    """A shared rippling base (so swing extremes and time alignment exist)
    plus a short directional tail. The short tail sets the TSI direction
    without saturating it at +-100 (a pure one-way trend would)."""
    base = trend_closes(n_base, start=100.0, drift=0.0, ripple=0.02)
    tail = [base[-1] * (1.0 + tail_slope * i) for i in range(1, n_tail + 1)]
    return base + tail


def _demo_fetch(symbol: str, interval: str, limit: int) -> List[Candle]:
    """Deterministic synthetic candles per symbol so the demo is reproducible.

    Illustrates the gate x trigger interaction:
      ETH  strong vs BTC + 4h up + 1h>=0   -> LONG  (ENTER_LONG)
      SOL  weak   vs BTC + 4h down + 1h<=0  -> SHORT (SWITCH_TO_SHORT)
      XRP  strong vs BTC but 1h TSI < 0     -> FLAT  (HOLD)
      ADA  weak   vs BTC but 4h TSI up      -> FLAT  (EXIT)
    BTC is the benchmark (gate skipped; decided by its own TSI).
    """
    tail_4h = {
        "BTCUSDT": 0.0015,   # benchmark, gentle up
        "ETHUSDT": 0.0040,   # outperforms BTC, 4h up   -> LONG
        "SOLUSDT": -0.0040,  # underperforms, 4h down    -> SHORT
        "XRPUSDT": 0.0040,   # outperforms BTC, 4h up ...
        "ADAUSDT": 0.0010,   # rises slower than BTC (weak), 4h up ...
    }
    tail_1h = {
        "XRPUSDT": -0.0040,  # ... but 1h TSI < 0 -> long trigger fails -> FLAT
    }
    slope = tail_1h[symbol] if (interval == "1h" and symbol in tail_1h) else tail_4h.get(symbol, 0.0)
    return synthetic_candles(_demo_series(slope), interval=interval)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TSI long/short signal engine")
    parser.add_argument("--symbols", help="CSV with symbol,asset_class,current_position[,target_notional]")
    parser.add_argument("--out", help="write the to-be table to this CSV path")
    parser.add_argument("--demo", action="store_true", help="run offline with synthetic data")
    parser.add_argument("--market", choices=["spot", "futures"], default="spot")
    parser.add_argument("--benchmark", default="BTCUSDT")
    parser.add_argument("--default-notional", type=float, default=1000.0)
    parser.add_argument("--limit", type=int, default=1000,
                        help="klines per request (warm-up + RS start-time window)")
    parser.add_argument("--require-reversal", action="store_true",
                        help="4h TSI must TURN (down->up / up->down) this bar, not merely slope")
    parser.add_argument("--require-ref", action="store_true",
                        help="block signals for symbols that have no manual RS start time")
    args = parser.parse_args(argv)

    params = SignalParams(
        benchmark=args.benchmark,
        require_reversal=args.require_reversal,
        require_ref=args.require_ref,
    )

    if args.demo:
        symbols = _demo_symbols()
        fetch = _demo_fetch
    else:
        if not args.symbols:
            parser.error("--symbols is required unless --demo is given")
        symbols = load_symbols(args.symbols)
        base_url = FUTURES_BASE_URL if args.market == "futures" else SPOT_BASE_URL
        fetch = partial(fetch_klines, base_url=base_url)

    try:
        rows = run_engine(symbols, fetch, params, default_notional=args.default_notional,
                          kline_limit=args.limit)
    except Exception as exc:  # benchmark fetch / data source failure
        print(f"ERROR: failed to fetch market data: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Hint: api.binance.com may be blocked by the network allowlist. "
              "Run locally or allowlist the host (fapi.binance.com for --market futures).",
              file=sys.stderr)
        return 1

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"# TSI signal run @ {stamp}  (benchmark={params.benchmark}, "
          f"{'DEMO' if args.demo else args.market})\n")
    print(format_table(rows))
    print("\nGATE   = direction allowed by BTC-relative strength (long/short/both).")
    print("ACTION = what to do on the exchange: ENTER_*/SWITCH_*/ADD/REDUCE/EXIT/HOLD.")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(to_csv(rows))
        print(f"\nWrote {len(rows)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
