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

from cardwell_web import bp as cardwell_bp
from cardwell_web import _refresh_symbols as _refresh_cardwell_symbols
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
    discover_tradfi_futures,
    fetch_all_futures_symbols,
    fetch_futures_symbol_info,
    resolve_tradfi_markets,
    scan_all,
    symbolscan_to_dict,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Position tracking config (overridable via env so systemd can tune it).
POSITIONS_PATH = os.environ.get("TSI_POSITIONS", "positions.json")
DISASTER_PCT = float(os.environ.get("TSI_DISASTER_PCT", DEFAULT_DISASTER_PCT))

app = Flask(__name__)
app.register_blueprint(cardwell_bp)

# ---------------------------------------------------------------------------
# Shared state (protected by _lock)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_cache: dict = {
    "results": [],       # List[SymbolScan]
    "scanned_at": None,  # datetime UTC
    "scanning": False,
    "error": None,
    "notice": None,      # non-fatal info (e.g. unlisted TradFi symbols)
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
        info = fetch_futures_symbol_info()
        fut_universe = set(fetch_all_futures_symbols(info=info))
        # TradFi ①: underlyingType/underlyingSubType 기반 자동 탐색 (fapi)
        auto_tradfi = set(discover_tradfi_futures(info))
        # TradFi ②: 안전망 후보를 선물 전체(모든 contractType) → 현물 순 확인
        fut_all = {s["symbol"] for s in info if s["status"] == "TRADING"}
        markets, missing = resolve_tradfi_markets(fut_all)
        tradfi_syms = sorted(auto_tradfi | set(markets))
        parts = []
        if tradfi_syms:
            shown = ", ".join(tradfi_syms[:12])
            if len(tradfi_syms) > 12:
                shown += f" 외 {len(tradfi_syms) - 12}종목"
            parts.append(f"TradFi {len(tradfi_syms)}종목 포함: {shown}")
        if missing:
            parts.append("바이낸스 미상장: " + ", ".join(missing))
        with _lock:
            _cache["notice"] = " · ".join(parts) if parts else None
        symbols = sorted(fut_universe | auto_tradfi | set(markets))
        log.info("Scanning %d symbols × 4 timeframes …", len(symbols))
        results = scan_all(symbols, markets=markets)
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
        notice: Optional[str] = _cache.get("notice")
        positions: list = list(_cache["positions"])
    return render_template(
        "dashboard.html",
        results=results,
        scanned_at=scanned_at,
        scanning=scanning,
        error=error,
        notice=notice,
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

    # Preferred inputs: strategy TF (alt) + refresh period -> base=refresh, mult=alt/refresh.
    alt_tf = cfg.get("alt")
    refresh = cfg.get("refresh")
    if alt_tf and refresh:
        if alt_tf not in occ.INTERVAL_MS or refresh not in occ.INTERVAL_MS:
            return jsonify({"ok": False, "error": "Unsupported alt/refresh timeframe"}), 400
        alt_ms, ref_ms = occ.INTERVAL_MS[alt_tf], occ.INTERVAL_MS[refresh]
        if alt_ms < ref_ms or alt_ms % ref_ms != 0:
            return jsonify({"ok": False,
                            "error": f"전략 TF({alt_tf})는 반영 주기({refresh})의 정수배여야 합니다."}), 400
        interval = refresh
        cfg["interval"] = interval
        cfg["mult"] = alt_ms // ref_ms
        cfg["use_res"] = cfg["mult"] > 1
    else:
        interval = cfg.get("interval", "15m")
    if interval not in occ.INTERVAL_MS:
        return jsonify({"ok": False, "error": f"Unsupported interval: {interval}"}), 400

    # Guard against pathologically large fetches (fine refresh × long history).
    est_bars = days * 86_400_000 / occ.INTERVAL_MS[interval]
    if est_bars > 70_000:
        return jsonify({"ok": False,
                        "error": f"{interval} × {days}일 ≈ {est_bars:,.0f}봉으로 너무 큽니다. "
                                 "기간을 줄이거나 반영 주기를 늘리세요."}), 400

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
# Daily Close Comparison strategy (separate, repaint-aware)
# ---------------------------------------------------------------------------

@app.route("/dcc")
def dcc():
    return render_template("dcc.html")


@app.route("/api/dcc/run", methods=["POST"])
def dcc_run():
    """Backtest the repaint-aware Daily Close Comparison strategy."""
    import dcc_strategy as dccmod
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

    alt_tf = cfg.get("alt", "1d")
    refresh = cfg.get("refresh", "15m")
    if alt_tf not in occ.INTERVAL_MS or refresh not in occ.INTERVAL_MS:
        return jsonify({"ok": False, "error": "Unsupported alt/refresh timeframe"}), 400
    alt_ms, ref_ms = occ.INTERVAL_MS[alt_tf], occ.INTERVAL_MS[refresh]
    if alt_ms < ref_ms or alt_ms % ref_ms != 0:
        return jsonify({"ok": False,
                        "error": f"비교 TF({alt_tf})는 반영 주기({refresh})의 정수배여야 합니다."}), 400
    interval = refresh
    cfg["mult"] = alt_ms // ref_ms
    cfg["use_res"] = cfg["mult"] > 1

    est_bars = days * 86_400_000 / ref_ms
    if est_bars > 70_000:
        return jsonify({"ok": False,
                        "error": f"{refresh} × {days}일 ≈ {est_bars:,.0f}봉으로 너무 큽니다. "
                                 "기간을 줄이거나 반영 주기를 늘리세요."}), 400

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
                        "error": f"Too few bars ({len(candles)}). Try a longer window."}), 400

    try:
        result = dccmod.run_dcc_web(candles, cfg)
    except Exception as exc:
        log.exception("DCC backtest failed")
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
    threading.Thread(target=_refresh_cardwell_symbols, daemon=True).start()

    log.info("Dashboard available at http://0.0.0.0:%d", args.port)
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
