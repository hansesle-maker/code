#!/usr/bin/env python3
"""CVD Divergence 스캐너 — Flask Blueprint (``/cvd``).

web_scanner.py (포트 5000)에 통합되어 CVD(누적 거래량 델타) 다이버전스를
12h/4h/1h/15m/5m/1m 여섯 타임프레임에서 스캔한다. 거래소는 바이낸스
USDT-M 선물(기본)과 빗썸 원화마켓을 전환할 수 있다.

스캔은 요청당 심볼 × 6TF 만큼 REST 호출이 필요해(바이낸스 6, 빗썸 8)
24시간 거래대금 상위 N개만 훑는 것이 기본이며, 백그라운드 스레드에서
진행률과 함께 수행된다.
"""
from __future__ import annotations

import argparse
import datetime
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import requests as _req
from flask import Blueprint, Flask, jsonify, render_template, request

from tsi_signal.cvd import (
    DEF_MODE, DEF_N, DEF_PERIOD, scan_divergence, strength_label,
)
from tsi_signal.exchanges import (
    BINANCE, BITHUMB, EXCHANGE_LABELS, TIMEFRAMES,
    display_symbol, get_candles, get_universe,
)

log = logging.getLogger(__name__)

bp = Blueprint("cvd", __name__)

# Bars fetched per timeframe: EMA50 warmup + 21-bar Hist + 30-bar divergence
# window needs ≥90; 150 keeps Binance at weight 2 and Bithumb at one page.
CVD_NEED = 150
_WORKERS = {BINANCE: 6, BITHUMB: 4}
TOP_N_CHOICES = (50, 120, 250, 0)   # 0 = 전체

_lock = threading.Lock()


def _blank(top_n: int = 120) -> dict:
    return {
        "rows": [], "scanned_at": None, "scanning": False,
        "error": None, "warn": None, "done": 0, "total": 0,
        "top_n": top_n, "n": DEF_N, "period": DEF_PERIOD, "mode": DEF_MODE,
    }


_cache: Dict[str, dict] = {BINANCE: _blank(), BITHUMB: _blank()}


def _sig_dict(sig) -> dict:
    return {
        "dir": sig.direction,
        "bars": sig.bars_ago,
        "strength": sig.strength,
        "strength_label": strength_label(sig.strength),
        "phase": sig.phase,
        "active": sig.active,
        "provisional": sig.provisional,
        "hist": sig.hist,
        "pivot_price": sig.pivot_price,
        "prev_price": sig.prev_price,
        "pivot_hist": round(sig.pivot_hist, 2),
        "prev_hist": round(sig.prev_hist, 2),
    }


def _scan_one(exchange: str, symbol: str, n: int, period: int, mode: str):
    """Scan every timeframe for one symbol. Returns (row, [error strings])."""
    session = _req.Session()
    session.headers.update({"Connection": "keep-alive"})
    cells: Dict[str, Optional[dict]] = {}
    errs: List[str] = []
    try:
        for tf in TIMEFRAMES:
            try:
                candles = get_candles(exchange, symbol, tf, CVD_NEED, session)
                sig = scan_divergence(candles, n=n, period=period, mode=mode)
                cells[tf] = _sig_dict(sig) if sig else None
            except Exception as exc:            # per-timeframe isolation
                cells[tf] = None
                errs.append(f"{symbol} {tf}: {exc}")
    finally:
        session.close()
    return {
        "symbol": symbol,
        "label": display_symbol(exchange, symbol),
        "tf": cells,
    }, errs


def do_scan(exchange: str, top_n: int, n: int = DEF_N,
            period: int = DEF_PERIOD, mode: str = DEF_MODE) -> None:
    """Scan the ranked universe and refresh ``_cache[exchange]``."""
    with _lock:
        if _cache[exchange]["scanning"]:
            return
        _cache[exchange].update(scanning=True, error=None, warn=None,
                                done=0, total=0, top_n=top_n,
                                n=n, period=period, mode=mode)
    try:
        symbols = get_universe(exchange, top_n or None)
        with _lock:
            _cache[exchange]["total"] = len(symbols)
        log.info("CVD scan (%s): %d symbols × %d TFs …",
                 exchange, len(symbols), len(TIMEFRAMES))

        rows: List[dict] = []
        all_errs: List[str] = []
        dead = 0
        with ThreadPoolExecutor(max_workers=_WORKERS.get(exchange, 4)) as pool:
            futs = {pool.submit(_scan_one, exchange, s, n, period, mode): s
                    for s in symbols}
            for fut in as_completed(futs):
                try:
                    row, errs = fut.result()
                except Exception as exc:
                    all_errs.append(f"{futs[fut]}: {exc}")
                    dead += 1
                else:
                    rows.append(row)
                    all_errs.extend(errs)
                    if len(errs) == len(TIMEFRAMES):
                        dead += 1
                with _lock:
                    _cache[exchange]["done"] += 1

        rows.sort(key=lambda r: r["symbol"])
        total = len(symbols)
        error = warn = None
        if total and dead >= max(1, total // 2):
            # Most symbols returned nothing on every timeframe — treat the
            # API/format as broken rather than showing an empty table.
            error = (f"{EXCHANGE_LABELS[exchange]} 데이터를 받지 못했습니다 "
                     f"({dead}/{total} 종목 실패). 첫 오류: "
                     + (all_errs[0] if all_errs else "알 수 없음"))
        elif all_errs:
            warn = f"{len(all_errs)}건의 개별 요청 실패 (예: {all_errs[0]})"

        with _lock:
            _cache[exchange].update(
                rows=rows, scanned_at=datetime.datetime.utcnow(),
                error=error, warn=warn,
            )
        log.info("CVD scan (%s) done — %d rows, %d failures",
                 exchange, len(rows), len(all_errs))
    except Exception as exc:
        log.error("CVD scan (%s) failed: %s", exchange, exc)
        with _lock:
            _cache[exchange]["error"] = str(exc)
    finally:
        with _lock:
            _cache[exchange]["scanning"] = False


def _exchange_arg(src) -> str:
    ex = (src.get("exchange") or BINANCE).lower()
    return ex if ex in _cache else BINANCE


def _snapshot(exchange: str) -> dict:
    with _lock:
        c = _cache[exchange]
        return {
            "exchange": exchange,
            "exchange_label": EXCHANGE_LABELS[exchange],
            "rows": list(c["rows"]),
            "scanned_at": c["scanned_at"].isoformat() + "Z" if c["scanned_at"] else None,
            "scanning": c["scanning"],
            "error": c["error"],
            "warn": c["warn"],
            "done": c["done"],
            "total": c["total"],
            "top_n": c["top_n"],
            "n": c["n"],
            "period": c["period"],
            "mode": c["mode"],
        }


# ── Routes ─────────────────────────────────────────────────────────────────

@bp.route("/cvd")
def cvd_page():
    return render_template(
        "cvd.html",
        timeframes=list(TIMEFRAMES),
        exchanges=[{"id": k, "label": v} for k, v in EXCHANGE_LABELS.items()],
        top_n_choices=list(TOP_N_CHOICES),
    )


@bp.route("/api/cvd/status")
def cvd_status():
    return jsonify({"ok": True, **_snapshot(_exchange_arg(request.args))})


@bp.route("/api/cvd/scan", methods=["POST"])
def cvd_scan():
    data = request.get_json(force=True, silent=True) or {}
    exchange = _exchange_arg(data)
    try:
        top_n = int(data.get("top_n", 120))
    except (TypeError, ValueError):
        top_n = 120
    top_n = 0 if top_n <= 0 else min(1000, top_n)
    try:
        n = min(5, max(1, int(data.get("n", DEF_N))))
    except (TypeError, ValueError):
        n = DEF_N
    try:
        period = min(200, max(5, int(data.get("period", DEF_PERIOD))))
    except (TypeError, ValueError):
        period = DEF_PERIOD
    mode = "ema" if str(data.get("mode", DEF_MODE)).lower() == "ema" else "periodic"

    with _lock:
        if _cache[exchange]["scanning"]:
            return jsonify({"ok": True, "started": False, "reason": "already scanning"})
    threading.Thread(target=do_scan, args=(exchange, top_n, n, period, mode),
                     daemon=True).start()
    return jsonify({"ok": True, "started": True})


def main() -> None:
    parser = argparse.ArgumentParser(description="CVD divergence scanner (standalone)")
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    app = Flask(__name__)
    app.register_blueprint(bp)
    log.info("단독 실행: http://0.0.0.0:%d/cvd", args.port)
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
