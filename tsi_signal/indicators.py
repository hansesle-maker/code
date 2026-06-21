"""Pure-Python technical indicators for the TSI signal engine.

No third-party dependencies, so this module runs anywhere (including the
sandbox where pandas/numpy are not installed). Every function operates on
plain Python lists of floats.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple


def ema(values: Sequence[float], period: int) -> List[float]:
    """Exponential moving average, seeded with the first value.

    Matches TradingView's recursive ``ta.ema`` once enough warm-up bars are
    supplied: the seed's influence decays geometrically, so with a few
    hundred bars the result converges to TradingView's to within rounding.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    out: List[float] = []
    k = 2.0 / (period + 1.0)
    prev: Optional[float] = None
    for v in values:
        v = float(v)
        prev = v if prev is None else v * k + prev * (1.0 - k)
        out.append(prev)
    return out


def true_strength_index(
    close: Sequence[float],
    long_period: int = 25,
    short_period: int = 13,
    signal_period: int = 13,
) -> Tuple[List[float], List[float]]:
    """True Strength Index (TSI) and its signal line.

        TSI = 100 * EMA(EMA(momentum, long), short)
                  / EMA(EMA(|momentum|, long), short)

    where ``momentum = close - close[-1]``. Defaults (25 / 13 / 13) match
    TradingView's built-in TSI. Returns ``(tsi, signal)``; both lists are
    the same length as ``close`` and the first element is 0.0 by convention
    (no prior close to difference against).
    """
    n = len(close)
    if n == 0:
        return [], []

    momentum = [0.0] + [float(close[i] - close[i - 1]) for i in range(1, n)]
    abs_momentum = [abs(m) for m in momentum]

    double_smoothed = ema(ema(momentum, long_period), short_period)
    double_smoothed_abs = ema(ema(abs_momentum, long_period), short_period)

    tsi = [
        (100.0 * double_smoothed[i] / double_smoothed_abs[i])
        if double_smoothed_abs[i] != 0.0
        else 0.0
        for i in range(n)
    ]
    signal = ema(tsi, signal_period)
    return tsi, signal


def relative_strength(
    sym_now: float, sym_ref: float, bench_now: float, bench_ref: float
) -> Optional[float]:
    """Performance of a symbol relative to a benchmark since a reference bar.

        rs = (sym_now / sym_ref) / (bench_now / bench_ref) - 1

    Positive => the symbol outperformed the benchmark over the window
    (relative strength); negative => it underperformed. Returns ``None`` if
    any reference price is non-positive. ``sym_ref`` / ``bench_ref`` are the
    closes at the user-chosen start time, NOT an auto-detected swing point.
    """
    if sym_ref <= 0 or bench_ref <= 0 or bench_now <= 0:
        return None
    return (sym_now / sym_ref) / (bench_now / bench_ref) - 1.0
