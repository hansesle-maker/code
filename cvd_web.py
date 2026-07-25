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
import os
import threading
import time
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

# Bars fetched per timeframe, per exchange. Binance charges the same weight
# (2) for any limit in 100–499, so depth there is free: on synthetic markets
# 350 bars reproduced full-history detection 100% of the time versus 88% at
# 150. Bithumb pages at 200 rows, so 198 keeps it to one request per
# timeframe while still beating the old 150.
CVD_NEED = {BINANCE: 350, BITHUMB: 198}
CVD_NEED_DEFAULT = 200
TOP_N_CHOICES = (50, 120, 250, 0)   # 0 = 전체
DEFAULT_TOP_N = 0                   # 전체 종목
DEFAULT_TFS = ("4h", "1h", "15m")   # 스캔·표시 기본 타임프레임

# One request is one work item, so wall time ≈ requests / min(rate cap,
# workers / RTT). Workers only need to be high enough to keep the rate
# limiter in exchanges.py saturated; that limiter is what protects the IP.
_WORKERS = {
    BINANCE: int(os.environ.get("TSI_CVD_WORKERS_BINANCE", 16)),
    BITHUMB: int(os.environ.get("TSI_CVD_WORKERS_BITHUMB", 10)),
}

# Thread-local HTTP sessions so connections (and TLS handshakes) are reused
# across every request a worker makes, instead of one session per symbol.
_tls = threading.local()


def _session() -> _req.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = _req.Session()
        s.headers.update({"Connection": "keep-alive"})
        _tls.session = s
    return s

_lock = threading.Lock()


def _blank(top_n: int = DEFAULT_TOP_N) -> dict:
    return {
        "rows": [], "scanned_at": None, "scanning": False,
        "error": None, "warn": None, "done": 0, "total": 0,
        "symbols": 0, "started_at": None, "took": None,
        "tfs": list(DEFAULT_TFS),
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


def _scan_cell(exchange: str, symbol: str, tf: str, n: int, period: int,
               mode: str):
    """One request's worth of work: (symbol, tf) → cell dict or error."""
    try:
        need = CVD_NEED.get(exchange, CVD_NEED_DEFAULT)
        candles = get_candles(exchange, symbol, tf, need, _session())
        sig = scan_divergence(candles, n=n, period=period, mode=mode)
        return symbol, tf, (_sig_dict(sig) if sig else None), None
    except Exception as exc:
        return symbol, tf, None, f"{symbol} {tf}: {exc}"


def do_scan(exchange: str, top_n: int, n: int = DEF_N,
            period: int = DEF_PERIOD, mode: str = DEF_MODE,
            tfs: Optional[List[str]] = None) -> None:
    """Scan the ranked universe and refresh ``_cache[exchange]``.

    Work is parallelised per (symbol, timeframe) so every worker stays busy
    and fewer selected timeframes cut the time proportionally.
    """
    tfs = [tf for tf in (tfs or DEFAULT_TFS) if tf in TIMEFRAMES] or list(DEFAULT_TFS)
    with _lock:
        if _cache[exchange]["scanning"]:
            return
        _cache[exchange].update(scanning=True, error=None, warn=None,
                                done=0, total=0, symbols=0, took=None,
                                started_at=datetime.datetime.utcnow(),
                                tfs=list(tfs), top_n=top_n,
                                n=n, period=period, mode=mode)
    t0 = time.monotonic()
    try:
        symbols = get_universe(exchange, top_n or None)
        tasks = [(s, tf) for s in symbols for tf in tfs]
        with _lock:
            _cache[exchange]["total"] = len(tasks)
            _cache[exchange]["symbols"] = len(symbols)
        log.info("CVD scan (%s): %d symbols × %d TFs = %d requests, %d workers …",
                 exchange, len(symbols), len(tfs), len(tasks),
                 _WORKERS.get(exchange, 4))

        cells: Dict[str, Dict[str, Optional[dict]]] = {s: {} for s in symbols}
        all_errs: List[str] = []
        err_count: Dict[str, int] = {s: 0 for s in symbols}
        with ThreadPoolExecutor(max_workers=_WORKERS.get(exchange, 4)) as pool:
            futs = [pool.submit(_scan_cell, exchange, s, tf, n, period, mode)
                    for s, tf in tasks]
            for fut in as_completed(futs):
                sym, tf, cell, err = fut.result()
                cells[sym][tf] = cell
                if err:
                    all_errs.append(err)
                    err_count[sym] += 1
                with _lock:
                    _cache[exchange]["done"] += 1

        rows = [{"symbol": s, "label": display_symbol(exchange, s), "tf": cells[s]}
                for s in sorted(symbols)]
        dead = sum(1 for s in symbols if err_count[s] == len(tfs))
        total = len(symbols)
        error = warn = None
        if total and dead >= max(1, total // 2):
            # Most symbols returned nothing on every timeframe — treat the
            # API/format as broken rather than showing an empty table.
            error = (f"{EXCHANGE_LABELS[exchange]} 데이터를 받지 못했습니다 "
                     f"({dead}/{total} 종목 실패). 첫 오류: "
                     + (all_errs[0] if all_errs else "알 수 없음"))
        elif all_errs:
            rate_hits = sum(1 for e in all_errs if "429" in e or "418" in e)
            warn = f"{len(all_errs)}건의 개별 요청 실패 (예: {all_errs[0]})"
            if rate_hits:
                warn = (f"{len(all_errs)}건 실패 중 {rate_hits}건이 요청 한도(429) "
                        f"— 재시도 후에도 실패한 건입니다. TSI 스캔과 겹쳤다면 "
                        f"TSI_BINANCE_WEIGHT_PER_MIN 값을 낮춰 보세요. "
                        f"(예: {all_errs[0]})")

        took = time.monotonic() - t0
        with _lock:
            _cache[exchange].update(
                rows=rows, scanned_at=datetime.datetime.utcnow(),
                error=error, warn=warn, took=round(took, 1),
            )
        log.info("CVD scan (%s) done — %d rows, %d requests in %.1fs "
                 "(%.1f req/s), %d failures", exchange, len(rows), len(tasks),
                 took, len(tasks) / took if took else 0.0, len(all_errs))
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
            "symbols": c["symbols"],
            "tfs": list(c["tfs"]),
            "took": c["took"],
            "elapsed": (round((datetime.datetime.utcnow()
                               - c["started_at"]).total_seconds(), 1)
                        if c["started_at"] and c["scanning"] else None),
            "workers": _WORKERS.get(exchange, 4),
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
        default_tfs=list(DEFAULT_TFS),
        default_top_n=DEFAULT_TOP_N,
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
        top_n = int(data.get("top_n", DEFAULT_TOP_N))
    except (TypeError, ValueError):
        top_n = DEFAULT_TOP_N
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
    req_tfs = data.get("tfs")
    tfs = ([tf for tf in TIMEFRAMES if tf in set(req_tfs)]
           if isinstance(req_tfs, list) and req_tfs else list(DEFAULT_TFS))

    with _lock:
        if _cache[exchange]["scanning"]:
            return jsonify({"ok": True, "started": False, "reason": "already scanning"})
    threading.Thread(target=do_scan,
                     args=(exchange, top_n, n, period, mode, tfs),
                     daemon=True).start()
    return jsonify({"ok": True, "started": True, "tfs": tfs})


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
