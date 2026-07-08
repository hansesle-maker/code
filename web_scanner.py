#!/usr/bin/env python3
"""Binance Futures scanner — mobile-friendly web dashboard.

Two screens on the same server:
    /      TSI multi-timeframe scanner (4h/1h/15m TSI states)
    /smc   Smart Money Concepts screener — per-symbol simulated position
           (LONG/SHORT/FLAT), entry, SL/TP, R:R, PnL, market structure
           (BOS/CHoCH), premium/discount zone, order blocks, FVG, EQH/EQL

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
import threading
import time
from typing import List, Optional

from flask import Flask, jsonify, render_template, request

from tsi_signal.scanner import (
    SymbolScan,
    fetch_all_futures_symbols,
    scan_all,
    symbolscan_to_dict,
)
from tsi_signal.smc_scanner import (
    DEFAULT_TIMEFRAME,
    SMC_TIMEFRAMES,
    scan_all_smc,
    smc_to_dict,
)
from tsi_signal.rate_limit import BinanceBanned

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

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
}

# SMC screener cache, one slot per timeframe.
_smc_cache: dict = {
    tf: {"results": [], "scanned_at": None, "scanning": False, "error": None}
    for tf in SMC_TIMEFRAMES
}
# The timeframe most recently viewed in the UI — the background loop keeps
# only this one fresh so we don't hammer the Binance API for unused TFs.
_smc_active_tf: str = DEFAULT_TIMEFRAME

# Global gate: only one full-symbol scan (TSI or SMC, any timeframe) runs at
# a time, so their Binance request-weight bursts never stack on top of each
# other regardless of which routes/threads triggered them.
_scan_gate = threading.Lock()


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


def do_scan() -> None:
    """Fetch all symbols and compute TSI states; update the shared cache."""
    with _lock:
        if _cache["scanning"]:
            return
        _cache["scanning"] = True
        _cache["error"] = None
    if not _scan_gate.acquire(blocking=False):
        log.info("Scan skipped: another full-symbol scan is already running")
        with _lock:
            _cache["scanning"] = False
        return
    try:
        log.info("Scan started: fetching symbol list …")
        symbols = fetch_all_futures_symbols()
        log.info("Scanning %d symbols × 3 timeframes …", len(symbols))
        results = scan_all(symbols)
        with _lock:
            _cache["results"] = results
            _cache["scanned_at"] = datetime.datetime.utcnow()
        log.info("Scan complete — %d symbols", len(results))
    except BinanceBanned as exc:
        log.error("Scan aborted: %s", exc)
        with _lock:
            _cache["error"] = str(exc)
    except Exception as exc:
        log.error("Scan failed: %s", exc)
        with _lock:
            _cache["error"] = str(exc)
    finally:
        with _lock:
            _cache["scanning"] = False
        _scan_gate.release()


def do_smc_scan(tf: str = DEFAULT_TIMEFRAME) -> None:
    """Run the SMC screener over all futures symbols for one timeframe."""
    if tf not in _smc_cache:
        return
    slot = _smc_cache[tf]
    with _lock:
        if slot["scanning"]:
            return
        slot["scanning"] = True
        slot["error"] = None
    if not _scan_gate.acquire(blocking=False):
        log.info("SMC scan (%s) skipped: another full-symbol scan is already running", tf)
        with _lock:
            slot["scanning"] = False
        return
    try:
        log.info("SMC scan started (%s): fetching symbol list …", tf)
        symbols = fetch_all_futures_symbols()
        log.info("SMC scanning %d symbols @ %s …", len(symbols), tf)
        results = scan_all_smc(symbols, tf)
        with _lock:
            slot["results"] = results
            slot["scanned_at"] = datetime.datetime.utcnow()
        log.info("SMC scan complete (%s) — %d symbols", tf, len(results))
    except BinanceBanned as exc:
        log.error("SMC scan aborted (%s): %s", tf, exc)
        with _lock:
            slot["error"] = str(exc)
    except Exception as exc:
        log.error("SMC scan failed (%s): %s", tf, exc)
        with _lock:
            slot["error"] = str(exc)
    finally:
        with _lock:
            slot["scanning"] = False
        _scan_gate.release()


def _background_loop() -> None:
    """Sleep until each 15-minute candle close, then trigger the scans.

    The two scans run one after another (not in parallel) so their Binance
    request-weight bursts don't stack on top of each other.
    """
    while True:
        wait = _secs_to_next_15m()
        log.info("Next scan in %.0f s (at next 15 m close + 8 s)", wait)
        time.sleep(wait)
        do_scan()
        do_smc_scan(_smc_active_tf)


# ---------------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------------

@app.template_filter("fmt_tsi")
def fmt_tsi(v: float) -> str:
    return f"{v:+.2f}"


@app.template_filter("fmt_price")
def fmt_price(v: Optional[float]) -> str:
    """Adaptive price formatting across the huge Binance price spectrum."""
    if v is None:
        return "—"
    av = abs(v)
    if av >= 1000:
        return f"{v:,.1f}"
    if av >= 100:
        return f"{v:,.2f}"
    if av >= 1:
        return f"{v:.4f}"
    if av >= 0.01:
        return f"{v:.5f}"
    return f"{v:.8f}"


@app.template_filter("fmt_signed")
def fmt_signed(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:+.2f}"


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
    return render_template(
        "dashboard.html",
        results=results,
        scanned_at=scanned_at,
        scanning=scanning,
        error=error,
        static_mode=False,
    )


@app.route("/api/data")
def api_data():
    with _lock:
        results = list(_cache["results"])
        scanned_at = _cache["scanned_at"]
    payload = [symbolscan_to_dict(r) for r in results]
    return jsonify({
        "scanned_at": scanned_at.isoformat() + "Z" if scanned_at else None,
        "count": len(payload),
        "symbols": payload,
    })


@app.route("/refresh", methods=["POST"])
def manual_refresh():
    t = threading.Thread(target=do_scan, daemon=True)
    t.start()
    return jsonify({"status": "started"})


# ---------------------------------------------------------------------------
# SMC screener routes
# ---------------------------------------------------------------------------

def _parse_tf() -> str:
    tf = request.args.get("tf", DEFAULT_TIMEFRAME)
    return tf if tf in SMC_TIMEFRAMES else DEFAULT_TIMEFRAME


@app.route("/smc")
def smc_dashboard():
    global _smc_active_tf
    tf = _parse_tf()
    _smc_active_tf = tf
    slot = _smc_cache[tf]
    with _lock:
        results = list(slot["results"])
        scanned_at = slot["scanned_at"]
        scanning = slot["scanning"]
        error = slot["error"]
    # lazy first scan: viewing a timeframe with no data kicks one off
    if not results and not scanning:
        threading.Thread(target=do_smc_scan, args=(tf,), daemon=True).start()
        scanning = True
    return render_template(
        "smc.html",
        results=results,
        scanned_at=scanned_at,
        scanning=scanning,
        error=error,
        tf=tf,
        timeframes=SMC_TIMEFRAMES,
    )


@app.route("/api/smc")
def api_smc():
    tf = _parse_tf()
    slot = _smc_cache[tf]
    with _lock:
        results = list(slot["results"])
        scanned_at = slot["scanned_at"]
    return jsonify({
        "tf": tf,
        "scanned_at": scanned_at.isoformat() + "Z" if scanned_at else None,
        "count": len(results),
        "symbols": [smc_to_dict(r) for r in results],
    })


@app.route("/smc/refresh", methods=["POST"])
def smc_manual_refresh():
    tf = _parse_tf()
    threading.Thread(target=do_smc_scan, args=(tf,), daemon=True).start()
    return jsonify({"status": "started", "tf": tf})


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
        def _initial_scans():
            # Sequential, not parallel: both scans share the same Binance
            # rate-limit budget, so running them back-to-back (rather than
            # in parallel threads) halves the peak request burst.
            do_scan()
            do_smc_scan(DEFAULT_TIMEFRAME)
        threading.Thread(target=_initial_scans, daemon=True).start()

    threading.Thread(target=_background_loop, daemon=True).start()

    log.info("Dashboard available at http://0.0.0.0:%d", args.port)
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
