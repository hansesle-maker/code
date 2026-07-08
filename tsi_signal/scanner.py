"""Multi-symbol, multi-timeframe TSI scanner for Binance USDT-M Futures.

Fetches live klines for every active USDT perpetual and computes TSI(25,13,13)
across three timeframes (4h, 1h, 15m). Designed for the web dashboard but
importable on its own.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests as _req

from .data import FUTURES_BASE_URL, FUTURES_KLINES_PATH, fetch_klines
from .indicators import true_strength_index
from .rate_limit import FUTURES_LIMITER, BinanceBanned

TIMEFRAMES = ("4h", "1h", "15m")
KLINE_LIMIT = 300  # TSI(25,13,13) needs ~55 bars; 300 gives plenty of warm-up
EXCHANGE_INFO_WEIGHT = 20  # conservative estimate for GET /fapi/v1/exchangeInfo
KLINE_WEIGHT = 2           # weight for klines with 100 < limit <= 500


@dataclass
class TFState:
    tsi: float
    signal: float
    above_zero: bool   # TSI > 0
    rising: bool       # TSI[n] > TSI[n-1]
    above_signal: bool # TSI > signal


@dataclass
class SymbolScan:
    symbol: str
    ts: int                             # epoch ms of latest closed bar
    tf: Dict[str, Optional[TFState]]    # "4h" / "1h" / "15m" -> TFState | None
    error: Optional[str] = None

    @property
    def bull_score(self) -> int:
        """Timeframes where TSI > 0 AND TSI > signal (fully bullish)."""
        return sum(
            1 for tf in TIMEFRAMES
            if (s := self.tf.get(tf)) and s.above_zero and s.above_signal
        )

    @property
    def bear_score(self) -> int:
        """Timeframes where TSI < 0 AND TSI < signal (fully bearish)."""
        return sum(
            1 for tf in TIMEFRAMES
            if (s := self.tf.get(tf)) and not s.above_zero and not s.above_signal
        )


def fetch_all_futures_symbols(session=None) -> List[str]:
    """Return sorted list of active USDT-M perpetual futures symbols."""
    http = session or _req
    FUTURES_LIMITER.acquire(EXCHANGE_INFO_WEIGHT)
    try:
        resp = http.get(f"{FUTURES_BASE_URL}/fapi/v1/exchangeInfo", timeout=20)
        resp.raise_for_status()
    except _req.exceptions.HTTPError as exc:
        FUTURES_LIMITER.note_error(exc)
        raise
    return sorted(
        s["symbol"]
        for s in resp.json()["symbols"]
        if s["status"] == "TRADING"
        and s["contractType"] == "PERPETUAL"
        and s["quoteAsset"] == "USDT"
    )


def _state_from_closes(closes: List[float]) -> Optional[TFState]:
    if len(closes) < 55:
        return None
    tsi_vals, sig_vals = true_strength_index(closes)
    if len(tsi_vals) < 2:
        return None
    cur, prev, sig = tsi_vals[-1], tsi_vals[-2], sig_vals[-1]
    return TFState(
        tsi=round(cur, 4),
        signal=round(sig, 4),
        above_zero=cur > 0,
        rising=cur > prev,
        above_signal=cur > sig,
    )


def scan_symbol(symbol: str, session=None) -> SymbolScan:
    """Fetch klines for all three timeframes and compute TSI states."""
    http = session or _req
    tf_states: Dict[str, Optional[TFState]] = {}
    latest_ts = 0
    for tf in TIMEFRAMES:
        FUTURES_LIMITER.acquire(KLINE_WEIGHT)  # raises BinanceBanned if cooling down
        try:
            candles = fetch_klines(
                symbol, tf,
                limit=KLINE_LIMIT,
                base_url=FUTURES_BASE_URL,
                path=FUTURES_KLINES_PATH,
                drop_unclosed=True,
                session=http,
            )
            if candles:
                latest_ts = max(latest_ts, candles[-1].open_time)
                tf_states[tf] = _state_from_closes([c.close for c in candles])
            else:
                tf_states[tf] = None
        except _req.exceptions.HTTPError as exc:
            FUTURES_LIMITER.note_error(exc)  # raises BinanceBanned on 429/418
            tf_states[tf] = None
        except Exception:
            tf_states[tf] = None
    return SymbolScan(symbol=symbol, ts=latest_ts, tf=tf_states)


def scan_all(symbols: List[str], max_workers: int = 5) -> List[SymbolScan]:
    """Scan all symbols concurrently and return results sorted by symbol name.

    Aborts early (raising :class:`BinanceBanned`) if Binance rate-limits or
    bans the IP mid-scan, instead of continuing to hammer it symbol by symbol.
    """
    results: List[SymbolScan] = []
    session = _req.Session()
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {pool.submit(scan_symbol, sym, session): sym for sym in symbols}
            try:
                for fut in as_completed(futs):
                    try:
                        results.append(fut.result())
                    except BinanceBanned:
                        raise
                    except Exception as exc:
                        results.append(
                            SymbolScan(symbol=futs[fut], ts=0, tf={}, error=str(exc))
                        )
            except BinanceBanned:
                for f in futs:
                    f.cancel()
                raise
    finally:
        session.close()
    return sorted(results, key=lambda r: r.symbol)


def symbolscan_to_dict(r: SymbolScan) -> dict:
    """Serialize a scan to a plain dict (for data.json / the JSON API)."""
    tfs = {}
    for tf, s in r.tf.items():
        if s:
            tfs[tf] = {
                "tsi": s.tsi,
                "signal": s.signal,
                "above_zero": s.above_zero,
                "rising": s.rising,
                "above_signal": s.above_signal,
            }
    return {
        "symbol": r.symbol,
        "bull_score": r.bull_score,
        "bear_score": r.bear_score,
        "tf": tfs,
    }
