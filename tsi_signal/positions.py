"""Stateful position tracking + layered exit signals on top of the scanner.

The scanner itself is stateless — it re-reads the whole market every 15 min.
This module remembers which symbols we "entered" (auto-registered when an entry
trigger fires: 3/3 aligned + a fresh 15m signal cross) and, on every later
scan, re-evaluates each open position to emit *layered* exit signals:

    🚨 DISASTER  price moved ≥ disaster_pct against entry (gap / news guard)
    🔴 CLOSE     1h TSI crossed signal against the position (trend turned)
    ❌ STOP      15m crossed against us while still underwater (thesis void)
    ⚠️ WARN      15m crossed against us but in profit (take-profit heads-up)

Only DISASTER / CLOSE / STOP actually close the position; WARN keeps it open
(it's just an early "momentum is fading" notice). The disaster stop is loose on
purpose — normally it never triggers and exits are driven purely by TSI logic;
it only catches sudden gap/news crashes. Set ``disaster_pct=0`` to disable it.

State persists in ``positions.json`` so it survives between 15-min runs (the
Flask server keeps it on disk; the GitHub Actions workflow commits it).
"""
from __future__ import annotations

import datetime
import json
import os
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

from . import alerts as _al
from .scanner import SymbolScan

# Default disaster-stop distance from entry (fraction). Loose on purpose.
DEFAULT_DISASTER_PCT = 0.06

# Keep only the most recent N closed trades in the state file.
MAX_CLOSED_HISTORY = 200


@dataclass
class Position:
    symbol: str
    side: str            # "long" / "short"
    entry_price: float
    entry_ts: int        # epoch ms of the entry bar
    entry_at: str        # ISO timestamp (UTC) for humans

    def pnl_pct(self, price: float) -> float:
        """Signed P&L percent for this side (positive = in profit)."""
        if self.entry_price <= 0:
            return 0.0
        raw = (price - self.entry_price) / self.entry_price * 100.0
        return raw if self.side == "long" else -raw


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state(path: Optional[str]) -> dict:
    """Load ``{"open": {symbol: pos}, "closed": [...]}``; empty if missing."""
    if not path or not os.path.exists(path):
        return {"open": {}, "closed": []}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {"open": {}, "closed": []}
    data.setdefault("open", {})
    data.setdefault("closed", [])
    return data


def save_state(path: str, state: dict) -> None:
    """Persist state, trimming closed-trade history to the last N entries."""
    state["closed"] = state.get("closed", [])[-MAX_CLOSED_HISTORY:]
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)   # atomic-ish write so a crash can't truncate it


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_price(p: float) -> str:
    """Human-friendly price across BTC (62000) … SHIB (0.0000123)."""
    if p >= 10:
        return f"{p:,.2f}"
    if p >= 0.1:
        return f"{p:.4f}"
    return f"{p:.8f}".rstrip("0").rstrip(".")


def _line(pos: Position, price: float, pnl: float) -> str:
    side = "LONG" if pos.side == "long" else "SHORT"
    return (f"{pos.symbol} {side}  진입 {_fmt_price(pos.entry_price)} → "
            f"현재 {_fmt_price(price)} ({pnl:+.1f}%)")


# ---------------------------------------------------------------------------
# Entry registration / exit evaluation
# ---------------------------------------------------------------------------

def register_entries(
    state: dict, results: List[SymbolScan], scanned_at: datetime.datetime
) -> List[Position]:
    """Auto-register symbols that just fired an entry trigger.

    Entry = fully aligned (3/3) on the same side AS a fresh 15m signal cross.
    Symbols already being tracked are skipped. Returns the newly opened ones.
    """
    opened: List[Position] = []
    open_map = state["open"]
    for r in results:
        if r.symbol in open_map:
            continue                       # already tracking this symbol
        s15 = r.tf.get("15m")
        if not s15 or not r.last_price:
            continue
        if r.bull_score == 3 and s15.fresh_cross == 1:
            side = "long"
        elif r.bear_score == 3 and s15.fresh_cross == -1:
            side = "short"
        else:
            continue
        pos = Position(
            symbol=r.symbol,
            side=side,
            entry_price=r.last_price,
            entry_ts=r.ts,
            entry_at=scanned_at.replace(microsecond=0).isoformat() + "Z",
        )
        open_map[r.symbol] = asdict(pos)
        opened.append(pos)
    return opened


def _close(state: dict, pos: Position, price: float, pnl: float,
           reason: str, scanned_at: datetime.datetime) -> None:
    state["open"].pop(pos.symbol, None)
    rec = asdict(pos)
    rec.update({
        "exit_price": price,
        "pnl_pct": round(pnl, 2),
        "reason": reason,
        "closed_at": scanned_at.replace(microsecond=0).isoformat() + "Z",
    })
    state["closed"].append(rec)


def evaluate_exits(
    state: dict,
    results: List[SymbolScan],
    scanned_at: datetime.datetime,
    disaster_pct: float = DEFAULT_DISASTER_PCT,
) -> Dict[str, List[str]]:
    """Evaluate every open position and return ``{exit_category: [lines]}``.

    Closes positions that hit DISASTER / CLOSE / STOP (mutating ``state``);
    WARN is reported but the position stays open.
    """
    groups: Dict[str, List[str]] = {c: [] for c in _al.EXIT_CATEGORIES}
    by_symbol = {r.symbol: r for r in results}

    for symbol in list(state["open"].keys()):
        pos = Position(**state["open"][symbol])
        r = by_symbol.get(symbol)
        if not r or not r.last_price:
            continue                       # no fresh data this scan — wait
        price = r.last_price
        pnl = pos.pnl_pct(price)
        s15 = r.tf.get("15m")
        s1h = r.tf.get("1h")
        adverse = -1 if pos.side == "long" else 1  # cross direction against us

        if disaster_pct and pnl <= -abs(disaster_pct) * 100.0:
            reason = "disaster"
        elif s1h and s1h.fresh_cross == adverse:
            reason = "close"
        elif s15 and s15.fresh_cross == adverse:
            reason = "stop" if pnl < 0 else "warn"
        else:
            continue                       # thesis intact — keep holding

        line = _line(pos, price, pnl)
        title = {
            "disaster": _al.EXIT_DISASTER,
            "close": _al.EXIT_CLOSE,
            "stop": _al.EXIT_STOP,
            "warn": _al.EXIT_WARN,
        }[reason]
        groups[title].append(line)

        if reason != "warn":               # WARN keeps the position open
            _close(state, pos, price, pnl, reason, scanned_at)

    return groups


def open_positions_view(state: dict, results: List[SymbolScan]) -> List[dict]:
    """Snapshot of open positions with live P&L, for the dashboard / API."""
    by_symbol = {r.symbol: r for r in results}
    view: List[dict] = []
    for symbol, raw in state.get("open", {}).items():
        pos = Position(**raw)
        r = by_symbol.get(symbol)
        price = r.last_price if (r and r.last_price) else pos.entry_price
        view.append({
            "symbol": pos.symbol,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "entry_str": _fmt_price(pos.entry_price),
            "entry_at": pos.entry_at,
            "price": price,
            "price_str": _fmt_price(price),
            "pnl_pct": round(pos.pnl_pct(price), 2),
        })
    view.sort(key=lambda v: v["pnl_pct"], reverse=True)
    return view
