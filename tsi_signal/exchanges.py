"""Unified candle/universe access for Binance USDⓈ-M futures and Bithumb KRW.

Binance reuses :func:`tsi_signal.data.fetch_klines`. Bithumb is served by its
v1 REST API (Upbit-compatible shape)::

    GET /v1/market/all                      → [{"market": "KRW-BTC", …}, …]
    GET /v1/candles/minutes/{unit}          → newest-first candle rows
    GET /v1/ticker?markets=KRW-BTC,…        → 24h traded value per market

Bithumb's minute units stop at 240 (4h), so 12h candles are aggregated from
three 4h candles on UTC 00:00 / 12:00 boundaries — the same grid Binance and
TradingView use. Row parsing accepts the v1 keys, their short aliases and the
legacy ``/public/candlestick`` array shape, so a response-format change
degrades into a clear error instead of silently wrong numbers.
"""
from __future__ import annotations

import datetime
import logging
import threading
import time
from typing import Dict, List, Optional, Sequence

import requests as _req

from .data import (
    FUTURES_BASE_URL,
    FUTURES_KLINES_PATH,
    Candle,
    fetch_klines,
)

log = logging.getLogger(__name__)

BITHUMB_BASE = "https://api.bithumb.com"

BINANCE = "binance"
BITHUMB = "bithumb"

EXCHANGE_LABELS = {
    BINANCE: "바이낸스 USDT-M 선물",
    BITHUMB: "빗썸 원화(KRW)",
}

# Timeframes the CVD page scans, longest first.
TIMEFRAMES = ("12h", "4h", "1h", "15m", "5m", "1m")

TF_MS: Dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "12h": 43_200_000,
}

# Bithumb v1 minute units (1,3,5,10,15,30,60,240 are supported upstream).
_BITHUMB_MIN_UNIT = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
_BITHUMB_PAGE = 200          # max rows per v1 candle request
_AGG_12H_PARTS = 3           # 12h = 3 × 4h


class RateLimiter:
    """Simple thread-safe cap of ``rate`` requests per second."""

    def __init__(self, rate: float):
        self._min_gap = 1.0 / rate if rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self._min_gap <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if self._next > now:
                delay = self._next - now
            else:
                delay = 0.0
            self._next = max(now, self._next) + self._min_gap
        if delay > 0:
            time.sleep(delay)


# Bithumb's public API is rate-limited far more tightly than Binance's.
BITHUMB_LIMITER = RateLimiter(18.0)


def _num(row: dict, *keys) -> float:
    for k in keys:
        if k in row and row[k] is not None:
            return float(row[k])
    raise KeyError(f"none of {keys} present in {sorted(row)[:8]}")


def _bithumb_row_to_candle(row) -> Candle:
    """Parse one Bithumb candle row (v1 dict, alias dict, or legacy array)."""
    if isinstance(row, (list, tuple)):
        # legacy /public/candlestick: [ts, open, close, high, low, volume]
        return Candle(open_time=int(row[0]), open=float(row[1]),
                      high=float(row[3]), low=float(row[4]),
                      close=float(row[2]), volume=float(row[5]))
    ts = row.get("timestamp")
    iso = row.get("candle_date_time_utc")
    if iso:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "")).replace(
            tzinfo=datetime.timezone.utc)
        open_ms = int(dt.timestamp() * 1000)
    elif ts is not None:
        open_ms = int(ts)
    else:
        raise KeyError("candle row has neither candle_date_time_utc nor timestamp")
    return Candle(
        open_time=open_ms,
        open=_num(row, "opening_price", "open"),
        high=_num(row, "high_price", "high"),
        low=_num(row, "low_price", "low"),
        close=_num(row, "trade_price", "close"),
        volume=_num(row, "candle_acc_trade_volume", "volume", "units_traded"),
    )


def _bithumb_get(path: str, params: Optional[dict] = None, session=None):
    http = session or _req
    BITHUMB_LIMITER.wait()
    resp = http.get(f"{BITHUMB_BASE}{path}", params=params or {}, timeout=20,
                    headers={"accept": "application/json"})
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if "error" in data:
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else err
            raise RuntimeError(f"Bithumb API error on {path}: {msg}")
        status = data.get("status")
        if status is not None and str(status) != "0000":
            raise RuntimeError(f"Bithumb API status {status} on {path}")
        if "data" in data:
            return data["data"]
    return data


def fetch_bithumb_markets(session=None) -> List[str]:
    """All tradable KRW markets, e.g. ``["KRW-BTC", "KRW-ETH", …]``."""
    data = _bithumb_get("/v1/market/all", {"isDetails": "false"}, session)
    out: List[str] = []
    if isinstance(data, dict):        # legacy ALL_KRW shape: {"BTC": {...}, …}
        out = [f"KRW-{k}" for k in data if k != "date"]
    else:
        for m in data:
            mk = m.get("market") if isinstance(m, dict) else None
            if mk and str(mk).startswith("KRW-"):
                out.append(str(mk))
    if not out:
        raise RuntimeError("Bithumb market list came back empty")
    return sorted(out)


def _bithumb_minutes(market: str, unit: int, count: int,
                     session=None, to: Optional[str] = None) -> List[Candle]:
    params: dict = {"market": market, "count": min(_BITHUMB_PAGE, count)}
    if to:
        params["to"] = to
    rows = _bithumb_get(f"/v1/candles/minutes/{unit}", params, session)
    if not isinstance(rows, list):
        raise RuntimeError(f"unexpected Bithumb candle payload: {type(rows).__name__}")
    return [_bithumb_row_to_candle(r) for r in rows]


def _bithumb_minutes_paged(market: str, unit: int, need: int,
                           session=None) -> List[Candle]:
    """Page backwards with ``to`` until ``need`` candles are collected."""
    out: List[Candle] = []
    to: Optional[str] = None
    seen: set = set()
    while len(out) < need:
        batch = _bithumb_minutes(market, unit, need - len(out), session, to)
        fresh = [c for c in batch if c.open_time not in seen]
        if not fresh:
            break
        for c in fresh:
            seen.add(c.open_time)
        out.extend(fresh)
        oldest = min(c.open_time for c in fresh)
        to = datetime.datetime.fromtimestamp(
            oldest / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        if len(batch) < _BITHUMB_PAGE:
            break
    out.sort(key=lambda c: c.open_time)
    return out


def aggregate(candles: Sequence[Candle], step_ms: int, parts: int) -> List[Candle]:
    """Group candles into ``step_ms`` buckets, keeping only complete buckets."""
    buckets: Dict[int, List[Candle]] = {}
    for c in candles:
        buckets.setdefault(c.open_time // step_ms, []).append(c)
    out: List[Candle] = []
    for b in sorted(buckets):
        group = sorted(buckets[b], key=lambda c: c.open_time)
        if len(group) != parts:
            continue                       # partial (or gapped) bucket
        out.append(Candle(
            open_time=b * step_ms,
            open=group[0].open,
            high=max(c.high for c in group),
            low=min(c.low for c in group),
            close=group[-1].close,
            volume=sum(c.volume for c in group),
        ))
    return out


def _drop_forming(candles: List[Candle], tf: str) -> List[Candle]:
    """Drop the still-open bar so only closed candles reach the indicator."""
    step = TF_MS[tf]
    now_bucket = int(time.time() * 1000) // step
    while candles and candles[-1].open_time // step >= now_bucket:
        candles = candles[:-1]
    return candles


def fetch_bithumb_candles(market: str, tf: str, need: int = 150,
                          session=None) -> List[Candle]:
    if tf in _BITHUMB_MIN_UNIT:
        candles = _bithumb_minutes_paged(market, _BITHUMB_MIN_UNIT[tf],
                                         need + 2, session)
    elif tf == "12h":
        raw = _bithumb_minutes_paged(market, 240,
                                     (need + 2) * _AGG_12H_PARTS, session)
        candles = aggregate(raw, TF_MS["12h"], _AGG_12H_PARTS)
    else:
        raise ValueError(f"unsupported Bithumb timeframe: {tf}")
    return _drop_forming(candles, tf)


def fetch_bithumb_volumes(markets: Sequence[str], session=None) -> Dict[str, float]:
    """24h traded value (KRW) per market; missing entries just rank last."""
    out: Dict[str, float] = {}
    chunk = 100
    for i in range(0, len(markets), chunk):
        part = list(markets[i:i + chunk])
        try:
            rows = _bithumb_get("/v1/ticker", {"markets": ",".join(part)}, session)
        except Exception as exc:
            log.warning("Bithumb ticker chunk failed: %s", exc)
            continue
        if isinstance(rows, dict):
            rows = [rows]
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            mk = r.get("market")
            if not mk:
                continue
            try:
                out[str(mk)] = _num(r, "acc_trade_price_24h",
                                    "acc_trade_price24h", "acc_trade_price")
            except KeyError:
                continue
    return out


def fetch_binance_volumes(session=None) -> Dict[str, float]:
    """24h quote volume per USDⓈ-M futures symbol (single weight-40 call)."""
    http = session or _req
    resp = http.get(f"{FUTURES_BASE_URL}/fapi/v1/ticker/24hr", timeout=30)
    resp.raise_for_status()
    out: Dict[str, float] = {}
    for r in resp.json():
        try:
            out[r["symbol"]] = float(r.get("quoteVolume") or 0.0)
        except (TypeError, ValueError):
            continue
    return out


def get_candles(exchange: str, symbol: str, tf: str, need: int = 150,
                session=None) -> List[Candle]:
    """Closed candles, oldest first, for either exchange."""
    if exchange == BITHUMB:
        return fetch_bithumb_candles(symbol, tf, need, session)
    return fetch_klines(
        symbol, tf,
        limit=min(1500, need + 2),
        base_url=FUTURES_BASE_URL,
        path=FUTURES_KLINES_PATH,
        drop_unclosed=True,
        session=session,
    )


def get_universe(exchange: str, top_n: Optional[int] = None,
                 session=None) -> List[str]:
    """Symbols ranked by 24h traded value, most active first."""
    if exchange == BITHUMB:
        symbols = fetch_bithumb_markets(session)
        vols = fetch_bithumb_volumes(symbols, session)
    else:
        from .scanner import fetch_all_futures_symbols
        symbols = fetch_all_futures_symbols(session=session)
        vols = fetch_binance_volumes(session)
    ranked = sorted(symbols, key=lambda s: (-vols.get(s, 0.0), s))
    return ranked[:top_n] if top_n else ranked


def display_symbol(exchange: str, symbol: str) -> str:
    """Short label: ``BTCUSDT`` → ``BTC``, ``KRW-BTC`` → ``BTC``."""
    if exchange == BITHUMB:
        return symbol.split("-", 1)[1] if "-" in symbol else symbol
    return symbol[:-4] if symbol.endswith("USDT") else symbol
