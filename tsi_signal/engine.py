"""Engine: load the symbol list, fetch candles, evaluate triggers, and emit
the "to-be" position table that replaces the manual chart-reading step.

Phase 1 is execution-free (semi-automatic): it reports the target position
(direction x conviction-scaled size) and the delta from your current one; you
place the orders on Binance.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .data import Candle, Fetcher
from .signals import Direction, SignalParams, evaluate_symbol

# Korea Standard Time has no DST, so a fixed +9 offset is exact (and needs no
# tzdata, which Windows lacks). Naive ref_time values are interpreted as KST.
KST = timezone(timedelta(hours=9))


@dataclass
class SymbolConfig:
    symbol: str
    asset_class: str = "crypto"
    current_position: float = 0.0  # signed: +long / -short, in notional units
    target_notional: Optional[float] = None  # full-size notional; falls back to default
    ref_time_ms: Optional[int] = None  # manual RS start time (epoch ms)


@dataclass
class Row:
    symbol: str
    asset_class: str
    rs_vs_bench: Optional[float]
    gate: str
    tsi_4h: float
    state_4h: int
    tsi_1h: float
    state_1h: int
    conviction: int
    size_fraction: float
    direction: Direction
    current_position: float
    target_position: float
    delta: float
    action: str
    note: str = ""


def parse_time(value: str, default_tz: timezone = KST) -> Optional[int]:
    """Parse a manual reference time into epoch milliseconds.

    Accepts epoch seconds / milliseconds, or ISO-8601 (``2026-06-01``,
    ``2026-06-01 08:00``, ``2026-06-01T08:00:00Z``). Naive times use
    ``default_tz`` (KST by default); add an explicit offset (``Z`` or
    ``+00:00`` for UTC) to override. A leading apostrophe (Excel's "store as
    text" prefix) and ``/`` date separators are tolerated.
    """
    s = (value or "").strip().lstrip("'").strip()
    if not s:
        return None
    if s.isdigit():
        v = int(s)
        return v * 1000 if len(s) == 10 else v  # 10 digits => seconds
    iso = s.replace("/", "-").replace("Z", "+00:00")
    if "T" not in iso and " " in iso:
        iso = iso.replace(" ", "T", 1)
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return int(dt.timestamp() * 1000)


def load_symbols(path: str) -> List[SymbolConfig]:
    """Read the watch-list CSV (the file that replaces the Excel sheet).

    Leading ``#`` comment lines and blank lines are skipped, so the real
    header row is used (not a comment).
    """
    out: List[SymbolConfig] = []
    with open(path, newline="", encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    for raw in csv.DictReader(lines):
        row = {
            k.strip().lower(): (v.strip() if isinstance(v, str) else "")
            for k, v in raw.items()
            if k is not None
        }
        symbol = row.get("symbol", "")
        if not symbol:
            continue
        tn = row.get("target_notional", "")
        out.append(
            SymbolConfig(
                symbol=symbol.upper(),
                asset_class=row.get("asset_class") or "crypto",
                current_position=float(row.get("current_position") or 0.0),
                target_notional=float(tn) if tn else None,
                ref_time_ms=parse_time(row.get("ref_time", "")),
            )
        )
    return out


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def _action(current: float, target: float) -> str:
    cur, tgt = _sign(current), _sign(target)
    if cur == tgt:
        if abs(target - current) < 1e-9:
            return "HOLD"
        return "ADD" if abs(target) > abs(current) else "REDUCE"
    if tgt == 0:
        return "EXIT"
    if cur == 0:
        return "ENTER_LONG" if target > 0 else "ENTER_SHORT"
    return "SWITCH_TO_LONG" if target > 0 else "SWITCH_TO_SHORT"


def _signed_target(direction: Direction, size_fraction: float, notional: float) -> float:
    mag = abs(notional) * size_fraction
    if direction is Direction.LONG:
        return mag
    if direction is Direction.SHORT:
        return -mag
    return 0.0


def _ref_close(
    candles: List[Candle], ref_time_ms: Optional[int]
) -> Tuple[Optional[int], Optional[float], str]:
    """Return (open_time, close, note) of the first bar at/after the start time."""
    if ref_time_ms is None or not candles:
        return None, None, ""
    if ref_time_ms < candles[0].open_time:
        return None, None, "start time precedes fetched window (raise --limit)"
    for c in candles:
        if c.open_time >= ref_time_ms:
            return c.open_time, c.close, ""
    return None, None, "start time is in the future"


def run_engine(
    symbols: List[SymbolConfig],
    fetch: Fetcher,
    params: SignalParams,
    default_notional: float = 1000.0,
    kline_limit: int = 1000,
) -> List[Row]:
    """Evaluate every symbol and return one :class:`Row` each.

    ``fetch`` abstracts the data source so the same engine runs against live
    Binance klines or synthetic candles (demo/tests).
    """
    bench_4h = fetch(params.benchmark, "4h", kline_limit)
    bench_by_time: Dict[int, float] = {c.open_time: c.close for c in bench_4h}
    bench_now: Optional[float] = bench_4h[-1].close if bench_4h else None

    rows: List[Row] = []
    for cfg in symbols:
        is_bench = cfg.symbol == params.benchmark
        try:
            candles_4h = bench_4h if is_bench else fetch(cfg.symbol, "4h", kline_limit)
            candles_1h = fetch(cfg.symbol, "1h", kline_limit)

            ref_ot, sym_ref, ref_note = _ref_close(candles_4h, cfg.ref_time_ms)
            bench_ref = bench_by_time.get(ref_ot) if ref_ot is not None else None

            sig = evaluate_symbol(
                cfg.symbol, candles_4h, candles_1h,
                sym_ref_close=sym_ref, bench_ref_close=bench_ref,
                bench_now_close=bench_now, params=params, is_benchmark=is_bench,
            )
            note = "; ".join(n for n in (ref_note, sig.note) if n)
        except Exception as exc:  # one bad symbol must not sink the whole run
            rows.append(
                Row(cfg.symbol, cfg.asset_class, None, "n/a", 0.0, 0, 0.0, 0, 0, 0.0,
                    Direction.FLAT, cfg.current_position, cfg.current_position,
                    0.0, "ERROR", f"{type(exc).__name__}: {exc}")
            )
            continue

        notional = cfg.target_notional if cfg.target_notional is not None else default_notional
        target = _signed_target(sig.direction, sig.size_fraction, notional)
        rows.append(
            Row(
                symbol=cfg.symbol, asset_class=cfg.asset_class, rs_vs_bench=sig.rs_vs_bench,
                gate=sig.gate, tsi_4h=sig.tsi_4h, state_4h=sig.state_4h,
                tsi_1h=sig.tsi_1h, state_1h=sig.state_1h, conviction=sig.conviction,
                size_fraction=sig.size_fraction, direction=sig.direction,
                current_position=cfg.current_position, target_position=target,
                delta=target - cfg.current_position,
                action=_action(cfg.current_position, target), note=note,
            )
        )
    return rows


def format_table(rows: List[Row]) -> str:
    """Render rows as an aligned, human-readable to-be table."""
    header = [
        "SYMBOL", "RS%vsBTC", "GATE", "TSI4h", "St4h", "TSI1h", "St1h",
        "CONV", "SIGNAL", "SIZE%", "CUR", "TARGET", "DELTA", "ACTION", "NOTE",
    ]
    lines = [header]
    for r in rows:
        rs = "-" if r.rs_vs_bench is None else f"{r.rs_vs_bench * 100:+.2f}"
        lines.append([
            r.symbol, rs, r.gate, f"{r.tsi_4h:+.1f}", f"{r.state_4h:+d}",
            f"{r.tsi_1h:+.1f}", f"{r.state_1h:+d}", f"{r.conviction:+d}",
            r.direction.value, f"{r.size_fraction * 100:.0f}",
            f"{r.current_position:g}", f"{r.target_position:g}",
            f"{r.delta:+g}", r.action, r.note,
        ])
    widths = [max(len(row[i]) for row in lines) for i in range(len(header))]
    return "\n".join("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)) for row in lines)


def to_csv(rows: List[Row]) -> str:
    """Serialise rows to CSV text (for archiving / spreadsheet import)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "symbol", "asset_class", "rs_vs_btc", "gate", "tsi_4h", "state_4h",
        "tsi_1h", "state_1h", "conviction", "signal", "size_pct",
        "current_position", "target_position", "delta", "action", "note",
    ])
    for r in rows:
        writer.writerow([
            r.symbol, r.asset_class,
            "" if r.rs_vs_bench is None else f"{r.rs_vs_bench:.6f}", r.gate,
            f"{r.tsi_4h:.4f}", r.state_4h, f"{r.tsi_1h:.4f}", r.state_1h,
            r.conviction, r.direction.value, f"{r.size_fraction * 100:.0f}",
            f"{r.current_position:g}", f"{r.target_position:g}",
            f"{r.delta:g}", r.action, r.note,
        ])
    return buf.getvalue()
