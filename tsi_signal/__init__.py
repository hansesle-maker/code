"""TSI-based long/short signal engine (Phase 1: signal generation only).

Public surface:
    - :class:`~tsi_signal.signals.SignalParams` / :class:`~tsi_signal.signals.Direction`
    - :func:`~tsi_signal.engine.run_engine` and the formatting helpers
    - :func:`~tsi_signal.data.fetch_klines` (live) / :func:`~tsi_signal.data.synthetic_candles`
"""
from .data import Candle, fetch_klines, synthetic_candles, trend_closes
from .engine import (
    Row,
    SymbolConfig,
    format_table,
    load_symbols,
    run_engine,
    to_csv,
)
from .signals import Direction, SignalParams, SymbolSignal, evaluate_symbol

__all__ = [
    "Candle",
    "Direction",
    "Row",
    "SignalParams",
    "SymbolConfig",
    "SymbolSignal",
    "evaluate_symbol",
    "fetch_klines",
    "format_table",
    "load_symbols",
    "run_engine",
    "synthetic_candles",
    "to_csv",
    "trend_closes",
]
