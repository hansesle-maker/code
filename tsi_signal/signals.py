"""Trigger logic: turn candles into a LONG / SHORT / FLAT decision.

Two independent stages, matching the user's system:

1. Relative-strength GATE (vs BTC, measured from a MANUAL start time).
   Strength only decides *which direction is allowed* — it never enters a
   position by itself:
       stronger than BTC since the start time  -> long permitted
       weaker  than BTC since the start time   -> short permitted
   The start time is supplied by you per symbol (e.g. a prior low/high, but
   that choice is yours); this module just receives the symbol/benchmark
   closes at that time.

2. TSI TRIGGER (the symbol's own 4h & 1h TSI) makes the actual entry call:
       long  : 4h TSI direction up   AND 1h TSI >= +threshold
       short : 4h TSI direction down  AND 1h TSI <= -threshold

A position is taken only when the gate permits the direction *and* the
trigger fires; otherwise FLAT. Every knob lives in :class:`SignalParams`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from .data import Candle
from .indicators import relative_strength, true_strength_index


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


@dataclass
class SignalParams:
    tsi_long: int = 25
    tsi_short: int = 13
    tsi_signal: int = 13
    slope_lookback: int = 1  # bars used to measure the 4h TSI direction
    one_h_threshold: float = 0.0  # long needs 1h TSI >= +t ; short needs <= -t
    require_reversal: bool = False  # if True the 4h TSI must *turn* this bar
    require_ref: bool = False  # if True, a missing RS start time blocks signals
    benchmark: str = "BTCUSDT"


@dataclass
class SymbolSignal:
    symbol: str
    direction: Direction
    rs_vs_bench: Optional[float]  # fraction since the start time; None if unavailable
    gate: str  # 'long' | 'short' | 'both' | 'neutral' | 'n/a'
    tsi_4h: float
    tsi_4h_slope: int  # +1 up, -1 down, 0 flat
    tsi_1h: float
    note: str = ""


def _sign(x: float, eps: float = 1e-12) -> int:
    if x > eps:
        return 1
    if x < -eps:
        return -1
    return 0


def _slope(series: List[float], lookback: int) -> int:
    if len(series) <= lookback:
        return 0
    return _sign(series[-1] - series[-1 - lookback])


def _turned_up(series: List[float]) -> bool:
    return len(series) >= 3 and series[-1] > series[-2] and series[-2] <= series[-3]


def _turned_down(series: List[float]) -> bool:
    return len(series) >= 3 and series[-1] < series[-2] and series[-2] >= series[-3]


def evaluate_symbol(
    symbol: str,
    candles_4h: List[Candle],
    candles_1h: List[Candle],
    sym_ref_close: Optional[float],
    bench_ref_close: Optional[float],
    bench_now_close: Optional[float],
    params: SignalParams,
    is_benchmark: bool = False,
) -> SymbolSignal:
    """Evaluate one symbol and return its target :class:`Direction`.

    ``sym_ref_close`` / ``bench_ref_close`` are the symbol's and benchmark's
    closes at the user's chosen start time; ``bench_now_close`` is the
    benchmark's latest close. For the benchmark itself the relative-strength
    gate is skipped and the TSI trigger decides alone.
    """
    close_4h = [c.close for c in candles_4h]
    close_1h = [c.close for c in candles_1h]

    tsi4, _ = true_strength_index(close_4h, params.tsi_long, params.tsi_short, params.tsi_signal)
    tsi1, _ = true_strength_index(close_1h, params.tsi_long, params.tsi_short, params.tsi_signal)
    last_tsi4 = tsi4[-1] if tsi4 else 0.0
    last_tsi1 = tsi1[-1] if tsi1 else 0.0
    slope4 = _slope(tsi4, params.slope_lookback)

    # --- TSI trigger (symbol's own 4h direction + 1h level) ---------------
    if params.require_reversal:
        bull_4h, bear_4h = _turned_up(tsi4), _turned_down(tsi4)
    else:
        bull_4h, bear_4h = slope4 > 0, slope4 < 0
    long_trigger = bull_4h and last_tsi1 >= params.one_h_threshold
    short_trigger = bear_4h and last_tsi1 <= -params.one_h_threshold

    # --- relative-strength gate (vs BTC, from the manual start time) ------
    rs: Optional[float] = None
    if not is_benchmark and sym_ref_close and bench_ref_close and bench_now_close:
        rs = relative_strength(close_4h[-1], sym_ref_close, bench_now_close, bench_ref_close)

    note = ""
    if is_benchmark:
        gate, long_ok, short_ok = "both", True, True  # strength vs itself undefined
    elif rs is None:
        if params.require_ref:
            gate, long_ok, short_ok = "n/a", False, False
            note = "no RS start time"
        else:
            gate, long_ok, short_ok = "both", True, True
            note = "no RS start time (gate skipped)"
    elif rs > 0:
        gate, long_ok, short_ok = "long", True, False
    elif rs < 0:
        gate, long_ok, short_ok = "short", False, True
    else:
        gate, long_ok, short_ok = "neutral", False, False

    direction = Direction.FLAT
    if long_ok and long_trigger:
        direction = Direction.LONG
    elif short_ok and short_trigger:
        direction = Direction.SHORT

    return SymbolSignal(
        symbol=symbol,
        direction=direction,
        rs_vs_bench=rs,
        gate=gate,
        tsi_4h=last_tsi4,
        tsi_4h_slope=slope4,
        tsi_1h=last_tsi1,
        note=note,
    )
