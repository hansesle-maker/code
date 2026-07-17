"""Multi-symbol, multi-timeframe TSI scanner for Binance USDT-M Futures.

Fetches live klines for every active USDT perpetual and computes TSI(25,13,13)
across four timeframes (12h, 4h, 1h, 15m). Designed for the web dashboard but
importable on its own.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests as _req

from .data import FUTURES_BASE_URL, FUTURES_KLINES_PATH, fetch_klines
from .indicators import true_strength_index

log = logging.getLogger(__name__)

TIMEFRAMES = ("12h", "4h", "1h", "15m")

# Extra TradFi symbols to always include (merged with auto-fetched list).
TRADFI_SYMBOLS: List[str] = ["XAUUSDT"]

# Only these three timeframes contribute to bull/bear score so that existing
# alert thresholds (score == 3 = full alignment) remain unchanged.
_SCORE_TFS = ("4h", "1h", "15m")

# Binance USDT-M klines weight tiers (per request):
#   limit  1-99  → weight 1   ← we use this
#   limit 100-499 → weight 2
# TSI(25,13,13) needs ≥50 bars; 99 gives ~49 bars of extra warm-up and
# keeps weight=1 so we stay well within the 2400-weight/min rate limit.
KLINE_LIMIT = 99


@dataclass
class TFState:
    tsi: float
    signal: float
    above_zero: bool      # TSI > 0
    rising: bool          # TSI[n] > TSI[n-1]  (TSI slope up)
    above_signal: bool    # TSI > signal
    sig_slope: bool       # signal[n] > signal[n-1]  (signal line rising)
    sig_above_zero: bool  # signal > 0
    fresh_cross: int = 0  # +1 = just crossed above signal, -1 = just crossed below, 0 = no cross
    sig_inflect_bars: int = -1  # bars ago of last mathematical inflection in signal line (-1 = not found)
    sig_inflect_type: str = ""  # "하락변곡" (peak) or "상승변곡" (trough)


@dataclass
class SymbolScan:
    symbol: str
    ts: int                             # epoch ms of latest closed bar
    tf: Dict[str, Optional[TFState]]    # "12h"/"4h"/"1h"/"15m" -> TFState | None
    last_price: float = 0.0             # latest closed 15m price (entry/exit ref)
    error: Optional[str] = None

    @property
    def bull_score(self) -> int:
        """4h/1h/15m timeframes where TSI > 0 AND TSI > signal (fully bullish)."""
        return sum(
            1 for tf in _SCORE_TFS
            if (s := self.tf.get(tf)) and s.above_zero and s.above_signal
        )

    @property
    def bear_score(self) -> int:
        """4h/1h/15m timeframes where TSI < 0 AND TSI < signal (fully bearish)."""
        return sum(
            1 for tf in _SCORE_TFS
            if (s := self.tf.get(tf)) and not s.above_zero and not s.above_signal
        )


def fetch_all_futures_symbols(session=None) -> List[str]:
    """Return sorted list of active USDT-M perpetual futures symbols."""
    http = session or _req
    resp = http.get(f"{FUTURES_BASE_URL}/fapi/v1/exchangeInfo", timeout=20)
    resp.raise_for_status()
    return sorted(
        s["symbol"]
        for s in resp.json()["symbols"]
        if s["status"] == "TRADING"
        and s["contractType"] == "PERPETUAL"
        and s["quoteAsset"] == "USDT"
    )


def _find_last_inflection(sig_vals: List[float], max_bars: int = 30):
    """Return (bars_ago, type_str) for the most recent signal-line inflection point.

    A mathematical inflection occurs where the slope (first derivative) changes sign:
    - "하락변곡": slope went positive → negative (local peak)
    - "상승변곡": slope went negative → positive (local trough)

    Searches only the last max_bars bars to avoid warmup-period artifacts.
    Returns (-1, "") when no inflection is found within the search window.
    """
    n = len(sig_vals)
    if n < 3:
        return -1, ""
    slopes = [sig_vals[i] - sig_vals[i - 1] for i in range(1, n)]
    # j indexes slopes; sig_vals[j] is the candidate inflection point.
    # bars_ago = (n-1) - j  (j = n-2 → bars_ago = 1 = most recent possible)
    j_min = max(1, len(slopes) - max_bars)
    for j in range(len(slopes) - 1, j_min - 1, -1):
        prev_s, cur_s = slopes[j - 1], slopes[j]
        if prev_s > 0 and cur_s < 0:
            return (n - 1) - j, "하락변곡"
        if prev_s < 0 and cur_s > 0:
            return (n - 1) - j, "상승변곡"
    return -1, ""


def _state_from_closes(closes: List[float]) -> Optional[TFState]:
    if len(closes) < 55:
        return None
    tsi_vals, sig_vals = true_strength_index(closes)
    if len(tsi_vals) < 2:
        return None
    cur, prev = tsi_vals[-1], tsi_vals[-2]
    sig, sig_prev = sig_vals[-1], sig_vals[-2]

    # Detect a fresh signal-line cross on this bar vs the previous bar.
    if cur > sig and prev <= sig_prev:
        fresh_cross = 1      # just crossed above signal
    elif cur < sig and prev >= sig_prev:
        fresh_cross = -1     # just crossed below signal
    else:
        fresh_cross = 0

    inf_bars, inf_type = _find_last_inflection(sig_vals)

    return TFState(
        tsi=round(cur, 4),
        signal=round(sig, 4),
        above_zero=cur > 0,
        rising=cur > prev,
        above_signal=cur > sig,
        sig_slope=sig > sig_prev,
        sig_above_zero=sig > 0,
        fresh_cross=fresh_cross,
        sig_inflect_bars=inf_bars,
        sig_inflect_type=inf_type,
    )


def _fetch_with_retry(symbol: str, tf: str, http, retries: int = 3) -> list:
    """Fetch klines with up to ``retries`` retries on 429 / 5xx errors."""
    for attempt in range(retries):
        try:
            candles = fetch_klines(
                symbol, tf,
                limit=KLINE_LIMIT,
                base_url=FUTURES_BASE_URL,
                path=FUTURES_KLINES_PATH,
                drop_unclosed=True,
                session=http,
            )
            return candles
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 429 or status == 418:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    log.warning("Rate-limited fetching %s %s (attempt %d/%d) — waiting %ds",
                                symbol, tf, attempt + 1, retries, wait)
                    time.sleep(wait)
            else:
                log.debug("Fetch error %s %s: %s", symbol, tf, exc)
                break
    return []


def scan_symbol(symbol: str, session=None) -> SymbolScan:
    """Fetch klines for all four timeframes and compute TSI states."""
    http = session or _req
    tf_states: Dict[str, Optional[TFState]] = {}
    latest_ts = 0
    last_price = 0.0
    for tf in TIMEFRAMES:
        candles = _fetch_with_retry(symbol, tf, http)
        if candles:
            latest_ts = max(latest_ts, candles[-1].open_time)
            closes = [c.close for c in candles]
            if tf == "15m":
                last_price = closes[-1]
            tf_states[tf] = _state_from_closes(closes)
        else:
            tf_states[tf] = None
    return SymbolScan(symbol=symbol, ts=latest_ts, tf=tf_states,
                      last_price=last_price)


def scan_all(
    symbols: List[str],
    max_workers: int = 6,
    progress_every: int = 50,
) -> List[SymbolScan]:
    """Scan all symbols concurrently and return results sorted by symbol name.

    ``max_workers=6`` with ``KLINE_LIMIT=99`` (weight=1) keeps us comfortably
    under Binance's 2400-weight/min limit even for 500+ symbol universes.
    Each thread gets its own session to avoid connection-pool contention.
    """
    results: List[SymbolScan] = []

    def _make_session() -> _req.Session:
        s = _req.Session()
        s.headers.update({"Connection": "keep-alive"})
        return s

    done = 0
    total = len(symbols)

    with ThreadPoolExecutor(max_workers=max_workers,
                            initializer=None) as pool:
        futs = {pool.submit(scan_symbol, sym, _make_session()): sym
                for sym in symbols}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append(
                    SymbolScan(symbol=futs[fut], ts=0, tf={}, error=str(exc))
                )
            done += 1
            if progress_every and done % progress_every == 0:
                ok = sum(1 for r in results if any(r.tf.values()))
                log.info("  %d/%d scanned, %d with data …", done, total, ok)

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
                "sig_slope": s.sig_slope,
                "sig_above_zero": s.sig_above_zero,
                "fresh_cross": s.fresh_cross,
                "sig_inflect_bars": s.sig_inflect_bars,
                "sig_inflect_type": s.sig_inflect_type,
            }
    return {
        "symbol": r.symbol,
        "bull_score": r.bull_score,
        "bear_score": r.bear_score,
        "last_price": r.last_price,
        "tf": tfs,
    }
