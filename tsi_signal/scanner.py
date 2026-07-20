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

# TradFi 수동 후보 (안전망). 기본 탐색은 :func:`discover_tradfi_futures`가
# exchangeInfo의 underlyingType/underlyingSubType로 자동 수행하므로 보통
# 비워 둔다. 자동 탐색에 안 걸리는 심볼이 있으면 여기 추가 — 매 스캔마다
# 선물 → 현물 순으로 상장 확인 후(:func:`resolve_tradfi_markets`) 맞는
# API로 가져오고, 어느 쪽에도 없으면 대시보드에 "미상장"으로 표시된다.
TRADFI_SYMBOLS: List[str] = []

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
    ts: int                             # epoch ms of latest bar (진행 중 봉 포함)
    tf: Dict[str, Optional[TFState]]    # "12h"/"4h"/"1h"/"15m" -> TFState | None
    last_price: float = 0.0             # scan-time 15m price (entry/exit ref)
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


# fapi exchangeInfo에서 TradFi(주식·귀금속 등) 상품을 식별하는 토큰.
# 예: underlyingType "EQUITY"/"STOCK", underlyingSubType ["US-STOCKS"] 등.
# METAL/COMMODITY는 금(XAU류) 상품이 상장될 경우를 대비해 포함.
_TRADFI_TYPE_TOKENS = ("STOCK", "EQUITY", "TRADFI", "METAL", "COMMODITY")


def is_tradfi_entry(s: dict) -> bool:
    """exchangeInfo 심볼 항목이 TradFi(주식 등) 상품인지 판별."""
    ut = str(s.get("underlyingType", "")).upper()
    if any(tok in ut for tok in _TRADFI_TYPE_TOKENS):
        return True
    subs = [str(x).upper() for x in (s.get("underlyingSubType") or [])]
    return any(tok in sub for sub in subs for tok in _TRADFI_TYPE_TOKENS)


def discover_tradfi_futures(info: List[dict]) -> List[str]:
    """fapi exchangeInfo에서 TradFi 상품 심볼을 자동 탐색.

    주식 퍼프는 contractType 값이 "PERPETUAL"이 아닐 수 있어 따지지
    않는다. 단 ``_`` 포함 심볼(만기형 delivery 계약)은 제외.
    """
    return sorted(
        s["symbol"] for s in info
        if s.get("status") == "TRADING"
        and "_" not in s["symbol"]
        and is_tradfi_entry(s)
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


def _slope_pivots(sig_vals: List[float], swing_frac: float) -> List[tuple]:
    """시그널선 기울기 시리즈의 확정 극값(ZigZag 피벗) 목록.

    기울기의 극대 = 곡률 + → - 전환점 = "하락변곡",
    기울기의 극소 = 곡률 - → + 전환점 = "상승변곡".

    극값은 기울기가 반대 방향으로 ``th`` 이상 되돌렸을 때 확정된다.
    th 스케일은 max-min 대신 |기울기|의 90퍼센타일을 쓴다 — 극단봉
    하나가 창에 들고날 때마다 th가 출렁이며 피벗 분해 전체가 재편되는
    것(새로고침마다 변곡이 널뛰는 원인)을 막기 위함. 클린 사인파 기준
    2·p90(|s|) ≈ max-min 이라 기존 swing_frac 캘리브레이션은 유지된다.

    반환: [(sig 인덱스, 종류), ...] 시간순.
    """
    n = len(sig_vals)
    if n < 8:
        return []
    s = [sig_vals[i] - sig_vals[i - 1] for i in range(1, n)]
    a = sorted(abs(x) for x in s)
    p90 = a[min(len(a) - 1, int(0.9 * (len(a) - 1) + 0.5))]
    if p90 <= 0.0:
        return []
    th = 2.0 * p90 * swing_frac
    pivots: List[tuple] = []
    direction = 0                      # +1 기울기 상승 추적, -1 하락 추적
    cur_max, cur_max_i = s[0], 0
    cur_min, cur_min_i = s[0], 0
    for i in range(1, len(s)):
        v = s[i]
        if direction >= 0:
            if v > cur_max:
                cur_max, cur_max_i = v, i
            if cur_max - v >= th:      # 기울기 고점 확정 → 하락변곡
                pivots.append((cur_max_i + 1, "하락변곡"))  # s[j]는 sig[j+1] 시점
                direction = -1
                cur_min, cur_min_i = v, i
                continue
        if direction <= 0:
            if v < cur_min:
                cur_min, cur_min_i = v, i
            if v - cur_min >= th:      # 기울기 저점 확정 → 상승변곡
                pivots.append((cur_min_i + 1, "상승변곡"))
                direction = 1
                cur_max, cur_max_i = v, i
    return pivots


def _find_last_inflection(sig_vals: List[float], max_bars: int = 45,
                          swing_frac: float = 0.25, window: int = 50):
    """시그널선의 가장 최근 '유의미한' 수학적 변곡점.

    2차 도함수 부호 전환(= 기울기의 극대/극소) 중에서 전환 전후의
    기울기 변화량이 ``swing_frac`` 스케일 이상인 것만 변곡으로
    인정한다(기울기 시리즈에 대한 ZigZag). 안정성을 위해:

    - **마감봉만 사용**: 마지막(진행 중) 봉은 제외한다. 진행 봉의
      기울기는 스캔마다 흔들려 새로고침할 때마다 변곡 보고가 널뛰는
      원인이었다. 변곡은 새 봉이 닫힐 때만 갱신된다 (bars_ago 표시는
      진행 중 봉 기준으로 +1 보정).
    - ``window=50``: TSI(EMA 3중첩)의 웜업 왜곡 구간(99봉 fetch의 앞쪽
      ~50봉)을 기울기 스케일 계산에서 배제 — 전체 히스토리를 쓰는
      실제 차트(트레이딩뷰)와의 불일치를 줄인다.

    반환: (bars_ago, 종류) 또는 (-1, "").
    """
    closed = sig_vals[:-1] if len(sig_vals) >= 2 else sig_vals
    tail = closed[-window:] if len(closed) > window else closed
    off = len(closed) - len(tail)
    piv = _slope_pivots(tail, swing_frac)
    if not piv:
        return -1, ""
    idx, typ = piv[-1]
    bars_ago = (len(closed) - 1) - (idx + off) + 1  # 진행 중 봉 기준 N봉전
    if bars_ago > max_bars:
        return -1, ""
    return bars_ago, typ


def _state_from_closes(closes: List[float],
                       swing_frac: float = 0.25) -> Optional[TFState]:
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

    inf_bars, inf_type = _find_last_inflection(sig_vals, swing_frac=swing_frac)

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
                # 진행 중인 봉 포함 → 마감봉이 아닌 "스캔 시점" 실시간 값 기준
                drop_unclosed=False,
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


def scan_symbol(symbol: str, session=None, market: str = "futures",
                swing_frac: float = 0.25) -> SymbolScan:
    """Fetch klines for all four timeframes and compute TSI states.

    ``market`` selects the API ("futures" | "spot", see :data:`MARKETS`) —
    TradFi symbols may live on spot instead of USDT-M futures.
    ``swing_frac`` tunes inflection sensitivity (see _find_last_inflection).
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
            tf_states[tf] = _state_from_closes(closes, swing_frac)
        else:
            tf_states[tf] = None
    return SymbolScan(symbol=symbol, ts=latest_ts, tf=tf_states,
                      last_price=last_price)


def scan_all(
    symbols: List[str],
    max_workers: int = 6,
    progress_every: int = 50,
    markets: Optional[Dict[str, str]] = None,
    swing_frac: float = 0.25,
) -> List[SymbolScan]:
    """Scan all symbols concurrently and return results sorted by symbol name.

    ``markets`` maps symbol → "futures"/"spot" for symbols not on USDT-M
    futures (TradFi); unlisted symbols default to futures.
    ``swing_frac`` is forwarded to the inflection detector.

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
                            markets.get(sym, "futures"), swing_frac): sym
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
