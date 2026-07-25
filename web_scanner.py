#!/usr/bin/env python3
"""Binance Futures TSI scanner — mobile-friendly web dashboard.

Usage:
    python web_scanner.py                  # starts on port 5000
    python web_scanner.py --port 8080
    python web_scanner.py --no-scan        # skip initial scan (show empty state)

Open http://<your-ip>:5000 in iOS Safari, then "Add to Home Screen" for an
app-like experience. The dashboard auto-refreshes on an interval that is
adjustable in the UI (default: every 15 minutes, at candle close).
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional

from flask import Flask, jsonify, render_template, request

from cardwell_web import bp as cardwell_bp
from cardwell_web import _refresh_symbols as _refresh_cardwell_symbols
from cvd_web import bp as cvd_bp
from tsi_signal.alerts import build_messages, diff_alerts, send_telegram
from tsi_signal.data import (
    FUTURES_BASE_URL,
    SPOT_BASE_URL,
    fetch_ticker_price,
)
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
app.register_blueprint(cvd_bp)

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
# Runtime-adjustable settings (web UI) — persisted so restarts keep them
# ---------------------------------------------------------------------------
SETTINGS_PATH = os.environ.get("TSI_SETTINGS", "web_settings.json")
DEFAULT_SETTINGS = {
    "swing_frac": 0.25,   # 변곡 민감도 (낮을수록 민감; scanner._find_last_inflection)
    "refresh_min": 15,    # 자동 스캔/새로고침 주기 (분)
}
_settings: dict = dict(DEFAULT_SETTINGS)


def _clamp_settings(d: dict) -> dict:
    """Validate/clamp incoming settings; ignore unknown or malformed keys."""
    out: dict = {}
    if "swing_frac" in d:
        try:
            out["swing_frac"] = min(0.60, max(0.05, round(float(d["swing_frac"]), 2)))
        except (TypeError, ValueError):
            pass
    if "refresh_min" in d:
        try:
            out["refresh_min"] = min(240, max(5, int(d["refresh_min"])))
        except (TypeError, ValueError):
            pass
    return out


def _load_settings() -> None:
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            _settings.update(_clamp_settings(json.load(f)))
        log.info("Settings loaded from %s: %s", SETTINGS_PATH, _settings)
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("Settings load failed (%s) — using defaults", exc)


def _save_settings_locked() -> None:
    """Write settings to disk. Caller must hold ``_lock``."""
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(_settings, f)
    except Exception as exc:
        log.warning("Settings save failed: %s", exc)


_load_settings()

# ---------------------------------------------------------------------------
# Bookmarks — server-side persistence (localStorage는 iOS에서 유실될 수 있음)
# ---------------------------------------------------------------------------
BOOKMARKS_PATH = os.environ.get("TSI_BOOKMARKS", "bookmarks.json")
_bookmarks: set = set()


def _load_bookmarks() -> None:
    global _bookmarks
    try:
        with open(BOOKMARKS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            _bookmarks = {str(s)[:32] for s in data if isinstance(s, str)}
        log.info("Bookmarks loaded: %d symbols", len(_bookmarks))
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("Bookmarks load failed: %s", exc)


def _save_bookmarks_locked() -> None:
    """Write bookmarks to disk. Caller must hold ``_lock``."""
    try:
        with open(BOOKMARKS_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(_bookmarks), f)
    except Exception as exc:
        log.warning("Bookmarks save failed: %s", exc)


_load_bookmarks()


# ---------------------------------------------------------------------------
# Scanning helpers
# ---------------------------------------------------------------------------

def _secs_to_next_boundary(minutes: int, buffer: int = 8) -> float:
    """Seconds until the next N-minute boundary (UTC epoch) plus a buffer."""
    step = max(1, int(minutes)) * 60
    now = time.time()
    return (int(now) // step + 1) * step - now + buffer


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
        swing_frac = _settings["swing_frac"]
    try:
        log.info("Scan started: fetching symbol list …")
        info = fetch_futures_symbol_info()
        fut_universe = set(fetch_all_futures_symbols(info=info))
        # TradFi ①: underlyingType/underlyingSubType 기반 자동 탐색 (fapi)
        auto_tradfi = set(discover_tradfi_futures(info))
        # TradFi ②: 안전망 후보를 선물 전체(모든 contractType) → 현물 순 확인
        fut_all = {s["symbol"] for s in info if s["status"] == "TRADING"}
        markets, missing = resolve_tradfi_markets(fut_all)
        if auto_tradfi or markets:
            log.info("TradFi included: %d symbols", len(auto_tradfi | set(markets)))
        with _lock:
            # 미상장 수동 후보만 경고 (자동 포함 목록은 UI에 표시하지 않음)
            _cache["notice"] = (
                "바이낸스 미상장 TradFi 심볼: " + ", ".join(missing)
                if missing else None
            )
        symbols = sorted(fut_universe | auto_tradfi | set(markets))
        log.info("Scanning %d symbols × 4 timeframes …", len(symbols))
        results = scan_all(symbols, markets=markets, swing_frac=swing_frac)
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
    """Sleep until the next refresh boundary, then trigger a scan.

    The interval (``refresh_min`` setting) is re-read every few seconds so a
    change made in the web UI takes effect without restarting the server.
    """
    while True:
        with _lock:
            iv = _settings["refresh_min"]
        target = time.time() + _secs_to_next_boundary(iv)
        log.info("Next scan in %.0f s (every %d min + 8 s)", target - time.time(), iv)
        while True:
            with _lock:
                iv2 = _settings["refresh_min"]
            if iv2 != iv:
                iv = iv2
                target = time.time() + _secs_to_next_boundary(iv)
                log.info("Refresh interval changed → next scan in %.0f s (every %d min)",
                         target - time.time(), iv)
            remain = target - time.time()
            if remain <= 0:
                break
            time.sleep(min(10.0, remain))
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
        settings = dict(_settings)
        bookmarks = sorted(_bookmarks)
    return render_template(
        "dashboard.html",
        results=results,
        scanned_at=scanned_at,
        scanning=scanning,
        error=error,
        notice=notice,
        static_mode=False,
        positions=positions,
        settings=settings,
        bookmarks=bookmarks,
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
# Compound realizer (복리 실현기) — live price proxy + persisted state
# ---------------------------------------------------------------------------
COMPOUND_PATH = os.environ.get("TSI_COMPOUND", "compound.json")
_compound_lock = threading.Lock()


@app.route("/api/price")
def api_price():
    """Live last price for a symbol (futures by default, spot optional)."""
    symbol = (request.args.get("symbol") or "").upper().strip()
    market = request.args.get("market", "futures")
    if not symbol:
        return jsonify({"ok": False, "error": "symbol required"}), 400
    base = SPOT_BASE_URL if market == "spot" else FUTURES_BASE_URL
    try:
        price = fetch_ticker_price(symbol, base_url=base)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify({"ok": True, "symbol": symbol, "market": market, "price": price})


@app.route("/compound")
def compound_page():
    return render_template("compound.html")


@app.route("/api/compound/state", methods=["GET", "POST"])
def compound_state():
    """Persist the compound-realizer config + realize log to a JSON file so
    the setup and history survive refreshes, restarts and device changes."""
    if request.method == "GET":
        try:
            with open(COMPOUND_PATH, encoding="utf-8") as f:
                return jsonify({"ok": True, "state": json.load(f)})
        except FileNotFoundError:
            return jsonify({"ok": True, "state": None})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "state must be an object"}), 400
    try:
        with _compound_lock:
            with open(COMPOUND_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True})


@app.route("/settings", methods=["POST"])
def update_settings():
    """Update runtime settings from the web UI.

    Body: {"swing_frac": float, "refresh_min": int} — either key optional.
    A swing_frac change triggers an immediate background rescan so the new
    sensitivity is visible without waiting for the next cycle.
    """
    data = request.get_json(force=True, silent=True) or {}
    clean = _clamp_settings(data)
    if not clean:
        return jsonify({"ok": False, "error": "no valid settings in request"}), 400
    with _lock:
        rescan = ("swing_frac" in clean
                  and clean["swing_frac"] != _settings["swing_frac"])
        _settings.update(clean)
        _save_settings_locked()
        current = dict(_settings)
    log.info("Settings updated via web: %s (rescan=%s)", current, rescan)
    if rescan:
        threading.Thread(target=do_scan, daemon=True).start()
    return jsonify({"ok": True, "settings": current, "rescan": rescan})


@app.route("/bookmarks", methods=["POST"])
def update_bookmarks():
    """Replace the bookmark list. Body: {"symbols": ["BTCUSDT", ...]}.

    서버 파일(bookmarks.json)에 저장되므로 새로고침·재시작·기기 변경에도
    유지된다 (localStorage는 백업 용도로만 사용).
    """
    global _bookmarks
    data = request.get_json(force=True, silent=True) or {}
    syms = data.get("symbols")
    if not isinstance(syms, list):
        return jsonify({"ok": False, "error": "symbols must be a list"}), 400
    clean = sorted({str(s)[:32] for s in syms if isinstance(s, str)})[:500]
    with _lock:
        _bookmarks = set(clean)
        _save_bookmarks_locked()
    return jsonify({"ok": True, "count": len(clean)})


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
