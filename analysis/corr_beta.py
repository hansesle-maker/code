#!/usr/bin/env python3
"""Shared correlation & beta math for the Bithumb / Binance analysis scripts.

Kept dependency-free (standard library only) so the metric logic can be unit
tested offline, independent of any exchange API.

Definitions
-----------
    corr : Pearson correlation of a coin's returns vs the benchmark's returns
    beta : sensitivity to the benchmark = cov(coin, bench) / var(bench)
           (identically corr * stdev(coin) / stdev(bench))
    r2   : correlation squared (share of the coin's variance explained)
"""
from __future__ import annotations

import argparse
import csv
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

KST = timezone(timedelta(hours=9))


def parse_start(s: str) -> int:
    """Parse 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (KST) into epoch milliseconds."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=KST)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"bad date: {s!r} (use YYYY-MM-DD)")


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
    """Return (pearson_corr, beta, r2) of y (coin) against x (benchmark).

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


def aligned_returns(bench: Dict[int, float], alt: Dict[int, float],
                    start_ms: int) -> Tuple[List[float], List[float], int]:
    """Align benchmark and alt closes on shared timestamps >= start_ms, and
    return their return series plus the number of paired return observations."""
    common = sorted(t for t in bench.keys() & alt.keys() if t >= start_ms)
    if len(common) < 2:
        return [], [], 0
    b_closes = [bench[t] for t in common]
    a_closes = [alt[t] for t in common]
    return returns_from_closes(b_closes), returns_from_closes(a_closes), len(common) - 1


class Row:
    """One coin's result line."""
    __slots__ = ("symbol", "n", "corr", "beta", "r2", "last")

    def __init__(self, symbol, n, corr, beta, r2, last):
        self.symbol, self.n, self.corr, self.beta, self.r2, self.last = \
            symbol, n, corr, beta, r2, last


def make_row(symbol: str, bench: Dict[int, float], alt: Dict[int, float],
             start_ms: int, min_points: int) -> Tuple[Optional[Row], int]:
    """Compute a Row for one coin, or (None, n) if it has too few observations."""
    xret, yret, n = aligned_returns(bench, alt, start_ms)
    if n < min_points:
        return None, n
    corr, beta, r2 = corr_beta(xret, yret)
    last = alt[max(alt)] if alt else float("nan")
    return Row(symbol, n, corr, beta, r2, last), n


def sort_rows(rows: List[Row]) -> None:
    """Sort in place by correlation descending (NaNs last)."""
    rows.sort(key=lambda r: (math.isnan(r.corr), -r.corr if not math.isnan(r.corr) else 0.0))


def enable_utf8_stdout() -> None:
    """Best-effort switch stdout/stderr to UTF-8 so non-ASCII coin names don't
    crash printing on Windows cp949 consoles."""
    import sys as _sys
    for stream in (_sys.stdout, _sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - older Pythons / non-reconfigurable
            pass


def print_table(rows: List[Row], price_label: str = "LAST") -> None:
    print(f"\n{'COIN':<12}{'N':>7}{'CORR':>9}{'BETA':>9}{'R^2':>8}   {price_label:>16}")
    print("-" * 69)
    for r in rows:
        print(f"{r.symbol:<12}{r.n:>7}{r.corr:>9.3f}{r.beta:>9.3f}{r.r2:>8.3f}"
              f"   {r.last:>16,.6g}")


def write_csv(path: str, rows: List[Row], meta: List) -> None:
    # utf-8-sig: BOM so Excel (incl. Korean Windows / cp949 locale) reads it as
    # UTF-8; without this, non-ASCII symbols crash the write mid-file on Windows.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(meta)
        w.writerow(["coin", "n", "corr", "beta", "r2", "last"])
        for r in rows:
            w.writerow([r.symbol, r.n, f"{r.corr:.6f}", f"{r.beta:.6f}",
                        f"{r.r2:.6f}", f"{r.last:.6f}"])


def report_skips(skipped: List[str], benchmark_count: int, min_points: int,
                 rows_count: int, out=None) -> None:
    """Print a grouped, actionable summary of why coins were skipped."""
    import sys as _sys
    out = out or _sys.stderr
    fetch_err = [s for s in skipped if "fetch error" in s]
    low_n = [s for s in skipped if "(n=" in s]
    print(f"\n# benchmark candles loaded: {benchmark_count}", file=out)
    print(f"# result: {rows_count} coins shown, {len(skipped)} skipped "
          f"({len(low_n)} too-few-points, {len(fetch_err)} fetch-error)", file=out)
    if rows_count == 0 and low_n:
        if benchmark_count < min_points:
            print(f"#   -> benchmark itself has only {benchmark_count} candles "
                  f"(< --min-points {min_points}). Use an earlier --from or a shorter --interval.",
                  file=out)
        else:
            print(f"#   -> every coin overlaps BTC by < {min_points} bars for this window. "
                  f"Try an earlier --from, or lower --min-points.", file=out)
    if fetch_err:
        print(f"#   -> fetch errors often mean rate-limiting or a blocked region; "
              f"raise --delay (e.g. --delay 0.3) or narrow --symbols.", file=out)
    if skipped:
        print(f"# skipped detail: {', '.join(skipped[:20])}"
              + (" ..." if len(skipped) > 20 else ""), file=out)


def self_test() -> int:
    """Offline validation of the metric math (no network)."""
    import random
    random.seed(7)
    # alt = 1.5 * bench + noise -> expect beta ~ 1.5, high positive corr
    bench = [random.gauss(0, 0.02) for _ in range(2000)]
    alt = [1.5 * b + random.gauss(0, 0.005) for b in bench]
    corr, beta, r2 = corr_beta(bench, alt)
    print(f"synthetic: corr={corr:.4f} beta={beta:.4f} r2={r2:.4f}")
    assert 1.4 < beta < 1.6, beta
    assert corr > 0.9, corr
    assert abs(r2 - corr * corr) < 1e-12

    # perfectly anti-correlated -> corr = -1, beta = -1
    x = [0.01, -0.02, 0.03, -0.01, 0.02]
    y = [-v for v in x]
    c, b, _ = corr_beta(x, y)
    assert abs(c + 1.0) < 1e-9 and abs(b + 1.0) < 1e-9, (c, b)

    # alignment: only shared timestamps >= start are used
    bench_m = {1000: 100.0, 2000: 110.0, 3000: 121.0, 4000: 133.1}
    alt_m = {2000: 50.0, 3000: 55.0, 4000: 60.5}  # listed later than bench
    xr, yr, n = aligned_returns(bench_m, alt_m, start_ms=1500)
    assert n == 2 and len(xr) == 2, (n, xr)  # ts 2000->3000, 3000->4000

    row, n = make_row("ALT", bench_m, alt_m, start_ms=1500, min_points=1)
    assert row is not None and row.n == 2 and abs(row.last - 60.5) < 1e-9
    print("self-test OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
