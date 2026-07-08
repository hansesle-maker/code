"""Multi-symbol SMC screener for Binance USDT-M Futures.

Fetches klines for every active USDT perpetual on a single timeframe and runs
the :mod:`tsi_signal.smc` engine on each, returning the full trading state
(position, SL/TP, structure, zones, order blocks …) per symbol. Designed for
the web dashboard but importable on its own.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import List, Optional

import requests as _req

from .data import FUTURES_BASE_URL, FUTURES_KLINES_PATH, fetch_klines
from .scanner import fetch_all_futures_symbols
from .smc import SMCParams, SMCResult, analyze

SMC_TIMEFRAMES = ("5m", "15m", "1h", "4h")
DEFAULT_TIMEFRAME = "15m"

# 499 bars keeps the Binance request weight low (limit<500 → weight 2) while
# still covering the ATR(200) warm-up plus ~300 tradable bars of simulation.
KLINE_LIMIT = 499


def scan_symbol_smc(symbol: str, tf: str = DEFAULT_TIMEFRAME,
                    params: Optional[SMCParams] = None, session=None) -> SMCResult:
    """Fetch closed klines for one symbol and run the SMC engine on them."""
    try:
        candles = fetch_klines(
            symbol, tf,
            limit=KLINE_LIMIT,
            base_url=FUTURES_BASE_URL,
            path=FUTURES_KLINES_PATH,
            drop_unclosed=True,
            session=session,
        )
        return analyze(candles, symbol=symbol, tf=tf, params=params)
    except Exception as exc:
        return SMCResult(symbol=symbol, tf=tf, error=f"{type(exc).__name__}: {exc}")


def scan_all_smc(symbols: List[str], tf: str = DEFAULT_TIMEFRAME,
                 params: Optional[SMCParams] = None,
                 max_workers: int = 8) -> List[SMCResult]:
    """Scan all symbols concurrently; results are sorted by symbol name."""
    results: List[SMCResult] = []
    session = _req.Session()
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {pool.submit(scan_symbol_smc, sym, tf, params, session): sym
                    for sym in symbols}
            for fut in as_completed(futs):
                try:
                    results.append(fut.result())
                except Exception as exc:  # defensive: scan_symbol_smc catches its own
                    results.append(SMCResult(symbol=futs[fut], tf=tf, error=str(exc)))
    finally:
        session.close()
    return sorted(results, key=lambda r: r.symbol)


def smc_to_dict(r: SMCResult) -> dict:
    """Serialize a result to a plain dict (for the JSON API)."""
    return asdict(r)


__all__ = [
    "SMC_TIMEFRAMES",
    "DEFAULT_TIMEFRAME",
    "KLINE_LIMIT",
    "fetch_all_futures_symbols",
    "scan_symbol_smc",
    "scan_all_smc",
    "smc_to_dict",
]
