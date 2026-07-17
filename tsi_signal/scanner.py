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

from .data import (
    FUTURES_BASE_URL,
    FUTURES_KLINES_PATH,
    MARKETS,
    SPOT_BASE_URL,
    fetch_klines,
)
from .indicators import true_strength_index

log = logging.getLogger(__name__)

TIMEFRAMES = ("12h", "4h", "1h", "15m")

# TradFi 후보 심볼 (금·미국주식 페어 등). 매 스캔마다 바이낸스 선물 →
# 현물 순서로 실제 상장 여부를 확인해서(:func:`resolve_tradfi_markets`)
# 있는 마켓의 API로 가져오고, 어느 쪽에도 없으면 대시보드에 "미상장"
# 안내를 띄운다. 여기에 심볼만 추가하면 나머지는 자동.
TRADFI_SYMBOLS: List[str] = [
    "XAUUSDT",   # 금 (없으면 미상장 표시 — 금 프록시는 PAXGUSDT 선물이 자동 포함됨)
    "NVDAUSDT",
    "TSLAUSDT",
    "AAPLUSDT",
]

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


def fetch_futures_symbol_info(session=None) -> List[dict]:
    """Raw symbol entries from USDT-M futures exchangeInfo (one HTTP call)."""
    http = session or _req
    resp = http.get(f"{FUTURES_BASE_URL}/fapi/v1/exchangeInfo", timeout=20)
    resp.raise_for_status()
    return resp.json()["symbols"]


def fetch_all_futures_symbols(session=None,
                              info: Optional[List[dict]] = None) -> List[str]:
    """Return sorted list of active USDT-M perpetual futures symbols."""
    if info is None:
        info = fetch_futures_symbol_info(session)
    return sorted(
        s["symbol"]
        for s in info
        if s["status"] == "TRADING"
        and s["contractType"] == "PERPETUAL"
        and s["quoteAsset"] == "USDT"
    )


def fetch_spot_symbols(session=None) -> set:
    """All TRADING symbols on Binance spot (tokenized stocks live here if
    they are not listed as USDT-M perpetuals)."""
    http = session or _req
    resp = http.get(f"{SPOT_BASE_URL}/api/v3/exchangeInfo", timeout=30)
    resp.raise_for_status()
    return {s["symbol"] for s in resp.json()["symbols"]
            if s["status"] == "TRADING"}


def resolve_tradfi_markets(futures_all: set, session=None):
    """TradFi 후보 심볼을 실제 상장 마켓에 매핑한다.

    확인 순서: USDT-M 선물(모든 contractType 포함) → 현물.
    반환: (markets, missing)
      markets: {symbol: "futures" | "spot"}  — 상장 확인된 심볼
      missing: [symbol, ...]                 — 어느 마켓에도 없는 심볼
    """
    markets: Dict[str, str] = {}
    missing: List[str] = []
    spot: Optional[set] = None
    for sym in TRADFI_SYMBOLS:
        if sym in futures_all:
            markets[sym] = "futures"
            continue
        if spot is None:
            try:
                spot = fetch_spot_symbols(session)
            except Exception as exc:
                log.warning("Spot exchangeInfo fetch failed: %s", exc)
                spot = set()
        if sym in spot:
            markets[sym] = "spot"
        else:
            missing.append(sym)
            log.warning("TradFi symbol %s not listed on Binance futures or spot", sym)
    if markets:
        log.info("TradFi symbols resolved: %s",
                 ", ".join(f"{s}({m})" for s, m in markets.items()))
    return markets, missing


def _find_last_inflection(sig_vals: List[float], max_bars: int = 45,
                          min_run: int = 2):
    """시그널선의 수학적 변곡점(2차 도함수 부호 전환) 탐지.

        accel[k] = sig[k+2] - 2·sig[k+1] + sig[k]   (이산 2차 도함수)

    - accel + → - : "하락변곡" (위로 볼록 전환 — 상승 둔화/하락 가속 시작)
    - accel - → + : "상승변곡" (아래로 볼록 전환 — 하락 둔화/상승 가속 시작)

    노이즈 필터: 전환 전·후의 곡률 부호가 각각 ``min_run``봉 이상 유지된
    "확정 변곡"만 인정. 1봉짜리 부호 반전(잔물결)은 무시되므로 TSI가
    한 봉 흔들릴 때마다 변곡 표시가 왔다갔다 하지 않는다.

    반환: (bars_ago, 종류). 확정 변곡이 ``max_bars`` 안에 없으면 (-1, "").
    """
    n = len(sig_vals)
    if n < 5:
        return -1, ""
    accel = [sig_vals[k + 2] - 2.0 * sig_vals[k + 1] + sig_vals[k]
             for k in range(n - 2)]
    signs: List[int] = []
    for a in accel:
        if a > 0:
            signs.append(1)
        elif a < 0:
            signs.append(-1)
        else:  # 정확히 0이면 직전 부호 유지 (전환으로 치지 않음)
            signs.append(signs[-1] if signs else 0)

    # 최신 → 과거 방향으로 같은 부호 구간(run) 목록 생성
    runs: List[tuple] = []  # (sign, start, end) — accel 인덱스 기준
    i = len(signs) - 1
    while i >= 0:
        j = i
        while j > 0 and signs[j - 1] == signs[i]:
            j -= 1
        runs.append((signs[i], j, i))
        i = j - 1

    # 인접 run 쌍에서 부호 반전 + 양쪽 min_run 이상 유지 → 확정 변곡
    for r in range(len(runs) - 1):
        new_sign, new_start, new_end = runs[r]
        old_sign, old_start, old_end = runs[r + 1]
        if new_sign == 0 or old_sign == 0 or new_sign == old_sign:
            continue
        if (new_end - new_start + 1) < min_run or (old_end - old_start + 1) < min_run:
            continue  # 1봉짜리 잔물결 — 확정 변곡 아님
        bars_ago = (n - 1) - (new_start + 2)  # accel[k] = sig[k+2] 시점의 곡률
        if bars_ago > max_bars:
            break
        return bars_ago, ("하락변곡" if new_sign < 0 else "상승변곡")
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


def _fetch_with_retry(symbol: str, tf: str, http,
                      base_url: str = FUTURES_BASE_URL,
                      path: str = FUTURES_KLINES_PATH,
                      retries: int = 3) -> list:
    """Fetch klines with up to ``retries`` retries on 429 / 5xx errors."""
    for attempt in range(retries):
        try:
            candles = fetch_klines(
                symbol, tf,
                limit=KLINE_LIMIT,
                base_url=base_url,
                path=path,
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


def scan_symbol(symbol: str, session=None, market: str = "futures") -> SymbolScan:
    """Fetch klines for all four timeframes and compute TSI states.

    ``market`` selects the API ("futures" | "spot", see :data:`MARKETS`) —
    TradFi symbols may live on spot instead of USDT-M futures.
    """
    http = session or _req
    base_url, path = MARKETS.get(market, MARKETS["futures"])
    tf_states: Dict[str, Optional[TFState]] = {}
    latest_ts = 0
    last_price = 0.0
    for tf in TIMEFRAMES:
        candles = _fetch_with_retry(symbol, tf, http, base_url, path)
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
    markets: Optional[Dict[str, str]] = None,
) -> List[SymbolScan]:
    """Scan all symbols concurrently and return results sorted by symbol name.

    ``markets`` maps symbol → "futures"/"spot" for symbols not on USDT-M
    futures (TradFi); unlisted symbols default to futures.

    ``max_workers=6`` with ``KLINE_LIMIT=99`` (weight=1) keeps us comfortably
    under Binance's 2400-weight/min limit even for 500+ symbol universes.
    Each thread gets its own session to avoid connection-pool contention.
    """
    results: List[SymbolScan] = []
    markets = markets or {}

    def _make_session() -> _req.Session:
        s = _req.Session()
        s.headers.update({"Connection": "keep-alive"})
        return s

    done = 0
    total = len(symbols)

    with ThreadPoolExecutor(max_workers=max_workers,
                            initializer=None) as pool:
        futs = {pool.submit(scan_symbol, sym, _make_session(),
                            markets.get(sym, "futures")): sym
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
