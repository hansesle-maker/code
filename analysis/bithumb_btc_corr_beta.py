#!/usr/bin/env python3
"""Correlation & beta of every Bithumb KRW-market coin vs BTC.

For each coin listed on the Bithumb KRW market, this fetches candlestick
history from the Bithumb public API, aligns it with BTC over a chosen period
(from a start date to now), and computes:

    corr  - Pearson correlation of daily (or chosen-interval) returns vs BTC
    beta  - sensitivity to BTC:  cov(coin, BTC) / var(BTC)
    r2    - correlation squared (share of variance explained by BTC)

No API key is needed - only public endpoints are used.

Examples
--------
    # Daily returns from 2024-01-01 (KST) to now, whole KRW market:
    python analysis/bithumb_btc_corr_beta.py --from 2024-01-01

    # 1-hour returns over the last stretch, write a CSV, only top 30 by corr:
    python analysis/bithumb_btc_corr_beta.py --from 2025-01-01 --interval 1h \
        --top 30 --csv out.csv

    # Just a few coins:
    python analysis/bithumb_btc_corr_beta.py --from 2024-01-01 --symbols ETH,XRP,SOL

    # Offline math check (no network):
    python analysis/bithumb_btc_corr_beta.py --self-test

Notes
-----
* Beta uses BTC as the market/benchmark, matching how equity beta uses the
  index. A coin with beta 1.5 has, on average, moved 1.5x BTC's return.
* Returns are simple percentage changes between consecutive *aligned* bars
  (timestamps present in both the coin and BTC series), so newly listed coins
  are compared only over their common history with BTC.
* Bithumb's candlestick endpoint returns the full available history for the
  interval; we filter to [--from, now].
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

_KST = timezone(timedelta(hours=9))
_BASE = "https://api.bithumb.com/public"
# Bithumb candlestick intervals (chart_intervals path segment).
_INTERVALS = {"1m", "3m", "5m", "10m", "30m", "1h", "6h", "12h", "24h"}


# --------------------------------------------------------------------------- #
# Bithumb public API
# --------------------------------------------------------------------------- #
def _get(url: str, retries: int = 4, timeout: int = 20) -> dict:
    """GET a Bithumb JSON endpoint with retry/backoff; return the parsed body."""
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=timeout,
                             headers={"Accept": "application/json"})
            r.raise_for_status()
            body = r.json()
            # Bithumb signals success with status == "0000".
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
    body = _get(f"{_BASE}/ticker/ALL_KRW")
    data = body.get("data", {})
    return sorted(k for k in data.keys() if k != "date")


def fetch_closes(symbol: str, interval: str) -> Dict[int, float]:
    """Return {timestamp_ms: close_price} for symbol_KRW at the given interval."""
    body = _get(f"{_BASE}/candlestick/{symbol}_KRW/{interval}")
    rows = body.get("data", []) or []
    out: Dict[int, float] = {}
    # Each row is [ts_ms, open, close, high, low, volume].
    for row in rows:
        try:
            out[int(row[0])] = float(row[2])
        except (ValueError, IndexError, TypeError):
            continue
    return out


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def returns_from_closes(closes: List[float]) -> List[float]:
    """Simple percentage returns between consecutive closes."""
    out: List[float] = []
    for prev, cur in zip(closes, closes[1:]):
        if prev and math.isfinite(prev) and math.isfinite(cur):
            out.append(cur / prev - 1.0)
        else:
            out.append(0.0)
    return out


def corr_beta(x: List[float], y: List[float]) -> Tuple[float, float, float]:
    """Return (pearson_corr, beta, r2) of y (coin) against x (BTC market).

    beta = cov(x, y) / var(x); corr = cov(x, y) / (std_x * std_y).
    """
    n = len(x)
    if n < 2:
        return float("nan"), float("nan"), float("nan")
    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((xi - mx) ** 2 for xi in x)
    syy = sum((yi - my) ** 2 for yi in y)
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    if sxx <= 0.0 or syy <= 0.0:
        return float("nan"), float("nan"), float("nan")
    corr = sxy / math.sqrt(sxx * syy)
    beta = sxy / sxx
    return corr, beta, corr * corr


def aligned_returns(btc: Dict[int, float], alt: Dict[int, float],
                    start_ms: int) -> Tuple[List[float], List[float], int]:
    """Align BTC and alt closes on shared timestamps >= start_ms, return their
    return series plus the number of paired return observations."""
    common = sorted(t for t in btc.keys() & alt.keys() if t >= start_ms)
    if len(common) < 2:
        return [], [], 0
    btc_closes = [btc[t] for t in common]
    alt_closes = [alt[t] for t in common]
    return returns_from_closes(btc_closes), returns_from_closes(alt_closes), len(common) - 1


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
class Row:
    __slots__ = ("symbol", "n", "corr", "beta", "r2", "last")

    def __init__(self, symbol, n, corr, beta, r2, last):
        self.symbol, self.n, self.corr, self.beta, self.r2, self.last = \
            symbol, n, corr, beta, r2, last


def parse_start(s: str) -> int:
    """Parse 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (KST) into epoch milliseconds."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=_KST)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"bad date: {s!r} (use YYYY-MM-DD)")


def run(args: argparse.Namespace) -> int:
    start_ms = parse_start(args.start)
    bench = args.benchmark.upper()
    print(f"# Benchmark: {bench}_KRW   Interval: {args.interval}   "
          f"From: {datetime.fromtimestamp(start_ms/1000, _KST):%Y-%m-%d %H:%M} KST",
          file=sys.stderr)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = fetch_krw_symbols()
        print(f"# {len(symbols)} KRW-market coins listed", file=sys.stderr)

    btc = fetch_closes(bench, args.interval)
    if not btc:
        print(f"error: no candlestick data for benchmark {bench}", file=sys.stderr)
        return 2

    rows: List[Row] = []
    skipped: List[str] = []
    for i, sym in enumerate(symbols, 1):
        if sym == bench:
            continue
        try:
            alt = fetch_closes(sym, args.interval)
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{sym} (fetch error)")
            continue
        xret, yret, n = aligned_returns(btc, alt, start_ms)
        if n < args.min_points:
            skipped.append(f"{sym} (n={n})")
        else:
            corr, beta, r2 = corr_beta(xret, yret)
            last = max(alt) and alt[max(alt)]
            rows.append(Row(sym, n, corr, beta, r2, last))
        if args.delay:
            time.sleep(args.delay)
        if i % 25 == 0:
            print(f"# ...{i}/{len(symbols)}", file=sys.stderr)

    rows.sort(key=lambda r: (math.isnan(r.corr), -r.corr if not math.isnan(r.corr) else 0))
    if args.top:
        rows = rows[:args.top]

    _print_table(rows, bench)
    if skipped:
        print(f"\n# skipped {len(skipped)}: {', '.join(skipped[:20])}"
              + (" ..." if len(skipped) > 20 else ""), file=sys.stderr)
    if args.csv:
        _write_csv(args.csv, rows, bench, args)
        print(f"# wrote {args.csv}", file=sys.stderr)
    return 0


def _print_table(rows: List[Row], bench: str) -> None:
    print(f"\n{'COIN':<8}{'N':>7}{'CORR':>9}{'BETA':>9}{'R^2':>8}   {'LAST(KRW)':>16}")
    print("-" * 65)
    for r in rows:
        print(f"{r.symbol:<8}{r.n:>7}{r.corr:>9.3f}{r.beta:>9.3f}{r.r2:>8.3f}"
              f"   {r.last:>16,.4g}")


def _write_csv(path: str, rows: List[Row], bench: str, args) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["benchmark", bench, "interval", args.interval, "from", args.start])
        w.writerow(["coin", "n", "corr", "beta", "r2", "last_krw"])
        for r in rows:
            w.writerow([r.symbol, r.n, f"{r.corr:.6f}", f"{r.beta:.6f}",
                        f"{r.r2:.6f}", f"{r.last:.6f}"])


# --------------------------------------------------------------------------- #
# Offline self-test (no network) - validates the corr/beta math.
# --------------------------------------------------------------------------- #
def self_test() -> int:
    import random
    random.seed(7)
    # Construct alt = 1.5 * btc + noise; expect beta ~ 1.5, positive corr.
    btc = [random.gauss(0, 0.02) for _ in range(2000)]
    alt = [1.5 * b + random.gauss(0, 0.005) for b in btc]
    corr, beta, r2 = corr_beta(btc, alt)
    print(f"synthetic: corr={corr:.4f} beta={beta:.4f} r2={r2:.4f}")
    assert 1.4 < beta < 1.6, beta
    assert corr > 0.9, corr
    assert abs(r2 - corr * corr) < 1e-12

    # Perfectly anti-correlated -> corr = -1, beta = -1.
    x = [0.01, -0.02, 0.03, -0.01, 0.02]
    y = [-v for v in x]
    c, b, _ = corr_beta(x, y)
    assert abs(c + 1.0) < 1e-9 and abs(b + 1.0) < 1e-9, (c, b)

    # Alignment: only shared timestamps >= start are used.
    btc_m = {1000: 100.0, 2000: 110.0, 3000: 121.0, 4000: 133.1}
    alt_m = {2000: 50.0, 3000: 55.0, 4000: 60.5}  # listed later than BTC
    xr, yr, n = aligned_returns(btc_m, alt_m, start_ms=1500)
    assert n == 2 and len(xr) == 2, (n, xr)  # ts 2000->3000, 3000->4000
    print("self-test OK")
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="start", default="2024-01-01",
                   help="start date, KST: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (default 2024-01-01)")
    p.add_argument("--interval", default="24h", choices=sorted(_INTERVALS),
                   help="candlestick interval (default 24h = daily)")
    p.add_argument("--benchmark", default="BTC", help="benchmark coin (default BTC)")
    p.add_argument("--symbols", default="",
                   help="comma-separated coins to limit to (default: whole KRW market)")
    p.add_argument("--min-points", type=int, default=20,
                   help="skip coins with fewer than N paired returns (default 20)")
    p.add_argument("--top", type=int, default=0,
                   help="show only the top N by correlation (0 = all)")
    p.add_argument("--delay", type=float, default=0.05,
                   help="seconds to sleep between requests, to respect rate limits (default 0.05)")
    p.add_argument("--csv", default="", help="also write results to this CSV path")
    p.add_argument("--self-test", action="store_true", help="run offline math checks and exit")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return self_test()
    if args.interval not in _INTERVALS:
        print(f"error: interval must be one of {sorted(_INTERVALS)}", file=sys.stderr)
        return 2
    try:
        return run(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
