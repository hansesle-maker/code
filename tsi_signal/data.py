"""Market data: a typed candle, a Binance kline fetcher, and a synthetic
generator used for offline demos and tests.

Only :func:`fetch_klines` needs the network (and the ``requests`` package).
Everything else is pure Python so the engine's logic can be exercised
without reaching an exchange.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

SPOT_BASE_URL = "https://api.binance.com"
FUTURES_BASE_URL = "https://fapi.binance.com"

# Default epoch (ms) for synthetic candles, so generated series line up in time.
SYNTH_START_MS = 1_700_000_000_000

# Binance interval -> milliseconds, for spacing synthetic candles.
INTERVAL_MS: Dict[str, int] = {
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}


@dataclass(frozen=True)
class Candle:
    open_time: int  # epoch milliseconds of the bar's open
    open: float
    high: float
    low: float
    close: float
    volume: float


# A fetcher takes (symbol, interval, limit) and returns oldest-first candles.
Fetcher = Callable[[str, str, int], List[Candle]]


def fetch_klines(
    symbol: str,
    interval: str,
    limit: int = 500,
    base_url: str = SPOT_BASE_URL,
    drop_unclosed: bool = True,
    session: Optional[object] = None,
) -> List[Candle]:
    """Fetch OHLCV klines from Binance's public REST API (no auth required).

    The most recent kline returned by Binance is the still-forming candle;
    with ``drop_unclosed=True`` it is removed so signals are evaluated on
    closed bars only (matching a "on bar close" reading of the chart).

    Requires network access to ``base_url``. In this sandbox that host is
    blocked by the egress allowlist; run locally or allowlist the host.
    """
    import requests  # imported lazily so offline use needs no dependency

    url = f"{base_url}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    http = session or requests
    resp = http.get(url, params=params, timeout=15)
    resp.raise_for_status()
    raw = resp.json()

    candles = [
        Candle(
            open_time=int(k[0]),
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
            volume=float(k[5]),
        )
        for k in raw
    ]
    if drop_unclosed and candles:
        candles = candles[:-1]
    return candles


def synthetic_candles(
    closes: List[float],
    interval: str = "4h",
    start_time: int = SYNTH_START_MS,
    wick: float = 0.002,
) -> List[Candle]:
    """Build candles from a list of closes (oldest first).

    High/low are derived from neighbouring closes plus a small ``wick`` so
    that swing-pivot detection has something to work with. Bars are spaced
    by ``interval`` starting at ``start_time`` so symbols generated with the
    same arguments line up in time (needed for relative-strength alignment).
    """
    step = INTERVAL_MS.get(interval, INTERVAL_MS["4h"])
    candles: List[Candle] = []
    prev = closes[0]
    for i, c in enumerate(closes):
        hi = max(prev, c) * (1.0 + wick)
        lo = min(prev, c) * (1.0 - wick)
        candles.append(
            Candle(
                open_time=start_time + i * step,
                open=prev,
                high=hi,
                low=lo,
                close=c,
                volume=1.0,
            )
        )
        prev = c
    return candles


def trend_closes(
    n: int,
    start: float = 100.0,
    drift: float = 0.0,
    ripple: float = 0.0,
    ripple_period: float = 17.0,
) -> List[float]:
    """Deterministic close series: linear ``drift`` per bar plus an optional
    sine ``ripple`` (so local swing pivots appear). No randomness, so demos
    and tests are reproducible."""
    out: List[float] = []
    for i in range(n):
        value = start * (1.0 + drift * i)
        if ripple:
            value += start * ripple * math.sin(2.0 * math.pi * i / ripple_period)
        out.append(value)
    return out
