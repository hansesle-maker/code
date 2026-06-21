"""Trigger logic: turn candles into a LONG / SHORT / FLAT decision plus a
conviction-scaled size.

Two independent stages, matching the user's system:

1. Relative-strength GATE (vs BTC, from a MANUAL start time) decides which
   *direction is allowed* (stronger than BTC -> long-only, weaker ->
   short-only). It never enters a position by itself.

2. TSI TRIGGER reads each timeframe's TSI through TWO reference lines instead
   of a raw 1-bar slope (which is noisy and context-blind):
       - zero line   : TSI > 0 bullish regime / < 0 bearish regime
       - signal line : TSI > signal momentum up / < signal momentum down
   giving a 4-level state per timeframe:
       +2  TSI>0 and TSI>signal   (confirmed up)
       +1  TSI<0 and TSI>signal   (turning up, still below zero)
       -1  TSI>0 and TSI<signal   (rolling over, still above zero)
       -2  TSI<0 and TSI<signal   (confirmed down)

Default ("confirmed"): long needs 4h state == +2 AND 1h above its signal
line; short is the mirror. With ``require_zero_4h=False`` ("aggressive") a 4h
state of +1/-1 also qualifies. The position SIZE scales with conviction =
4h state + 1h state (e.g. +-4 full, +-3 partial) via ``size_by_conviction``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

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
    require_zero_4h: bool = True  # confirmed: 4h must also be on the right side of zero
    require_zero_1h: bool = False  # 1h must also agree with zero (stricter timing)
    hysteresis: bool = False  # hold through weak states; exit only on reversal/gate flip
    exit_state_4h: int = -2  # with hysteresis, exit a long once 4h state <= this (short mirror)
    long_only: bool = False  # never take short positions
    short_only: bool = False  # never take long positions
    require_ref: bool = False  # if True, a missing RS start time blocks signals
    benchmark: str = "BTCUSDT"
    # conviction (|4h state + 1h state|) -> fraction of target notional
    size_by_conviction: Dict[int, float] = field(
        default_factory=lambda: {4: 1.0, 3: 0.6, 2: 0.3}
    )


@dataclass
class SymbolSignal:
    symbol: str
    direction: Direction
    rs_vs_bench: Optional[float]  # fraction since the start time; None if unavailable
    gate: str  # 'long' | 'short' | 'both' | 'neutral' | 'n/a'
    tsi_4h: float
    state_4h: int  # +2 / +1 / -1 / -2
    tsi_1h: float
    state_1h: int
    conviction: int  # state_4h + state_1h (signed)
    size_fraction: float  # 0..1, scales the target notional
    note: str = ""


def tsi_state(tsi: float, signal: float) -> int:
    """4-level momentum read from the zero line and the signal line.

    +2 (TSI>0 & TSI>signal), +1 (TSI<0 & TSI>signal),
    -1 (TSI>0 & TSI<signal), -2 (TSI<0 & TSI<signal).
    """
    if tsi > signal:
        return 2 if tsi > 0 else 1
    return -2 if tsi < 0 else -1


def _is_bull(state: int, require_zero: bool) -> bool:
    return state == 2 or (state == 1 and not require_zero)


def _is_bear(state: int, require_zero: bool) -> bool:
    return state == -2 or (state == -1 and not require_zero)


def decide(
    state_4h: int,
    state_1h: int,
    long_ok: bool,
    short_ok: bool,
    params: SignalParams,
    prev_direction: Direction = Direction.FLAT,
    prev_size: float = 0.0,
) -> Tuple[Direction, float, int]:
    """Core rule shared by the live engine and the backtester: turn the two
    timeframe states + the gate's permission into (direction, size, conviction).

    With ``params.hysteresis`` the entry is strict (a confirmed state) but an
    open position is HELD through weak states (e.g. a transient -1 in a strong
    uptrend) and only exited when the 4h state reverses to ``exit_state_4h`` or
    the gate flips. ``prev_direction``/``prev_size`` carry the current position
    (the live engine reads them from your current_position).
    """
    long_ok = long_ok and not params.short_only
    short_ok = short_ok and not params.long_only
    bull_4h = _is_bull(state_4h, params.require_zero_4h)
    bear_4h = _is_bear(state_4h, params.require_zero_4h)
    bull_1h = _is_bull(state_1h, params.require_zero_1h)
    bear_1h = _is_bear(state_1h, params.require_zero_1h)
    long_entry = long_ok and bull_4h and bull_1h
    short_entry = short_ok and bear_4h and bear_1h
    conviction = state_4h + state_1h

    if not params.hysteresis:
        direction = Direction.LONG if long_entry else (
            Direction.SHORT if short_entry else Direction.FLAT)
        size = params.size_by_conviction.get(abs(conviction), 0.0) if direction is not Direction.FLAT else 0.0
        return direction, size, conviction

    long_exit = state_4h <= params.exit_state_4h or not long_ok
    short_exit = state_4h >= -params.exit_state_4h or not short_ok
    if prev_direction is Direction.LONG:
        direction = Direction.SHORT if short_entry else (Direction.FLAT if long_exit else Direction.LONG)
    elif prev_direction is Direction.SHORT:
        direction = Direction.LONG if long_entry else (Direction.FLAT if short_exit else Direction.SHORT)
    else:
        direction = Direction.LONG if long_entry else (Direction.SHORT if short_entry else Direction.FLAT)

    if direction is Direction.FLAT:
        size = 0.0
    elif direction is prev_direction:  # holding -> keep the size we entered with
        size = prev_size
    else:  # fresh entry or switch -> size from current conviction
        size = params.size_by_conviction.get(abs(conviction), 0.0)
    return direction, size, conviction


def evaluate_symbol(
    symbol: str,
    candles_4h: List[Candle],
    candles_1h: List[Candle],
    sym_ref_close: Optional[float],
    bench_ref_close: Optional[float],
    bench_now_close: Optional[float],
    params: SignalParams,
    is_benchmark: bool = False,
    prev_direction: Direction = Direction.FLAT,
    prev_size: float = 0.0,
) -> SymbolSignal:
    """Evaluate one symbol -> direction + conviction-scaled size.

    ``sym_ref_close`` / ``bench_ref_close`` are the symbol's and benchmark's
    closes at the user's chosen start time; ``bench_now_close`` is the
    benchmark's latest close. For the benchmark itself the relative-strength
    gate is skipped and the TSI trigger decides alone.
    """
    close_4h = [c.close for c in candles_4h]
    close_1h = [c.close for c in candles_1h]

    tsi4, sig4 = true_strength_index(close_4h, params.tsi_long, params.tsi_short, params.tsi_signal)
    tsi1, sig1 = true_strength_index(close_1h, params.tsi_long, params.tsi_short, params.tsi_signal)
    last_tsi4 = tsi4[-1] if tsi4 else 0.0
    last_sig4 = sig4[-1] if sig4 else 0.0
    last_tsi1 = tsi1[-1] if tsi1 else 0.0
    last_sig1 = sig1[-1] if sig1 else 0.0

    state4 = tsi_state(last_tsi4, last_sig4)
    state1 = tsi_state(last_tsi1, last_sig1)

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

    direction, size_fraction, conviction = decide(
        state4, state1, long_ok, short_ok, params, prev_direction, prev_size)

    return SymbolSignal(
        symbol=symbol,
        direction=direction,
        rs_vs_bench=rs,
        gate=gate,
        tsi_4h=last_tsi4,
        state_4h=state4,
        tsi_1h=last_tsi1,
        state_1h=state1,
        conviction=conviction,
        size_fraction=size_fraction,
        note=note,
    )
