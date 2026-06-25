#!/usr/bin/env python3
"""
Cardwell RSI Trade Navigator — Web Backtester
Binance 선물 심볼을 선택해 15m / 1h / 4h 백테스트를 웹에서 실행합니다.

Usage:
    python cardwell_web.py              # port 5001
    python cardwell_web.py --port 8080
"""
from __future__ import annotations

import argparse
import base64
import datetime
import io
import logging
import threading
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from flask import Flask, jsonify, render_template, request

from cardwell_backtest import (
    Params, TIMEFRAMES,
    add_signals, simulate_trades, compute_stats, build_figure,
)
from tsi_signal.data import (
    FUTURES_BASE_URL, FUTURES_KLINES_PATH,
    Candle, fetch_klines, fetch_klines_range,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)

# ─── Symbol cache ─────────────────────────────────────────────────────────────
_sym_lock = threading.Lock()
_symbols: List[str] = []
_sym_fetched_at: Optional[datetime.datetime] = None


def _refresh_symbols() -> None:
    global _symbols, _sym_fetched_at
    import requests
    try:
        r = requests.get(f"{FUTURES_BASE_URL}/fapi/v1/exchangeInfo", timeout=10)
        r.raise_for_status()
        syms = sorted(
            s["symbol"]
            for s in r.json().get("symbols", [])
            if s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT"
            and s.get("contractType") == "PERPETUAL"
        )
        with _sym_lock:
            _symbols = syms
            _sym_fetched_at = datetime.datetime.utcnow()
        log.info("심볼 목록 갱신: %d개", len(syms))
    except Exception as e:
        log.warning("심볼 목록 로드 실패: %s", e)


def _get_symbols() -> List[str]:
    with _sym_lock:
        stale = (
            _sym_fetched_at is None
            or (datetime.datetime.utcnow() - _sym_fetched_at).total_seconds() > 3600
        )
    if stale:
        threading.Thread(target=_refresh_symbols, daemon=True).start()
    with _sym_lock:
        return list(_symbols)


# ─── Binance data ─────────────────────────────────────────────────────────────

def _candles_to_df(candles: List[Candle]) -> pd.DataFrame:
    if not candles:
        raise ValueError("Binance에서 데이터가 반환되지 않았습니다.")
    rows = [
        {"open": c.open, "high": c.high, "low": c.low,
         "close": c.close, "volume": c.volume, "_ts": c.open_time}
        for c in candles
    ]
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df.pop("_ts"), unit="ms", utc=True)
    return df


def _fetch_df(symbol: str, tf: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Fetch Binance Futures OHLCV and resample to requested TF."""
    binance_tf = "1h" if tf == "4h" else tf
    candles = fetch_klines_range(
        symbol, binance_tf, start_ms, end_ms,
        base_url=FUTURES_BASE_URL, path=FUTURES_KLINES_PATH,
    )
    df = _candles_to_df(candles)
    if tf == "4h":
        df = df.resample("4h", closed="left", label="left").agg(
            {"open": "first", "high": "max", "low": "min",
             "close": "last", "volume": "sum"}
        ).dropna(subset=["close"])
    return df


def _period_to_range(period: str) -> Tuple[int, int]:
    now = datetime.datetime.utcnow()
    end_ms = int(now.timestamp() * 1000)
    days = {"1M": 30, "3M": 90, "6M": 180, "1Y": 365,
            "2Y": 730, "3Y": 1095}.get(period, 365)
    start_ms = int((now - datetime.timedelta(days=days)).timestamp() * 1000)
    return start_ms, end_ms


def _parse_date(s: str, end_of_day: bool = False) -> int:
    dt = datetime.datetime.strptime(s, "%Y-%m-%d")
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


# ─── Chart ────────────────────────────────────────────────────────────────────

def _chart_b64(symbol: str, tf_results: dict) -> str:
    if not tf_results:
        return ""
    fig = build_figure(symbol, tf_results)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    symbols = _get_symbols()
    return render_template("cardwell.html", symbols=symbols)


@app.route("/api/symbols")
def api_symbols():
    return jsonify(_get_symbols())


@app.route("/api/run", methods=["POST"])
def api_run():
    body = request.get_json(silent=True) or {}

    symbol = (body.get("symbol") or "BTCUSDT").strip().upper()
    tfs = [t for t in body.get("timeframes", ["1h"]) if t in TIMEFRAMES]
    period = body.get("period", "1Y")
    start_date = (body.get("start_date") or "").strip()
    end_date   = (body.get("end_date")   or "").strip()

    if not tfs:
        return jsonify({"ok": False, "error": "타임프레임을 선택하세요"}), 400

    # Date range
    try:
        if start_date:
            start_ms = _parse_date(start_date)
        else:
            start_ms, _ = _period_to_range(period)

        if end_date:
            end_ms = _parse_date(end_date, end_of_day=True)
        else:
            end_ms = int(datetime.datetime.utcnow().timestamp() * 1000)
    except ValueError as e:
        return jsonify({"ok": False, "error": f"날짜 형식 오류: {e}"}), 400

    if start_ms >= end_ms:
        return jsonify({"ok": False, "error": "시작일이 종료일보다 늦습니다"}), 400

    # Build Params
    pp = body.get("params", {})
    p = Params(
        rsi_len     = int(pp.get("rsiLen",     14)),
        fast_len    = int(pp.get("fastLen",     9)),
        slow_len    = int(pp.get("slowLen",     45)),
        ma_type     =     pp.get("maType",   "RMA"),
        atr_len     = int(pp.get("atrLen",     14)),
        atr_mult_sl = float(pp.get("atrMultSl", 1.5)),
        rr1         = float(pp.get("rr1",        1.0)),
        rr2         = float(pp.get("rr2",        2.0)),
        rr3         = float(pp.get("rr3",        3.0)),
        use_htf     = bool(pp.get("useHtf",    False)),
        use_chop    = bool(pp.get("useChop",   False)),
        adx_len     = int(pp.get("adxLen",     14)),
        adx_min     = float(pp.get("adxMin",   20.0)),
        trail_stop  = bool(pp.get("trailStop", False)),
    )

    # Run backtest per TF
    tf_results: dict = {}
    json_tfs:   dict = {}

    for tf in tfs:
        try:
            log.info("백테스트 시작: %s %s", symbol, tf)
            df = _fetch_df(symbol, tf, start_ms, end_ms)
            df = add_signals(df, p)
            trades  = simulate_trades(df, p)
            s       = compute_stats(trades)
            tf_results[tf] = (df, trades, s)
            json_tfs[tf]   = {
                "bars":       len(df),
                "date_range": f"{df.index[0].date()} – {df.index[-1].date()}",
                "stats":      s,
                "trades":     _trades_json(trades),
            }
            log.info("%s %s 완료: %d 트레이드", symbol, tf, len(trades))
        except Exception as exc:
            log.error("%s %s 오류: %s", symbol, tf, exc)
            json_tfs[tf] = {"error": str(exc)}

    chart_b64 = _chart_b64(symbol, tf_results)

    return jsonify({
        "ok":         True,
        "symbol":     symbol,
        "timeframes": json_tfs,
        "chart_b64":  chart_b64,
    })


def _trades_json(trades: pd.DataFrame) -> list:
    if trades.empty:
        return []
    return [
        {
            "dir":      t["direction"],
            "entry_t":  str(t["entry_time"])[:16],
            "exit_t":   str(t["exit_time"])[:16],
            "entry_px": round(float(t["entry_px"]), 4),
            "exit_px":  round(float(t["exit_px"]),  4),
            "sl_px":    round(float(t["sl_px"]),    4),
            "tp1_px":   round(float(t["tp1_px"]),   4),
            "tp2_px":   round(float(t["tp2_px"]),   4),
            "tp3_px":   round(float(t["tp3_px"]),   4),
            "reason":   t["exit_reason"],
            "tp1":      bool(t["tp1_hit"]),
            "tp2":      bool(t["tp2_hit"]),
            "tp3":      bool(t["tp3_hit"]),
            "pnl_r":    round(float(t["pnl_r"]), 3),
            "win":      bool(t["win"]),
        }
        for _, t in trades.iterrows()
    ]


# ─── Entry ────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Cardwell RSI 백테스터 웹 서버")
    ap.add_argument("--port", type=int, default=5001)
    args = ap.parse_args()

    threading.Thread(target=_refresh_symbols, daemon=True).start()
    log.info("웹 백테스터: http://0.0.0.0:%d", args.port)
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
