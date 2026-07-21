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

# Klines REST paths differ between spot and USDⓈ-M futures (same row format).
SPOT_KLINES_PATH = "/api/v3/klines"
FUTURES_KLINES_PATH = "/fapi/v1/klines"

# market name -> (base_url, klines_path)
MARKETS = {
    "spot": (SPOT_BASE_URL, SPOT_KLINES_PATH),
    "futures": (FUTURES_BASE_URL, FUTURES_KLINES_PATH),
}

# Default epoch (ms) for synthetic candles, so generated series line up in time.
SYNTH_START_MS = 1_700_000_000_000

# Binance interval -> milliseconds, for spacing synthetic candles.
INTERVAL_MS: Dict[str, int] = {
    "15m": 15 * 60 * 1000,
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
    path: str = SPOT_KLINES_PATH,
    drop_unclosed: bool = True,
    session: Optional[object] = None,
) -> List[Candle]:
    """Fetch OHLCV klines from Binance's public REST API (no auth required).

    Works for spot (``base_url``/``path`` defaults) or USDⓈ-M futures
    (``FUTURES_BASE_URL`` + ``FUTURES_KLINES_PATH``); the row format is the
    same. See :data:`MARKETS` for the pairs.

    The most recent kline returned by Binance is the still-forming candle;
    with ``drop_unclosed=True`` it is removed so signals are evaluated on
    closed bars only (matching a "on bar close" reading of the chart).

    Requires network access to ``base_url``. In this sandbox that host is
    blocked by the egress allowlist; run locally or allowlist the host.
    """
    import requests  # imported lazily so offline use needs no dependency

    url = f"{base_url}{path}"
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


def fetch_ticker_price(
    symbol: str,
    base_url: str = FUTURES_BASE_URL,
    session: Optional[object] = None,
) -> float:
    """Latest traded price for ``symbol`` from Binance's public ticker.

    Uses ``/fapi/v1/ticker/price`` for USDⓈ-M futures (``base_url`` contains
    ``fapi``) or ``/api/v3/ticker/price`` for spot. Weight 1, no auth.
    """
    import requests  # imported lazily so offline use needs no dependency

    path = "/fapi/v1/ticker/price" if "fapi" in base_url else "/api/v3/ticker/price"
    http = session or requests
    resp = http.get(f"{base_url}{path}", params={"symbol": symbol}, timeout=10)
    resp.raise_for_status()
    return float(resp.json()["price"])


def _raw_to_candles(raw) -> List[Candle]:
    return [
        Candle(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
        for k in raw
    ]


def fetch_klines_range(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: Optional[int] = None,
    base_url: str = SPOT_BASE_URL,
    path: str = SPOT_KLINES_PATH,
    max_per_req: int = 1000,
    drop_unclosed: bool = True,
    session: Optional[object] = None,
) -> List[Candle]:
    """Fetch ALL klines in ``[start_ms, end_ms]`` (epoch ms), paginating past
    Binance's per-request cap so you can backtest an arbitrary date range.

    ``end_ms=None`` means "up to now" (the still-forming last bar is dropped
    when ``drop_unclosed``). Bars are returned oldest-first, de-duplicated.
    """
    import requests  # imported lazily so offline use needs no dependency

    http = session or requests
    url = f"{base_url}{path}"
    step = INTERVAL_MS.get(interval, INTERVAL_MS["4h"])
    out: List[Candle] = []
    cur = start_ms
    while True:
        params = {"symbol": symbol, "interval": interval, "startTime": cur, "limit": max_per_req}
        if end_ms is not None:
            params["endTime"] = end_ms
        resp = http.get(url, params=params, timeout=20)
        resp.raise_for_status()
        raw = resp.json()
        if not raw:
            break
        out.extend(_raw_to_candles(raw))
        nxt = int(raw[-1][0]) + step  # advance past the last bar (no overlap)
        if len(raw) < max_per_req or (end_ms is not None and nxt > end_ms):
            break
        cur = nxt

    if end_ms is not None:
        out = [c for c in out if c.open_time <= end_ms]
    elif drop_unclosed and out:
        out = out[:-1]  # only the live tail can be a forming bar
    return out


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
