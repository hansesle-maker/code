#!/usr/bin/env python3
"""Correlation & beta of every Bithumb KRW-market coin vs BTC.

For each coin listed on the Bithumb KRW market, this fetches candlestick
history from the Bithumb public API, aligns it with BTC over a chosen period
(from a start date to now), and computes Pearson correlation, beta and R^2 of
returns vs BTC. No API key is needed - only public endpoints are used.

See analysis/corr_beta.py for the metric definitions.

Examples
--------
    # Daily returns from 2024-01-01 (KST) to now, whole KRW market:
    python analysis/bithumb_btc_corr_beta.py --from 2024-01-01

    # 1-hour returns, write a CSV, only top 30 by correlation:
    python analysis/bithumb_btc_corr_beta.py --from 2025-01-01 --interval 1h \
        --top 30 --csv out.csv

    # Just a few coins:
    python analysis/bithumb_btc_corr_beta.py --from 2024-01-01 --symbols ETH,XRP,SOL

    # Offline math check (no network):
    python analysis/bithumb_btc_corr_beta.py --self-test
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests

from corr_beta import (KST, Row, make_row, parse_start, print_table, self_test,
                       sort_rows, write_csv)

_BASE = "https://api.bithumb.com/public"
# Bithumb candlestick intervals (chart_intervals path segment).
_INTERVALS = ["1m", "3m", "5m", "10m", "30m", "1h", "6h", "12h", "24h"]


def _get(url: str, retries: int = 4, timeout: int = 20) -> dict:
    """GET a Bithumb JSON endpoint with retry/backoff; return the parsed body."""
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=timeout, headers={"Accept": "application/json"})
            r.raise_for_status()
            body = r.json()
            if body.get("status") not in (None, "0000"):
                raise RuntimeError(f"Bithumb status {body.get('status')} for {url}")
            return body
        except Exception as exc:  # noqa: BLE001 - report and back off
            last = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 0.5)
    raise RuntimeError(f"request failed after {retries} tries: {url} :: {last}")


def fetch_krw_symbols() -> List[str]:
    """Return every coin symbol on the Bithumb KRW market (e.g. ['BTC','ETH',...])."""
    data = _get(f"{_BASE}/ticker/ALL_KRW").get("data", {})
    return sorted(k for k in data.keys() if k != "date")


def fetch_closes(symbol: str, interval: str) -> Dict[int, float]:
    """Return {timestamp_ms: close_price} for symbol_KRW at the given interval."""
    rows = _get(f"{_BASE}/candlestick/{symbol}_KRW/{interval}").get("data", []) or []
    out: Dict[int, float] = {}
    # Each row is [ts_ms, open, close, high, low, volume].
    for row in rows:
        try:
            out[int(row[0])] = float(row[2])
        except (ValueError, IndexError, TypeError):
            continue
    return out


def run(args: argparse.Namespace) -> int:
    start_ms = parse_start(args.start)
    bench = args.benchmark.upper()
    print(f"# Benchmark: {bench}_KRW   Interval: {args.interval}   "
          f"From: {datetime.fromtimestamp(start_ms/1000, KST):%Y-%m-%d %H:%M} KST",
          file=sys.stderr)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = fetch_krw_symbols()
        print(f"# {len(symbols)} KRW-market coins listed", file=sys.stderr)

    benchmark = fetch_closes(bench, args.interval)
    if not benchmark:
        print(f"error: no candlestick data for benchmark {bench}", file=sys.stderr)
        return 2

    rows: List[Row] = []
    skipped: List[str] = []
    for i, sym in enumerate(symbols, 1):
        if sym == bench:
            continue
        try:
            alt = fetch_closes(sym, args.interval)
        except Exception:  # noqa: BLE001
            skipped.append(f"{sym} (fetch error)")
            continue
        row, n = make_row(sym, benchmark, alt, start_ms, args.min_points)
        if row is None:
            skipped.append(f"{sym} (n={n})")
        else:
            rows.append(row)
        if args.delay:
            time.sleep(args.delay)
        if i % 25 == 0:
            print(f"# ...{i}/{len(symbols)}", file=sys.stderr)

    sort_rows(rows)
    if args.top:
        rows = rows[:args.top]

    print_table(rows, "LAST(KRW)")
    if skipped:
        print(f"\n# skipped {len(skipped)}: {', '.join(skipped[:20])}"
              + (" ..." if len(skipped) > 20 else ""), file=sys.stderr)
    if args.csv:
        write_csv(args.csv, rows, ["benchmark", bench, "interval", args.interval, "from", args.start])
        print(f"# wrote {args.csv}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="start", default="2024-01-01",
                   help="start date, KST: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (default 2024-01-01)")
    p.add_argument("--interval", default="24h", choices=_INTERVALS,
                   help="candlestick interval (default 24h = daily)")
    p.add_argument("--benchmark", default="BTC", help="benchmark coin (default BTC)")
    p.add_argument("--symbols", default="",
                   help="comma-separated coins to limit to (default: whole KRW market)")
    p.add_argument("--min-points", type=int, default=20,
                   help="skip coins with fewer than N paired returns (default 20)")
    p.add_argument("--top", type=int, default=0, help="show only the top N by correlation (0 = all)")
    p.add_argument("--delay", type=float, default=0.05,
                   help="seconds to sleep between requests (default 0.05)")
    p.add_argument("--csv", default="", help="also write results to this CSV path")
    p.add_argument("--self-test", action="store_true", help="run offline math checks and exit")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return self_test()
    try:
        return run(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
