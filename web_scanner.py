#!/usr/bin/env python3
"""Binance Futures TSI scanner — mobile-friendly web dashboard.

Usage:
    python web_scanner.py                  # starts on port 5000
    python web_scanner.py --port 8080
    python web_scanner.py --no-scan        # skip initial scan (show empty state)

Open http://<your-ip>:5000 in iOS Safari, then "Add to Home Screen" for an
app-like experience. The dashboard auto-refreshes at each 15-minute candle close.
"""
from __future__ import annotations

import argparse
import datetime
import logging
import os
import threading
import time
from typing import Dict, List, Optional

from flask import Flask, jsonify, render_template, request

from tsi_signal.alerts import build_messages, diff_alerts, send_telegram
from tsi_signal.positions import (
    DEFAULT_DISASTER_PCT,
    evaluate_exits,
    load_state,
    open_positions_view,
    register_entries,
    save_state,
)
from tsi_signal.scanner import (
    SymbolScan,
    fetch_all_futures_symbols,
    scan_all,
    symbolscan_to_dict,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Position tracking config (overridable via env so systemd can tune it).
POSITIONS_PATH = os.environ.get("TSI_POSITIONS", "positions.json")
DISASTER_PCT = float(os.environ.get("TSI_DISASTER_PCT", DEFAULT_DISASTER_PCT))

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Shared state (protected by _lock)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_cache: dict = {
    "results": [],       # List[SymbolScan]
    "scanned_at": None,  # datetime UTC
    "scanning": False,
    "error": None,
    "positions": [],     # open-position view (list of dicts)
}
# Previous scan's serialized symbols, kept in memory to diff market/entry
# alerts between runs (positions persist to disk separately).
_prev_symbols: Dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Scanning helpers
# ---------------------------------------------------------------------------

def _secs_to_next_15m(buffer: int = 8) -> float:
    """Seconds until the next 15-minute candle close plus a small buffer."""
    now = datetime.datetime.utcnow()
    total_secs = now.minute * 60 + now.second
    slot_secs = (total_secs // 900 + 1) * 900  # 900 = 15 * 60
    remain = slot_secs - total_secs + buffer
    return remain if remain > 0 else remain + 900


def _process_signals(results: List[SymbolScan],
                     scanned_at: datetime.datetime) -> list:
    """Diff market/entry alerts, run the position lifecycle, push Telegram.

    Returns the open-position view for the dashboard. Position state persists
    to ``POSITIONS_PATH`` so it survives server restarts.
    """
    global _prev_symbols

    groups = diff_alerts(_prev_symbols, results)

    state = load_state(POSITIONS_PATH)
    exit_groups = evaluate_exits(state, results, scanned_at, DISASTER_PCT)
    opened = register_entries(state, results, scanned_at)
    save_state(POSITIONS_PATH, state)
    groups.update(exit_groups)

    if opened:
        log.info("Registered %d new position(s): %s", len(opened),
                 ", ".join(f"{p.symbol} {p.side}" for p in opened))

    messages = build_messages(groups, scanned_at)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if messages and token and chat:
        for msg in messages:
            send_telegram(token, chat, msg)
        log.info("Sent %d Telegram alert message(s).", len(messages))
    elif messages:
        log.info("%d alert block(s) ready but TELEGRAM_BOT_TOKEN/CHAT_ID unset.",
                 sum(1 for v in groups.values() if v))

    # Snapshot this scan as the baseline for the next diff.
    _prev_symbols = {r.symbol: symbolscan_to_dict(r) for r in results}
    return open_positions_view(state, results)


def do_scan() -> None:
    """Fetch all symbols and compute TSI states; update the shared cache."""
    with _lock:
        if _cache["scanning"]:
            return
        _cache["scanning"] = True
        _cache["error"] = None
    try:
        log.info("Scan started: fetching symbol list …")
        symbols = fetch_all_futures_symbols()
        log.info("Scanning %d symbols × 3 timeframes …", len(symbols))
        results = scan_all(symbols)
        scanned_at = datetime.datetime.utcnow()
        positions_view = _process_signals(results, scanned_at)
        with _lock:
            _cache["results"] = results
            _cache["scanned_at"] = scanned_at
            _cache["positions"] = positions_view
        log.info("Scan complete — %d symbols, %d open position(s)",
                 len(results), len(positions_view))
    except Exception as exc:
        log.error("Scan failed: %s", exc)
        with _lock:
            _cache["error"] = str(exc)
    finally:
        with _lock:
            _cache["scanning"] = False


def _background_loop() -> None:
    """Sleep until each 15-minute candle close, then trigger a scan."""
    while True:
        wait = _secs_to_next_15m()
        log.info("Next scan in %.0f s (at next 15 m close + 8 s)", wait)
        time.sleep(wait)
        do_scan()


# ---------------------------------------------------------------------------
# Template filter
# ---------------------------------------------------------------------------

@app.template_filter("fmt_tsi")
def fmt_tsi(v: float) -> str:
    return f"{v:+.2f}"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    with _lock:
        results: List[SymbolScan] = list(_cache["results"])
        scanned_at: Optional[datetime.datetime] = _cache["scanned_at"]
        scanning: bool = _cache["scanning"]
        error: Optional[str] = _cache["error"]
        positions: list = list(_cache["positions"])
    return render_template(
        "dashboard.html",
        results=results,
        scanned_at=scanned_at,
        scanning=scanning,
        error=error,
        static_mode=False,
        positions=positions,
    )


@app.route("/api/data")
def api_data():
    with _lock:
        results = list(_cache["results"])
        scanned_at = _cache["scanned_at"]
        positions = list(_cache["positions"])
    payload = [symbolscan_to_dict(r) for r in results]
    return jsonify({
        "scanned_at": scanned_at.isoformat() + "Z" if scanned_at else None,
        "count": len(payload),
        "positions": positions,
        "symbols": payload,
    })


@app.route("/api/positions")
def api_positions():
    with _lock:
        positions = list(_cache["positions"])
        scanned_at = _cache["scanned_at"]
    return jsonify({
        "scanned_at": scanned_at.isoformat() + "Z" if scanned_at else None,
        "count": len(positions),
        "positions": positions,
    })


@app.route("/refresh", methods=["POST"])
def manual_refresh():
    t = threading.Thread(target=do_scan, daemon=True)
    t.start()
    return jsonify({"status": "started"})


# ---------------------------------------------------------------------------
# Strategy Lab
# ---------------------------------------------------------------------------

@app.route("/lab")
def lab():
    return render_template("lab.html")


@app.route("/api/lab/run", methods=["POST"])
def lab_run():
    """Run a dynamic backtest defined by the web strategy lab."""
    import strategy_lab as sl
    from tsi_signal.data import (
        FUTURES_BASE_URL,
        FUTURES_KLINES_PATH,
        SPOT_BASE_URL,
        SPOT_KLINES_PATH,
        fetch_klines_range,
    )
    from datetime import timezone as tz, timedelta

    cfg = request.get_json(force=True) or {}
    if "entry" not in cfg or "exit" not in cfg:
        return jsonify({"ok": False, "error": "entry and exit rules are required"}), 400

    symbol = str(cfg.get("symbol", "BTCUSDT")).upper()
    days   = max(1, int(cfg.get("days", 180)))
    market = cfg.get("market", "futures")

    if market == "futures":
        base, path = FUTURES_BASE_URL, FUTURES_KLINES_PATH
    else:
        base, path = SPOT_BASE_URL, SPOT_KLINES_PATH

    start_ms = int((datetime.datetime.now(tz.utc) - timedelta(days=days)).timestamp() * 1000)
    end_ms   = None

    try:
        c15 = fetch_klines_range(symbol, "15m", start_ms, end_ms, base_url=base, path=path)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Data fetch failed: {exc}"}), 502

    if len(c15) < 500:
        return jsonify({"ok": False,
                        "error": f"Too few bars ({len(c15)}). Try a longer window or check the symbol."}), 400

    try:
        result = sl.run_lab_backtest(c15, cfg)
    except Exception as exc:
        log.exception("Lab backtest failed")
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify(result)


@app.route("/api/lab/optimize", methods=["POST"])
def lab_optimize():
    """Auto-search condition combinations for the best strategy."""
    import strategy_lab as sl
    from tsi_signal.data import (
        FUTURES_BASE_URL,
        FUTURES_KLINES_PATH,
        SPOT_BASE_URL,
        SPOT_KLINES_PATH,
        fetch_klines_range,
    )
    from datetime import timezone as tz, timedelta

    cfg = request.get_json(force=True) or {}
    symbol = str(cfg.get("symbol", "BTCUSDT")).upper()
    days   = max(1, int(cfg.get("days", 180)))
    market = cfg.get("market", "futures")

    if market == "futures":
        base, path = FUTURES_BASE_URL, FUTURES_KLINES_PATH
    else:
        base, path = SPOT_BASE_URL, SPOT_KLINES_PATH

    start_ms = int((datetime.datetime.now(tz.utc) - timedelta(days=days)).timestamp() * 1000)

    try:
        c15 = fetch_klines_range(symbol, "15m", start_ms, None, base_url=base, path=path)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Data fetch failed: {exc}"}), 502

    if len(c15) < 1500:
        return jsonify({"ok": False,
                        "error": f"Too few bars ({len(c15)}) to optimize. Try a longer window."}), 400

    try:
        result = sl.run_lab_optimize(c15, cfg)
    except Exception as exc:
        log.exception("Lab optimize failed")
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify(result)


# ---------------------------------------------------------------------------
# Open-Close Cross strategy (separate, non-repainting)
# ---------------------------------------------------------------------------

@app.route("/occ")
def occ():
    return render_template("occ.html")


@app.route("/api/occ/run", methods=["POST"])
def occ_run():
    """Backtest the non-repainting Open-Close Cross strategy."""
    import occ_strategy as occ
    from tsi_signal.data import (
        FUTURES_BASE_URL,
        FUTURES_KLINES_PATH,
        SPOT_BASE_URL,
        SPOT_KLINES_PATH,
        fetch_klines_range,
    )
    from datetime import timezone as tz, timedelta

    cfg = request.get_json(force=True) or {}
    symbol = str(cfg.get("symbol", "BTCUSDT")).upper()
    days   = max(1, int(cfg.get("days", 180)))
    market = cfg.get("market", "futures")
    interval = cfg.get("interval", "15m")
    if interval not in occ.INTERVAL_MS:
        return jsonify({"ok": False, "error": f"Unsupported interval: {interval}"}), 400

    if market == "futures":
        base, path = FUTURES_BASE_URL, FUTURES_KLINES_PATH
    else:
        base, path = SPOT_BASE_URL, SPOT_KLINES_PATH

    start_ms = int((datetime.datetime.now(tz.utc) - timedelta(days=days)).timestamp() * 1000)

    try:
        candles = fetch_klines_range(symbol, interval, start_ms, None, base_url=base, path=path)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Data fetch failed: {exc}"}), 502

    if len(candles) < 300:
        return jsonify({"ok": False,
                        "error": f"Too few bars ({len(candles)}). Try a longer window or smaller interval."}), 400

    try:
        result = occ.run_occ_web(candles, cfg)
    except Exception as exc:
        log.exception("OCC backtest failed")
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="TSI web scanner dashboard")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--no-scan", action="store_true",
                        help="skip the initial scan on startup")
    args = parser.parse_args()

    if not args.no_scan:
        threading.Thread(target=do_scan, daemon=True).start()

    threading.Thread(target=_background_loop, daemon=True).start()

    log.info("Dashboard available at http://0.0.0.0:%d", args.port)
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
