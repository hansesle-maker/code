#!/usr/bin/env python3
"""Correlation & beta of every Binance USDT-M perpetual futures symbol vs BTC.

For each perpetual contract on Binance USDT-M Futures, this fetches kline
history from the public Binance Futures API, aligns it with BTCUSDT over a
chosen period (from a start date to now), and computes Pearson correlation,
beta and R^2 of returns vs BTC. No API key is needed.

See analysis/corr_beta.py for the metric definitions.

Examples
--------
    # Daily returns from 2024-01-01 (KST) to now, all USDT-M perpetuals:
    python analysis/binance_futures_btc_corr_beta.py --from 2024-01-01

    # 4-hour returns, top 30 by correlation, write CSV:
    python analysis/binance_futures_btc_corr_beta.py --from 2025-01-01 \
        --interval 4h --top 30 --csv out.csv

    # Just a few symbols:
    python analysis/binance_futures_btc_corr_beta.py --from 2024-01-01 --symbols ETHUSDT,SOLUSDT

    # Offline math check (no network):
    python analysis/binance_futures_btc_corr_beta.py --self-test
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests

from corr_beta import (KST, Row, enable_utf8_stdout, make_row, parse_start,
                       print_table, report_skips, self_test, sort_rows, write_csv)

_BASE = "https://fapi.binance.com"
# Binance kline intervals.
_INTERVALS = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h",
              "12h", "1d", "3d", "1w", "1M"]
_KLIMIT = 1500  # max klines per request


def _get(url: str, retries: int = 4, timeout: int = 20):
    """GET a Binance JSON endpoint with retry/backoff; return parsed JSON."""
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=timeout, headers={"Accept": "application/json"})
            # Binance returns a JSON error object with a negative "code".
            if r.status_code == 200:
                return r.json()
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            raise RuntimeError(f"HTTP {r.status_code} {body.get('code', '')} {body.get('msg', '')} :: {url}")
        except Exception as exc:  # noqa: BLE001 - report and back off
            last = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 0.5)
    raise RuntimeError(f"request failed after {retries} tries: {url} :: {last}")


def fetch_perp_symbols(quote: str) -> List[str]:
    """Return every TRADING perpetual symbol with the given quote asset."""
    info = _get(f"{_BASE}/fapi/v1/exchangeInfo")
    out: List[str] = []
    for s in info.get("symbols", []):
        if (s.get("status") == "TRADING"
                and s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == quote):
            out.append(s["symbol"])
    return sorted(out)


def fetch_closes(symbol: str, interval: str, start_ms: int, delay: float = 0.0) -> Dict[int, float]:
    """Return {open_time_ms: close_price} from start_ms to now, paginating."""
    out: Dict[int, float] = {}
    now = int(time.time() * 1000)
    cur = start_ms
    while cur < now:
        url = (f"{_BASE}/fapi/v1/klines?symbol={symbol}&interval={interval}"
               f"&startTime={cur}&limit={_KLIMIT}")
        data = _get(url)
        if not data:
            break
        for k in data:
            # kline: [openTime, open, high, low, close, volume, closeTime, ...]
            try:
                out[int(k[0])] = float(k[4])
            except (ValueError, IndexError, TypeError):
                continue
        if len(data) < _KLIMIT:
            break
        cur = int(data[-1][6]) + 1  # advance past last closeTime
        if delay:
            time.sleep(delay)
    return out


def run(args: argparse.Namespace) -> int:
    start_ms = parse_start(args.start)
    bench = args.benchmark.upper()
    print(f"# Benchmark: {bench}   Interval: {args.interval}   "
          f"From: {datetime.fromtimestamp(start_ms/1000, KST):%Y-%m-%d %H:%M} KST",
          file=sys.stderr)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = fetch_perp_symbols(args.quote.upper())
        print(f"# {len(symbols)} USDT-M perpetual symbols ({args.quote.upper()})", file=sys.stderr)

    benchmark = fetch_closes(bench, args.interval, start_ms, args.delay)
    if not benchmark:
        print(f"error: no kline data for benchmark {bench}", file=sys.stderr)
        return 2

    rows: List[Row] = []
    skipped: List[str] = []
    for i, sym in enumerate(symbols, 1):
        if sym == bench:
            continue
        try:
            alt = fetch_closes(sym, args.interval, start_ms, args.delay)
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

    print_table(rows, "LAST(USDT)")
    report_skips(skipped, len(benchmark), args.min_points, len(rows))
    if args.csv:
        write_csv(args.csv, rows, ["benchmark", bench, "interval", args.interval, "from", args.start])
        print(f"# wrote {args.csv}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="start", default="2024-01-01",
                   help="start date, KST: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (default 2024-01-01)")
    p.add_argument("--interval", default="1d", choices=_INTERVALS,
                   help="kline interval (default 1d = daily)")
    p.add_argument("--benchmark", default="BTCUSDT", help="benchmark symbol (default BTCUSDT)")
    p.add_argument("--quote", default="USDT", help="quote asset to scan (default USDT)")
    p.add_argument("--symbols", default="",
                   help="comma-separated symbols to limit to (default: all perpetuals)")
    p.add_argument("--min-points", type=int, default=20,
                   help="skip symbols with fewer than N paired returns (default 20)")
    p.add_argument("--top", type=int, default=0, help="show only the top N by correlation (0 = all)")
    p.add_argument("--delay", type=float, default=0.05,
                   help="seconds to sleep between requests (default 0.05)")
    p.add_argument("--csv", default="", help="also write results to this CSV path")
    p.add_argument("--self-test", action="store_true", help="run offline math checks and exit")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    enable_utf8_stdout()
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
