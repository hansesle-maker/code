"""CVD (Cumulative Volume Delta) divergence detector.

Python port of TradingFinder's Pine v5 indicator
"Cumulative Volume Delta Divergence [TradingFinder] Periodic EMA"
(CVD Divergence Oscillator), faithful to its execution model:

    Buying  = volume * (close - low)  / (high - low)
    Selling = volume * (high - close) / (high - low)
    delta   = Buying - Selling
    Hist    = sum(delta, Period)          # 'Periodic' mode (default)
            | ema(delta, Period)          # 'EMA' mode

Fractals are ``ta.pivothigh(n, n)`` / ``ta.pivotlow(n, n)`` gated by a trend
filter (``close[n]`` vs ``ema(close, 50)``), and a divergence is confirmed
when two consecutive same-side fractals disagree between price and Hist:

    bearish (-RD): price higher-high  +  Hist lower-high,  both Hist > 0
    bullish (+RD): price lower-low    +  Hist higher-low,  both Hist < 0

with Pine's two guards: the gap between the two pivots must be < 30 bars and
the newest pivot must be within 30 bars of the current bar.

Pure Python (no third-party deps) so it runs anywhere the rest of the
engine does and can be exercised offline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .data import Candle
from .indicators import ema

BULL = "강세"   # +RD, bullish regular divergence
BEAR = "약세"   # -RD, bearish regular divergence

# Pine input defaults
DEF_N = 2            # Divergence Fractal Periods
DEF_PERIOD = 21      # CVD Period
DEF_MODE = "periodic"  # 'Periodic' | 'ema'
DEF_EMA_LEN = 50     # trend filter length (hardcoded in Pine)
DEF_MAX_GAP = 30     # max bars between the two pivots (hardcoded in Pine)
DEF_WINDOW = 30      # divergence stays "live" this many bars (hardcoded)

# Minimum bars for a meaningful read: EMA warmup + Hist warmup + divergence window.
MIN_BARS = 90


@dataclass
class CVDSignal:
    """Most recent confirmed divergence on one timeframe."""
    direction: str          # BULL ("강세") | BEAR ("약세")
    bars_ago: int           # pivot distance from the current (forming) bar
    strength: int           # consecutive divergent fractals: 1=일반 2=양호 3+=강함
    phase: bool             # Hist crossed zero between the two pivots
    active: bool            # Pine's Time_Condition still true on the last bar
    hist: float             # Hist on the last closed bar
    pivot_price: float      # newest pivot's price
    prev_price: float       # previous pivot's price
    pivot_hist: float       # newest pivot's Hist
    prev_hist: float        # previous pivot's Hist


def strength_label(k: int) -> str:
    """Pine's text_power_* mapping, in Korean."""
    if k >= 3:
        return "강함"
    if k == 2:
        return "양호"
    if k == 1:
        return "일반"
    return "-"


def cvd_delta(candles: Sequence[Candle]) -> List[float]:
    """Per-bar volume delta. Zero-range bars carry no directional info."""
    out: List[float] = []
    for c in candles:
        rng = c.high - c.low
        if rng <= 0.0:
            out.append(0.0)
            continue
        buying = c.volume * (c.close - c.low) / rng
        selling = c.volume * (c.high - c.close) / rng
        out.append(buying - selling)
    return out


def cvd_hist(candles: Sequence[Candle], period: int = DEF_PERIOD,
             mode: str = DEF_MODE) -> List[Optional[float]]:
    """The oscillator histogram. ``None`` marks Pine's ``na`` warmup bars."""
    d = cvd_delta(candles)
    if mode == "ema":
        return list(ema(d, period))  # type: ignore[arg-type]
    out: List[Optional[float]] = [None] * len(d)
    run = 0.0
    for i, v in enumerate(d):
        run += v
        if i >= period:
            run -= d[i - period]
        if i >= period - 1:
            out[i] = run
    return out


def _is_pivot_high(vals: Sequence[float], i: int, n: int) -> bool:
    """``ta.pivothigh(n, n)``: strict local max with ``n`` bars each side.

    Strict on both sides, so a flat top yields no pivot (conservative —
    TradingView's tie handling on plateaus can differ marginally).
    """
    if i - n < 0 or i + n >= len(vals):
        return False
    c = vals[i]
    for j in range(i - n, i):
        if not c > vals[j]:
            return False
    for j in range(i + 1, i + n + 1):
        if not c > vals[j]:
            return False
    return True


def _is_pivot_low(vals: Sequence[float], i: int, n: int) -> bool:
    if i - n < 0 or i + n >= len(vals):
        return False
    c = vals[i]
    for j in range(i - n, i):
        if not c < vals[j]:
            return False
    for j in range(i + 1, i + n + 1):
        if not c < vals[j]:
            return False
    return True


def _phase_flag(hist: Sequence[Optional[float]], t: int, n: int,
                len_back: int, want_negative: bool) -> bool:
    """Pine's Bear_Phase / Bull_Phase loop:  i = (n+1) … (n+1)+len_back,
    testing ``Hist[i] < 0`` (bear) or ``Hist[i] > 0`` (bull) — i.e. whether
    Hist crossed zero between the two pivots."""
    for i in range(n + 1, n + 1 + max(1, len_back) + 1):
        k = t - i
        if k < 0:
            break
        h = hist[k]
        if h is None:
            continue
        if (h < 0.0) if want_negative else (h > 0.0):
            return True
    return False


def scan_divergence(
    candles: Sequence[Candle],
    n: int = DEF_N,
    period: int = DEF_PERIOD,
    mode: str = DEF_MODE,
    ema_len: int = DEF_EMA_LEN,
    max_gap: int = DEF_MAX_GAP,
    window: int = DEF_WINDOW,
) -> Optional[CVDSignal]:
    """Return the most recent confirmed divergence, or ``None``.

    ``candles`` must be closed bars, oldest first. Pine's per-bar execution
    is replicated: fractals confirm ``n`` bars after the pivot, so the
    freshest possible signal sits ``n`` bars back.

    ``bars_ago`` counts from the current (forming) bar — the last closed bar
    is 1봉전 — matching the TSI dashboard's convention.
    """
    N = len(candles)
    if N < max(MIN_BARS, ema_len + period + 2 * n + 2):
        return None

    hist = cvd_hist(candles, period, mode)
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    ema_c = ema(closes, ema_len)

    # (pivot_index, price, hist_at_pivot) for each confirmed fractal
    ups: List[Tuple[int, float, Optional[float]]] = []
    downs: List[Tuple[int, float, Optional[float]]] = []

    latest: Optional[CVDSignal] = None
    c_bear = c_bull = 0

    for t in range(N):
        p = t - n  # the pivot candidate this bar would confirm

        # ── bearish side ────────────────────────────────────────────────
        new_up = False
        if p >= 0 and _is_pivot_high(highs, p, n) and closes[p] > ema_c[t]:
            if not ups or ups[-1][0] != p:
                ups.append((p, highs[p], hist[p]))
                new_up = True

        bear = False
        if len(ups) >= 2:
            (lb, lp, lh), (pb, pp, ph) = ups[-1], ups[-2]
            if (lh is not None and ph is not None and lh > 0.0 and ph > 0.0
                    and (lb + window) > t and (lb - pb) < max_gap):
                bear = (lp > pp) and (lh < ph)
        if bear and new_up and ups[-1][1] != ups[-2][1]:
            c_bear += 1
            (lb, lp, lh), (pb, pp, ph) = ups[-1], ups[-2]
            latest = CVDSignal(
                direction=BEAR, bars_ago=lb, strength=c_bear,
                phase=_phase_flag(hist, t, n, lb - pb, want_negative=True),
                active=True, hist=0.0,
                pivot_price=lp, prev_price=pp,
                pivot_hist=lh, prev_hist=ph,
            )
        elif not bear:
            c_bear = 0

        # ── bullish side ────────────────────────────────────────────────
        new_dn = False
        if p >= 0 and _is_pivot_low(lows, p, n) and closes[p] < ema_c[t]:
            if not downs or downs[-1][0] != p:
                downs.append((p, lows[p], hist[p]))
                new_dn = True

        bull = False
        if len(downs) >= 2:
            (lb, lp, lh), (pb, pp, ph) = downs[-1], downs[-2]
            if (lh is not None and ph is not None and lh < 0.0 and ph < 0.0
                    and (lb + window) > t and (lb - pb) < max_gap):
                bull = (lp < pp) and (lh > ph)
        if bull and new_dn and downs[-1][1] != downs[-2][1]:
            c_bull += 1
            (lb, lp, lh), (pb, pp, ph) = downs[-1], downs[-2]
            latest = CVDSignal(
                direction=BULL, bars_ago=lb, strength=c_bull,
                phase=_phase_flag(hist, t, n, lb - pb, want_negative=False),
                active=True, hist=0.0,
                pivot_price=lp, prev_price=pp,
                pivot_hist=lh, prev_hist=ph,
            )
        elif not bull:
            c_bull = 0

    if latest is None:
        return None

    # bars_ago held the absolute pivot index during the walk; convert to a
    # distance from the current forming bar (last closed bar = 1봉전).
    pivot_idx = latest.bars_ago
    latest.bars_ago = (N - 1) - pivot_idx + 1
    latest.active = (pivot_idx + window) > (N - 1)
    last_hist = hist[-1]
    latest.hist = round(last_hist, 4) if last_hist is not None else 0.0
    return latest
