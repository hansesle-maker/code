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
import threading
import time
from typing import List, Optional

from flask import Flask, jsonify, render_template

from tsi_signal.scanner import (
    SymbolScan,
    fetch_all_futures_symbols,
    scan_all,
    symbolscan_to_dict,
)

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
    try:
        log.info("Scan started: fetching symbol list …")
        symbols = fetch_all_futures_symbols()
        log.info("Scanning %d symbols × 3 timeframes …", len(symbols))
        results = scan_all(symbols)
        with _lock:
            _cache["results"] = results
            _cache["scanned_at"] = datetime.datetime.utcnow()
        log.info("Scan complete — %d symbols", len(results))
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
