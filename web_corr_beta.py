#!/usr/bin/env python3
"""Altcoin ↔ BTC correlation & beta — web UI (Bithumb / Binance futures).

A small Flask front-end over the analysis/ scripts. Pick an exchange, a start
(and optional end) date/time, an interval, and it fetches every listed symbol,
computes each one's Pearson correlation and beta vs BTC, and shows a sortable
table. Long scans run in a background thread with a live progress bar.

Usage:
    python web_corr_beta.py                 # http://0.0.0.0:5000
    python web_corr_beta.py --port 8080

Open http://<your-vm-ip>:5000 from a browser. On Oracle Cloud you must also
open the port (see the note printed at startup).
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from flask import Flask, jsonify, render_template_string, request

# --- make the analysis/ package importable ---------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis"))

from corr_beta import KST, make_row, parse_start, sort_rows  # noqa: E402
import bithumb_btc_corr_beta as bithumb  # noqa: E402
import binance_futures_btc_corr_beta as binance  # noqa: E402
import tv_scripts as tvs  # noqa: E402
import pulse_entry as pulse  # noqa: E402

app = Flask(__name__)

_lock = threading.Lock()
_job: Dict = {
    "running": False,
    "cancel": False,
    "done": 0,
    "total": 0,
    "phase": "idle",       # idle | symbols | benchmark | scanning | done | error
    "error": None,
    "params": {},
    "rows": [],            # list of dicts
    "skipped_fetch": 0,
    "skipped_n": 0,
    "benchmark_n": 0,
    "started_at": None,
    "finished_at": None,
}

EXCHANGES = {
    "binance": {"label": "Binance USDT-M Futures", "bench": "BTCUSDT",
                "intervals": binance._INTERVALS, "default_interval": "1d"},
    "bithumb": {"label": "Bithumb KRW Market", "bench": "BTC",
                "intervals": bithumb._INTERVALS, "default_interval": "24h"},
}


def _set(**kw) -> None:
    with _lock:
        _job.update(kw)


def _run(params: Dict) -> None:
    """Background worker: fetch every symbol and compute corr/beta vs BTC."""
    try:
        ex = params["exchange"]
        interval = params["interval"]
        delay = float(params["delay"])
        min_points = int(params["min_points"])
        top = int(params["top"])
        start_ms = parse_start(params["from"])
        end_ms = parse_start(params["to"]) if params.get("to") else None
        bench = (params["benchmark"] or EXCHANGES[ex]["bench"]).upper()

        _set(phase="symbols", done=0, total=0, error=None, rows=[],
             skipped_fetch=0, skipped_n=0, benchmark_n=0)

        if params.get("symbols"):
            symbols = [s.strip().upper() for s in params["symbols"].split(",") if s.strip()]
        elif ex == "binance":
            symbols = binance.fetch_perp_symbols(params.get("quote", "USDT").upper(),
                                                 params.get("asset_class", "all"))
        else:
            symbols = bithumb.fetch_krw_symbols()

        _set(phase="benchmark", total=len(symbols))
        if ex == "binance":
            benchmark = binance.fetch_closes(bench, interval, start_ms, end_ms, delay)
            fetch = lambda s: binance.fetch_closes(s, interval, start_ms, end_ms, delay)
        else:
            benchmark = bithumb.fetch_closes(bench, interval)
            fetch = lambda s: bithumb.fetch_closes(s, interval)

        if not benchmark:
            _set(phase="error", error=f"no data for benchmark {bench}", running=False,
                 finished_at=time.time())
            return
        _set(phase="scanning", benchmark_n=len(benchmark))

        rows = []
        sk_fetch = sk_n = 0
        for i, sym in enumerate(symbols, 1):
            with _lock:
                if _job["cancel"]:
                    break
            if sym == bench:
                _set(done=i)
                continue
            try:
                alt = fetch(sym)
            except Exception:  # noqa: BLE001
                sk_fetch += 1
                _set(done=i, skipped_fetch=sk_fetch)
                continue
            row, n = make_row(sym, benchmark, alt, start_ms, min_points, end_ms)
            if row is None:
                sk_n += 1
            else:
                rows.append({"symbol": row.symbol, "n": row.n, "corr": row.corr,
                             "beta": row.beta, "r2": row.r2, "last": row.last})
            _set(done=i, skipped_n=sk_n)
            if delay:
                time.sleep(delay)

        rows.sort(key=lambda r: (r["corr"] != r["corr"], -r["corr"] if r["corr"] == r["corr"] else 0.0))
        if top:
            rows = rows[:top]
        _set(phase="done", rows=rows, finished_at=time.time())
    except Exception as exc:  # noqa: BLE001
        _set(phase="error", error=str(exc), finished_at=time.time())
    finally:
        _set(running=False, cancel=False)


PAGE = """<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>BTC 상관·베타 스캐너</title>
<style>
:root{color-scheme:dark light}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
 background:#0e1726;color:#e8eaed;padding:16px;max-width:1000px;margin:0 auto}
h1{font-size:18px;margin:0 0 12px;color:#f0b90b}
form{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;
 background:#141e30;border:1px solid #1e3a5f;border-radius:10px;padding:14px}
label{display:flex;flex-direction:column;font-size:12px;color:#8a919e;gap:4px}
input,select{background:#0e1726;color:#e8eaed;border:1px solid #1e3a5f;border-radius:6px;
 padding:8px;font-size:14px}
.row-btns{grid-column:1/-1;display:flex;gap:8px;flex-wrap:wrap}
button{background:#448aff;color:#fff;border:0;border-radius:6px;padding:10px 18px;
 font-size:14px;font-weight:600;cursor:pointer}
button.stop{background:#ff4976}
button:disabled{opacity:.5;cursor:default}
#status{margin:14px 0;font-size:13px;color:#8a919e;min-height:20px}
.bar{height:8px;background:#1e3a5f;border-radius:4px;overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;width:0;background:#00dcb4;transition:width .3s}
table{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}
th,td{padding:6px 8px;text-align:right;border-bottom:1px solid #1e3a5f;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:#8a919e;cursor:pointer;user-select:none;position:sticky;top:0;background:#0e1726}
tbody tr:hover{background:#141e30}
.pos{color:#00dcb4}.neg{color:#ff4976}.hi{color:#76ff03}
.wrap{overflow-x:auto}
@media (prefers-color-scheme:light){body{background:#f5f7fa;color:#1a2233}
 form{background:#fff;border-color:#dce3ee}h1{color:#b8860b}
 input,select,th{background:#fff;color:#1a2233}th{background:#f5f7fa}}
</style></head><body>
<h1>📊 BTC 상관계수 · 베타 스캐너 &nbsp;·&nbsp; <a href="/pulse" style="font-size:14px;color:#00dcb4">⚡ Pulse Entry 스크리너</a> &nbsp; <a href="/tv" style="font-size:14px;color:#448aff">📜 TV 스크립트 →</a></h1>
<form id="f">
 <label>거래소<select name="exchange" id="exchange">
  <option value="binance">Binance USDT-M Futures</option>
  <option value="bithumb">Bithumb KRW Market</option>
 </select></label>
 <label>자산군 (바이낸스)<select name="asset_class" id="asset_class">
  <option value="all">전체 (크립토+TradFi)</option>
  <option value="tradfi">TradFi만 (주식·ETF·상품)</option>
  <option value="crypto">크립토만</option>
 </select></label>
 <label>기준(벤치마크)<input name="benchmark" id="benchmark" value="BTCUSDT"></label>
 <label>인터벌<select name="interval" id="interval"></select></label>
 <label>시작 (KST)<input type="datetime-local" name="from" id="from"></label>
 <label>종료 (KST, 선택)<input type="datetime-local" name="to" id="to"></label>
 <label>최소 관측치<input type="number" name="min_points" value="20" min="2"></label>
 <label>상위 N (0=전체)<input type="number" name="top" value="0" min="0"></label>
 <label>종목 제한 (선택, 쉼표)<input name="symbols" placeholder="예: ETHUSDT,SOLUSDT"></label>
 <label>요청 간격(초)<input type="number" name="delay" value="0.05" step="0.05" min="0"></label>
 <div class="row-btns">
  <button type="submit" id="go">실행</button>
  <button type="button" class="stop" id="stop" disabled>중지</button>
  <button type="button" id="csv" disabled>CSV 저장</button>
 </div>
</form>
<div id="status">대기 중…<div class="bar"><i id="barfill"></i></div></div>
<div class="wrap"><table id="tbl"><thead><tr>
 <th data-k="symbol">COIN</th><th data-k="n">N</th><th data-k="corr">CORR</th>
 <th data-k="beta">BETA</th><th data-k="r2">R²</th><th data-k="last">LAST</th>
</tr></thead><tbody></tbody></table></div>
<script>
const INTERVALS={binance:%BINANCE_IV%,bithumb:%BITHUMB_IV%};
const DEF={binance:{bench:'BTCUSDT',iv:'1d'},bithumb:{bench:'BTC',iv:'24h'}};
const $=s=>document.querySelector(s);
let rows=[],sortK='corr',sortAsc=false,poll=null;
function fillIntervals(){const ex=$('#exchange').value;const sel=$('#interval');
 sel.innerHTML='';INTERVALS[ex].forEach(v=>{const o=document.createElement('option');
 o.value=v;o.textContent=v;if(v===DEF[ex].iv)o.selected=true;sel.appendChild(o);});
 $('#benchmark').value=DEF[ex].bench;}
$('#exchange').onchange=fillIntervals;fillIntervals();
// default start = 90 days ago, local time
(()=>{const d=new Date(Date.now()-90*864e5);d.setSeconds(0,0);
 $('#from').value=new Date(d-d.getTimezoneOffset()*6e4).toISOString().slice(0,16);})();
function fmt(x,d=3){return (x===null||x!==x)?'–':Number(x).toFixed(d);}
function render(){const tb=$('#tbl tbody');const r=[...rows].sort((a,b)=>{
 let x=a[sortK],y=b[sortK];if(typeof x==='string')return sortAsc?x.localeCompare(y):y.localeCompare(x);
 x=(x!==x)?-1e9:x;y=(y!==y)?-1e9:y;return sortAsc?x-y:y-x;});
 tb.innerHTML=r.map(o=>`<tr><td>${o.symbol}</td><td>${o.n}</td>
  <td class="${o.corr>=.5?'pos':o.corr<=-.5?'neg':''}">${fmt(o.corr)}</td>
  <td class="${o.beta>=1?'hi':o.beta<0?'neg':''}">${fmt(o.beta)}</td>
  <td>${fmt(o.r2)}</td><td>${fmt(o.last,6)}</td></tr>`).join('');}
document.querySelectorAll('th').forEach(th=>th.onclick=()=>{
 const k=th.dataset.k;if(k===sortK)sortAsc=!sortAsc;else{sortK=k;sortAsc=false;}render();});
$('#f').onsubmit=async e=>{e.preventDefault();const p=Object.fromEntries(new FormData($('#f')));
 const res=await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(p)});if(!res.ok){alert((await res.json()).error||'오류');return;}
 $('#go').disabled=true;$('#stop').disabled=false;$('#csv').disabled=true;
 if(poll)clearInterval(poll);poll=setInterval(tick,700);tick();};
$('#stop').onclick=()=>fetch('/cancel',{method:'POST'});
$('#csv').onclick=()=>window.location='/csv';
async function tick(){const s=await (await fetch('/status')).json();
 const pct=s.total?Math.round(100*s.done/s.total):0;$('#barfill').style.width=pct+'%';
 const ph={symbols:'종목 목록 로딩',benchmark:'벤치마크 로딩',scanning:'스캔 중',
  done:'완료',error:'오류',idle:'대기 중'}[s.phase]||s.phase;
 $('#status').firstChild.textContent=
  `${ph} — ${s.done}/${s.total} · 표시 ${s.rows.length} · 스킵 n=${s.skipped_n}/fetch=${s.skipped_fetch}`+
  (s.error?(' · '+s.error):'');
 rows=s.rows;render();
 if(!s.running){clearInterval(poll);poll=null;$('#go').disabled=false;$('#stop').disabled=true;
  $('#csv').disabled=rows.length===0;}}
</script></body></html>"""


@app.route("/")
def index():
    import json
    html = (PAGE
            .replace("%BINANCE_IV%", json.dumps(EXCHANGES["binance"]["intervals"]))
            .replace("%BITHUMB_IV%", json.dumps(EXCHANGES["bithumb"]["intervals"])))
    return render_template_string(html)


TV_PAGE = """<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>TV 스크립트 랭킹</title>
<style>
:root{color-scheme:dark light}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
 background:#0e1726;color:#e8eaed;padding:16px;max-width:1000px;margin:0 auto}
h1{font-size:18px;margin:0 0 12px;color:#f0b90b}
a{color:#448aff}
form{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;
 background:#141e30;border:1px solid #1e3a5f;border-radius:10px;padding:14px}
label{display:flex;flex-direction:column;font-size:12px;color:#8a919e;gap:4px}
input,select{background:#0e1726;color:#e8eaed;border:1px solid #1e3a5f;border-radius:6px;padding:8px;font-size:14px}
button{background:#448aff;color:#fff;border:0;border-radius:6px;padding:10px 18px;font-size:14px;font-weight:600;cursor:pointer}
button:disabled{opacity:.5}
#status{margin:14px 0;font-size:13px;color:#8a919e}
.warn{color:#ff9800}
table{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid #1e3a5f}
th{color:#8a919e;cursor:pointer;user-select:none}
td.n,th.n{text-align:right}
tbody tr:hover{background:#141e30}
.wrap{overflow-x:auto}
button.stop{background:#ff4976}
.bar{height:8px;background:#1e3a5f;border-radius:4px;overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;width:0;background:#00dcb4;transition:width .3s}
@media (prefers-color-scheme:light){body{background:#f5f7fa;color:#1a2233}
 form{background:#fff;border-color:#dce3ee}h1{color:#b8860b}input,select,th{background:#fff;color:#1a2233}}
</style></head><body>
<h1>📜 TradingView 스크립트 랭킹 &nbsp;·&nbsp; <a href="/" style="font-size:14px">← 상관·베타로</a></h1>
<form id="f">
 <label>페이지 수<input type="number" name="pages" id="pages" value="10" min="1" max="500"></label>
 <label>정렬<select name="sort" id="sort">
  <option value="boosts">부스트 많은순</option>
  <option value="date">최신순</option>
  <option value="title">제목순</option>
 </select></label>
 <label>요청 간격(초)<input type="number" name="delay" id="delay" value="1.0" step="0.1" min="0.3"></label>
 <button type="submit" id="go">불러오기</button>
 <button type="button" id="stop" disabled>중지</button>
</form>
<div id="status">대기 중…<div class="bar"><i id="barfill"></i></div></div>
<div class="wrap"><table id="tbl"><thead><tr>
 <th data-k="boosts" class="n">부스트</th><th data-k="author">작성자</th>
 <th data-k="published">게시일</th><th data-k="title">제목</th>
</tr></thead><tbody></tbody></table></div>
<script>
const $=s=>document.querySelector(s);let rows=[],sortK='boosts',sortAsc=false;
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function render(){const r=[...rows].sort((a,b)=>{let x=a[sortK],y=b[sortK];
 if(typeof x==='number')return sortAsc?x-y:y-x;
 return sortAsc?String(x).localeCompare(y):String(y).localeCompare(x);});
 $('#tbl tbody').innerHTML=r.map(o=>`<tr><td class="n">${o.boosts||0}</td>
  <td>${esc(o.author)}</td><td>${esc(o.published)}</td>
  <td>${o.url?`<a href="${o.url}" target="_blank">${esc(o.title)}</a>`:esc(o.title)}</td></tr>`).join('');}
document.querySelectorAll('th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;
 if(k===sortK)sortAsc=!sortAsc;else{sortK=k;sortAsc=(k==='title');}render();});
let poll=null;
$('#f').onsubmit=async e=>{e.preventDefault();
 const res=await fetch('/tv/run',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({pages:+$('#pages').value,sort:$('#sort').value,delay:+$('#delay').value})});
 if(!res.ok){const d=await res.json();$('#status').firstChild.textContent=d.error||'오류';return;}
 $('#go').disabled=true;$('#stop').disabled=false;
 if(poll)clearInterval(poll);poll=setInterval(tick,700);tick();};
$('#stop').onclick=()=>fetch('/tv/cancel',{method:'POST'});
async function tick(){const s=await (await fetch('/tv/status')).json();
 const pct=s.total?Math.round(100*s.done/s.total):0;$('#barfill').style.width=pct+'%';
 $('#status').firstChild.textContent=
  (s.running?`불러오는 중… ${s.done}/${s.total} 페이지`:`${(s.rows||[]).length}개 스크립트 (${s.done}페이지)`)
  +(s.error?(' · '+s.error):'');
 if(s.note){$('#status').firstChild.textContent+=' · '+s.note;}
 if(!s.running){rows=s.rows||[];sortK=$('#sort').value;sortAsc=(sortK==='title');render();
  clearInterval(poll);poll=null;$('#go').disabled=false;$('#stop').disabled=true;}}
</script></body></html>"""


@app.route("/tv")
def tv_index():
    return render_template_string(TV_PAGE)


_tv: Dict = {"running": False, "cancel": False, "done": 0, "total": 0,
             "rows": [], "note": None, "error": None, "sort": "boosts"}


def _tv_set(**kw):
    with _lock:
        _tv.update(kw)


def _tv_worker(pages: int, sort: str, delay: float):
    try:
        def prog(done, total, count):
            _tv_set(done=done, total=total)

        def cancelled():
            with _lock:
                return _tv["cancel"]
        res = tvs.get_scripts(pages, delay=delay, on_progress=prog, should_cancel=cancelled)
        rows = tvs.sort_rows(res["rows"], sort)
        _tv_set(rows=rows, note=res.get("note"), done=res.get("pages_fetched", pages))
    except Exception as exc:  # noqa: BLE001
        _tv_set(error=str(exc))
    finally:
        _tv_set(running=False, cancel=False)


@app.route("/tv/run", methods=["POST"])
def tv_run():
    with _lock:
        if _tv["running"]:
            return jsonify({"error": "이미 실행 중입니다"}), 409
    p = request.get_json(force=True) or {}
    pages = max(1, min(int(p.get("pages", 5)), 500))
    sort = p.get("sort", "boosts")
    delay = max(0.3, float(p.get("delay", 1.0)))
    _tv_set(running=True, cancel=False, done=0, total=pages, rows=[], note=None,
            error=None, sort=sort)
    threading.Thread(target=_tv_worker, args=(pages, sort, delay), daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/tv/status")
def tv_status():
    with _lock:
        return jsonify({k: _tv[k] for k in
                        ("running", "done", "total", "rows", "note", "error", "sort")})


@app.route("/tv/cancel", methods=["POST"])
def tv_cancel():
    _tv_set(cancel=True)
    return jsonify({"status": "cancelling"})


@app.route("/tv/raw")
def tv_raw():
    from flask import Response
    page = max(1, min(int(request.args.get("page", 1)), 500))
    try:
        return Response(tvs.fetch_page(page), mimetype="text/plain")
    except Exception as exc:  # noqa: BLE001
        return Response(f"error: {exc}", mimetype="text/plain", status=502)


@app.route("/run", methods=["POST"])
def run_route():
    with _lock:
        if _job["running"]:
            return jsonify({"error": "이미 실행 중입니다"}), 409
    params = request.get_json(force=True) or {}
    if params.get("exchange") not in EXCHANGES:
        return jsonify({"error": "unknown exchange"}), 400
    if not params.get("from"):
        return jsonify({"error": "시작 시각을 입력하세요"}), 400
    try:
        parse_start(params["from"])
        if params.get("to"):
            parse_start(params["to"])
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"날짜 형식 오류: {exc}"}), 400
    _set(running=True, cancel=False, phase="symbols", params=params, started_at=time.time())
    threading.Thread(target=_run, args=(params,), daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/status")
def status_route():
    with _lock:
        return jsonify({k: _job[k] for k in
                        ("running", "phase", "done", "total", "error", "rows",
                         "skipped_fetch", "skipped_n", "benchmark_n")})


@app.route("/cancel", methods=["POST"])
def cancel_route():
    _set(cancel=True)
    return jsonify({"status": "cancelling"})


@app.route("/csv")
def csv_route():
    from flask import Response
    with _lock:
        rows = list(_job["rows"])
        params = dict(_job["params"])
    lines = ["coin,n,corr,beta,r2,last"]
    for r in rows:
        lines.append(f'{r["symbol"]},{r["n"]},{r["corr"]:.6f},{r["beta"]:.6f},{r["r2"]:.6f},{r["last"]:.6f}')
    fname = f'corr_{params.get("exchange","x")}_{datetime.now(KST):%Y%m%d_%H%M}.csv'
    return Response("﻿" + "\n".join(lines), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


# ===========================================================================
#  Pulse Entry Engine screener  (Binance USDT-M perps, default 15m)
# ===========================================================================
_pulse: Dict = {"running": False, "cancel": False, "done": 0, "total": 0,
                "rows": [], "error": None, "at": None, "params": {}, "scan": "signal"}


def _pulse_set(**kw):
    with _lock:
        _pulse.update(kw)


def _pulse_worker(params: Dict):
    try:
        mode = params.get("mode", "Balanced")
        interval = params.get("interval", "15m")
        scan_type = params.get("scan", "signal")
        include_ready = bool(params.get("include_ready", False))
        min_score = int(params.get("min_score", 0))
        delay = max(0.0, float(params.get("delay", 0.05)))
        quote = params.get("quote", "USDT").upper()
        asset_class = params.get("asset_class", "all")

        if params.get("symbols"):
            symbols = [s.strip().upper() for s in params["symbols"].split(",") if s.strip()]
        else:
            symbols = binance.fetch_perp_symbols(quote, asset_class)
        _pulse_set(total=len(symbols), done=0, rows=[], error=None)

        rows = []
        for i, sym in enumerate(symbols, 1):
            with _lock:
                if _pulse["cancel"]:
                    break
            try:
                o, h, l, c = binance.fetch_ohlc(sym, interval, 1500)
                res = pulse.evaluate(o, h, l, c, mode=mode) if len(c) >= 1000 else None
            except Exception:  # noqa: BLE001
                res = None
            if res is not None and scan_type == "position":
                # currently in an open projection that has NOT hit any TP or SL
                if res["pos_active"] and res["pos_maxtp"] == 0 and res["pos_score"] >= min_score:
                    d = res["pos_dir"]
                    entry, now = res["pos_entry"], res["close"]
                    fav = (now - entry) if d == 1 else (entry - now)
                    rows.append({
                        "symbol": sym, "dir": "LONG" if d == 1 else "SHORT",
                        "score": res["pos_score"], "stars": pulse.stars(res["pos_score"]),
                        "entry": entry, "now": now,
                        "fav_pct": round(fav / entry * 100, 2) if entry else 0.0,
                        "tp1": res["pos_tp1"], "tp2": res["pos_tp2"], "tp3": res["pos_tp3"],
                        "sl": res["pos_sl"], "bars": res["pos_bars"]})
            elif res is not None:
                sig = "LONG" if res["new_long"] else "SHORT" if res["new_short"] else ""
                ready = "LONG" if res["long_cond"] else "SHORT" if res["short_cond"] else ""
                keep = (sig != "" or (include_ready and ready != "")) and res["score"] >= min_score
                if keep:
                    rows.append({"symbol": sym, "signal": sig, "ready": ready if sig == "" else "",
                                 "score": res["score"], "ratio": res["ratio"], "stretch": res["stretch"],
                                 "close": res["close"]})
            _pulse_set(done=i)
            if delay:
                time.sleep(delay)

        if scan_type == "position":
            rows.sort(key=lambda r: (-r["score"], -r["fav_pct"]))
        else:
            rows.sort(key=lambda r: (r["signal"] == "", -r["score"]))
        _pulse_set(rows=rows, scan=scan_type, at=datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"))
    except Exception as exc:  # noqa: BLE001
        _pulse_set(error=str(exc))
    finally:
        _pulse_set(running=False, cancel=False)


@app.route("/pulse")
def pulse_index():
    return render_template_string(PULSE_PAGE)


@app.route("/pulse/run", methods=["POST"])
def pulse_run():
    with _lock:
        if _pulse["running"]:
            return jsonify({"error": "이미 실행 중입니다"}), 409
    params = request.get_json(force=True) or {}
    _pulse_set(running=True, cancel=False, done=0, total=0, rows=[], error=None, params=params)
    threading.Thread(target=_pulse_worker, args=(params,), daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/pulse/status")
def pulse_status():
    with _lock:
        return jsonify({k: _pulse[k] for k in ("running", "done", "total", "rows", "error", "at", "scan")})


@app.route("/pulse/cancel", methods=["POST"])
def pulse_cancel():
    _pulse_set(cancel=True)
    return jsonify({"status": "cancelling"})


PULSE_PAGE = """<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pulse Entry 스크리너</title>
<style>
:root{color-scheme:dark light}*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:#0e1726;color:#e8eaed;padding:16px;max-width:1000px;margin:0 auto}
h1{font-size:18px;margin:0 0 4px;color:#00dcb4}
.sub{font-size:12px;color:#8a919e;margin:0 0 12px}
a{color:#448aff}
form{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;background:#141e30;border:1px solid #1e3a5f;border-radius:10px;padding:14px}
label{display:flex;flex-direction:column;font-size:12px;color:#8a919e;gap:4px}
input,select{background:#0e1726;color:#e8eaed;border:1px solid #1e3a5f;border-radius:6px;padding:8px;font-size:14px}
button{background:#00b39a;color:#04120f;border:0;border-radius:6px;padding:10px 18px;font-size:14px;font-weight:700;cursor:pointer}
button.stop{background:#ff4976;color:#fff}button:disabled{opacity:.5}
#status{margin:14px 0;font-size:13px;color:#8a919e}
.bar{height:8px;background:#1e3a5f;border-radius:4px;overflow:hidden;margin-top:6px}.bar>i{display:block;height:100%;width:0;background:#00dcb4;transition:width .3s}
table{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}
th,td{padding:6px 8px;text-align:right;border-bottom:1px solid #1e3a5f;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:#8a919e;cursor:pointer;user-select:none}
tbody tr:hover{background:#141e30}
.long{color:#00dcb4;font-weight:700}.short{color:#ff4976;font-weight:700}.rdy{color:#f0b90b}
.wrap{overflow-x:auto}
@media(prefers-color-scheme:light){body{background:#f5f7fa;color:#1a2233}form{background:#fff;border-color:#dce3ee}h1{color:#0a7d6c}input,select,th{background:#fff;color:#1a2233}}
</style></head><body>
<h1>⚡ Pulse Entry Engine 스크리너</h1>
<p class="sub">Binance USDT-M 선물 · 최근 <b>종료된</b> 봉 기준 LONG/SHORT 진입 신호 · <a href="/">← 상관·베타</a></p>
<form id="f">
 <label>스캔 유형<select name="scan" id="scan">
  <option value="signal">새 신호</option><option value="position">진입중 (미청산)</option>
 </select></label>
 <label>모드<select name="mode" id="mode">
  <option>Balanced</option><option>Aggressive</option><option>Scalping</option><option>Swing</option><option>Funded Account</option>
 </select></label>
 <label>인터벌<select name="interval" id="interval">
  <option value="15m" selected>15m</option><option value="5m">5m</option><option value="1m">1m</option>
  <option value="1h">1h</option><option value="4h">4h</option>
 </select></label>
 <label>자산군<select name="asset_class" id="asset_class">
  <option value="all">전체</option><option value="crypto">크립토</option><option value="tradfi">TradFi</option>
 </select></label>
 <label>최소 점수<input type="number" name="min_score" id="min_score" value="0" min="0" max="100"></label>
 <label>요청 간격(초)<input type="number" name="delay" id="delay" value="0.05" step="0.05" min="0"></label>
 <label style="flex-direction:row;align-items:center;gap:6px"><input type="checkbox" id="include_ready"> READY도 표시</label>
 <button type="submit" id="go">스캔</button>
 <button type="button" class="stop" id="stop" disabled>중지</button>
 <button type="button" id="auto">자동refresh: OFF</button>
</form>
<div id="status">대기 중…<div class="bar"><i id="barfill"></i></div></div>
<div class="wrap"><table id="tbl"><thead id="thead"></thead><tbody></tbody></table></div>
<script>
const $=s=>document.querySelector(s);let rows=[],scan='signal',sortK='',sortAsc=false,poll=null,auto=false,autoTimer=null;
function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function fp(x){if(x==null)return '—';const a=Math.abs(x);const d=a>=100?2:a>=1?4:a>=0.01?6:8;return x.toFixed(d);}
const COLS={
 signal:[['symbol','SYMBOL'],['signal','SIGNAL'],['score','SCORE'],['ratio','STRETCH×'],['stretch','STATE'],['close','CLOSE']],
 position:[['symbol','SYMBOL'],['dir','DIR'],['stars','★'],['entry','ENTRY'],['now','NOW (유리%)'],['tp1','TP1'],['tp2','TP2'],['tp3','TP3'],['sl','SL'],['bars','BARS']]
};
function header(){$('#thead').innerHTML='<tr>'+COLS[scan].map(c=>`<th data-k="${c[0]}">${c[1]}</th>`).join('')+'</tr>';
 document.querySelectorAll('#thead th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;
  if(k===sortK)sortAsc=!sortAsc;else{sortK=k;sortAsc=false;}render();});}
function row(o){
 if(scan==='position'){const c=o.dir==='LONG'?'long':'short';
  const favc=o.fav_pct>=0?'long':'short';
  return `<tr><td>${esc(o.symbol)}</td><td class="${c}">${o.dir}</td><td class="rdy">${o.stars}</td>
   <td>${fp(o.entry)}</td><td>${fp(o.now)} <span class="${favc}">(${o.fav_pct>=0?'+':''}${o.fav_pct}%)</span></td>
   <td>${fp(o.tp1)}</td><td>${fp(o.tp2)}</td><td>${fp(o.tp3)}</td><td class="short">${fp(o.sl)}</td><td>${o.bars}</td></tr>`;}
 const sig=o.signal||(o.ready?o.ready+' READY':'');const cl=o.signal==='LONG'?'long':o.signal==='SHORT'?'short':o.ready?'rdy':'';
 return `<tr><td>${esc(o.symbol)}</td><td class="${cl}">${esc(sig||'—')}</td><td>${o.score}</td><td>${o.ratio}</td><td>${esc(o.stretch)}</td><td>${fp(o.close)}</td></tr>`;}
function render(){let r=[...rows];if(sortK)r.sort((a,b)=>{let x=a[sortK],y=b[sortK];
  if(typeof x==='number')return sortAsc?x-y:y-x;return sortAsc?String(x).localeCompare(y):String(y).localeCompare(x);});
 $('#tbl tbody').innerHTML=r.map(row).join('');}
async function startScan(){const p=Object.fromEntries(new FormData($('#f')));p.include_ready=$('#include_ready').checked;
 scan=p.scan;sortK='';header();
 const res=await fetch('/pulse/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});
 if(!res.ok){$('#status').firstChild.textContent=(await res.json()).error||'오류';return;}
 $('#go').disabled=true;$('#stop').disabled=false;if(poll)clearInterval(poll);poll=setInterval(tick,700);tick();}
$('#f').onsubmit=e=>{e.preventDefault();startScan();};
$('#scan').onchange=()=>{scan=$('#scan').value;sortK='';header();rows=[];render();};
$('#stop').onclick=()=>fetch('/pulse/cancel',{method:'POST'});
$('#auto').onclick=()=>{auto=!auto;$('#auto').textContent='자동refresh: '+(auto?'ON(60s)':'OFF');if(auto&&!poll)startScan();};
async function tick(){const s=await (await fetch('/pulse/status')).json();
 const pct=s.total?Math.round(100*s.done/s.total):0;$('#barfill').style.width=pct+'%';
 if(s.scan)scan=s.scan;
 const lbl=scan==='position'?'진입중':'신호';
 $('#status').firstChild.textContent=(s.running?`스캔 중… ${s.done}/${s.total}`
  :`완료 · ${lbl} ${(s.rows||[]).length}건 · ${s.at||''}`)+(s.error?(' · '+s.error):'');
 rows=s.rows||[];render();
 if(!s.running){clearInterval(poll);poll=null;$('#go').disabled=false;$('#stop').disabled=true;
  if(auto){if(autoTimer)clearTimeout(autoTimer);autoTimer=setTimeout(startScan,60000);}}}
header();
</script></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description="BTC correlation/beta web UI")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    print(f"* Correlation/beta UI on http://{args.host}:{args.port}")
    print(f"* Open http://<your-vm-ip>:{args.port} in a browser.")
    print(f"* Oracle Cloud: open the port first —")
    print(f"    sudo firewall-cmd --permanent --add-port={args.port}/tcp && sudo firewall-cmd --reload")
    print(f"    (and add an Ingress rule for TCP {args.port} in the VCN Security List / NSG)")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
