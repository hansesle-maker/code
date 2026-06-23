#!/usr/bin/env python3
"""Scan Binance USDT-M futures, render a STATIC dashboard + data.json, and
(optionally) push Telegram alerts.

This is the "no server" path: GitHub Actions runs it every 15 minutes, the
generated ``public/`` folder is published to GitHub Pages, and the page is
viewable on any phone with no backend running. See MOBILE_SETUP.md.

    python generate_static.py --out public --prev prev.json
    python generate_static.py --out public --limit 20   # quick local test

Telegram push is enabled only when both TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID are present in the environment.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import List

from jinja2 import Environment, FileSystemLoader, select_autoescape

from tsi_signal.alerts import (
    build_messages,
    diff_alerts,
    load_prev_map,
    send_telegram,
)
from tsi_signal.scanner import (
    KLINE_LIMIT,
    TIMEFRAMES,
    SymbolScan,
    fetch_all_futures_symbols,
    scan_all,
    symbolscan_to_dict,
)

TEMPLATE_DIR = Path(__file__).parent / "templates"


def _fmt_tsi(v: float) -> str:
    return f"{v:+.2f}"


def render_site(results: List[SymbolScan], scanned_at: datetime.datetime,
                out_dir: str) -> None:
    """Write index.html + data.json + .nojekyll into ``out_dir``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["fmt_tsi"] = _fmt_tsi
    html = env.get_template("dashboard.html").render(
        results=results,
        scanned_at=scanned_at,
        scanning=False,
        error=None,
        static_mode=True,
    )
    (out / "index.html").write_text(html, encoding="utf-8")

    payload = {
        "scanned_at": scanned_at.isoformat() + "Z",
        "count": len(results),
        "symbols": [symbolscan_to_dict(r) for r in results],
    }
    (out / "data.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    # Tell GitHub Pages not to run Jekyll (serve files verbatim).
    (out / ".nojekyll").write_text("", encoding="utf-8")


def run_alerts(prev_path, results, scanned_at, enabled: bool = True) -> int:
    """Diff against the previous scan and push Telegram alerts. Returns count."""
    prev_map = load_prev_map(prev_path)
    groups = diff_alerts(prev_map, results)
    total = sum(len(v) for v in groups.values())
    messages = build_messages(groups, scanned_at)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")

    if not messages:
        print("No notable changes to alert.")
        return 0
    if not (token and chat):
        print(f"{total} notable changes, but TELEGRAM_BOT_TOKEN/CHAT_ID not "
              "set — skipping push.")
        return total
    if not enabled:
        print(f"{total} notable changes (Telegram disabled via flag).")
        return total

    for msg in messages:
        ok = send_telegram(token, chat, msg)
        print("Telegram:", "sent" if ok else "FAILED")
    return total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Static TSI dashboard generator")
    ap.add_argument("--out", default="public", help="output directory")
    ap.add_argument("--prev", default=None,
                    help="previous data.json to diff for alerts")
    ap.add_argument("--workers", type=int, default=6,
                    help="concurrent fetch workers (default 6 keeps weight under Binance 2400/min limit)")
    ap.add_argument("--limit", type=int, default=0,
                    help="scan only the first N symbols (debugging)")
    ap.add_argument("--no-telegram", action="store_true",
                    help="compute alerts but do not send them")
    args = ap.parse_args(argv)

    try:
        symbols = fetch_all_futures_symbols()
    except Exception as exc:
        print(f"ERROR: cannot reach Binance futures API: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        print("If this is HTTP 451/403, the runner's region is geo-blocked by "
              "Binance. See MOBILE_SETUP.md → 'Binance 지역 차단'.",
              file=sys.stderr)
        return 2

    if args.limit:
        symbols = symbols[: args.limit]

    print(f"Scanning {len(symbols)} symbols × {len(TIMEFRAMES)} timeframes "
          f"({args.workers} workers, KLINE_LIMIT={KLINE_LIMIT}) …")
    results = scan_all(symbols, max_workers=args.workers)
    scanned_at = datetime.datetime.utcnow()

    ok   = sum(1 for r in results if any(r.tf.values()))
    fail = len(results) - ok
    print(f"Scan complete: {ok}/{len(results)} with data"
          + (f", {fail} empty (rate-limited or error)" if fail else "") + ".")

    if fail > len(results) * 0.5:
        print("WARNING: >50% of symbols have no data — likely rate-limited.",
              file=sys.stderr)
        print("Try --workers 3 or wait a minute and re-run.", file=sys.stderr)

    run_alerts(args.prev, results, scanned_at, enabled=not args.no_telegram)

    render_site(results, scanned_at, args.out)
    print(f"Wrote {args.out}/index.html and {args.out}/data.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
