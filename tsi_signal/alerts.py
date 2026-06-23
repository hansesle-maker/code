"""Diff two scans and push Telegram alerts on notable TSI state changes.

Used by ``generate_static.py`` on GitHub Actions: it reads the *previous*
``data.json`` (the last deployed scan), compares it to the fresh scan, and
pushes only the symbols whose state actually changed — so the phone gets a
short, meaningful notification instead of 300 rows every 15 minutes.

What counts as "notable" (all evaluated on closed bars):
    - a symbol newly reaching 3/3 bullish (TSI>0 & >signal on 4h, 1h, 15m)
    - a symbol newly reaching 3/3 bearish (mirror)
    - the 4h TSI crossing its signal line (up or down)
    - the 4h TSI crossing the zero line (up or down)

The first run (no previous data) only establishes a baseline — it sends
nothing, to avoid alerting on every already-aligned symbol.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from .scanner import SymbolScan

# Alert category -> emoji-prefixed title (ordering controls message layout).
# High-priority (fresh cross on already-aligned symbols) listed first.
CROSS_15M_BULL = "🔥 3/3 강세 + 15m signal 상향 교차 (진입 타이밍)"
CROSS_15M_BEAR = "🔥 3/3 약세 + 15m signal 하향 교차 (진입 타이밍)"
BULL3 = "🟢 3/3 강세 정렬 (신규)"
BEAR3 = "🔴 3/3 약세 정렬 (신규)"
SIG_UP = "⚡ 4h TSI가 signal 상향 돌파"
SIG_DN = "🔻 4h TSI가 signal 하향 이탈"
ZERO_UP = "📈 4h TSI가 0선 상향 돌파"
ZERO_DN = "📉 4h TSI가 0선 하향 이탈"

_CATEGORIES = (CROSS_15M_BULL, CROSS_15M_BEAR, BULL3, BEAR3, SIG_UP, SIG_DN, ZERO_UP, ZERO_DN)


def load_prev_map(path: Optional[str]) -> Dict[str, dict]:
    """Load the previous data.json into ``{symbol: record}``; {} if missing."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return {s["symbol"]: s for s in data.get("symbols", []) if "symbol" in s}


def _prev_tf(prev: Optional[dict], tf: str) -> Optional[dict]:
    if not prev:
        return None
    return prev.get("tf", {}).get(tf)


def _line(r: SymbolScan) -> str:
    """Compact one-line summary: ``BTCUSDT  4h +12.3 1h +5.1 15m +3.2``."""
    def t(tf: str) -> str:
        s = r.tf.get(tf)
        return f"{tf} {s.tsi:+.1f}" if s else f"{tf} —"
    return f"{r.symbol}  {t('4h')} {t('1h')} {t('15m')}"


def diff_alerts(
    prev_map: Dict[str, dict], results: List[SymbolScan]
) -> Dict[str, List[str]]:
    """Return ``{category_title: [symbol_lines]}`` for everything that changed."""
    groups: Dict[str, List[str]] = {c: [] for c in _CATEGORIES}

    # No baseline yet -> don't alert on pre-existing states.
    if not prev_map:
        return groups

    for r in results:
        prev = prev_map.get(r.symbol)
        prev_bull = prev.get("bull_score") if prev else None
        prev_bear = prev.get("bear_score") if prev else None

        # 15m fresh cross on a fully-aligned symbol — highest-priority alert.
        # fresh_cross is single-bar information (computed from klines), so no
        # prev-scan diff needed; it fires only on the exact bar of the cross.
        s15 = r.tf.get("15m")
        if s15 and s15.fresh_cross == 1 and r.bull_score == 3:
            groups[CROSS_15M_BULL].append(_line(r))
        if s15 and s15.fresh_cross == -1 and r.bear_score == 3:
            groups[CROSS_15M_BEAR].append(_line(r))

        if r.bull_score == 3 and prev_bull is not None and prev_bull < 3:
            groups[BULL3].append(_line(r))
        if r.bear_score == 3 and prev_bear is not None and prev_bear < 3:
            groups[BEAR3].append(_line(r))

        new4 = r.tf.get("4h")
        p4 = _prev_tf(prev, "4h")
        if new4 and p4:
            if new4.above_signal and not p4.get("above_signal"):
                groups[SIG_UP].append(_line(r))
            elif not new4.above_signal and p4.get("above_signal"):
                groups[SIG_DN].append(_line(r))
            if new4.above_zero and not p4.get("above_zero"):
                groups[ZERO_UP].append(_line(r))
            elif not new4.above_zero and p4.get("above_zero"):
                groups[ZERO_DN].append(_line(r))

    return groups


def build_messages(
    groups: Dict[str, List[str]],
    scanned_at,
    max_per_group: int = 25,
    max_len: int = 3500,
) -> List[str]:
    """Render alert groups into one or more Telegram-sized text messages."""
    blocks: List[str] = []
    for title in _CATEGORIES:
        items = groups.get(title) or []
        if not items:
            continue
        shown = items[:max_per_group]
        body = "\n".join(shown)
        if len(items) > max_per_group:
            body += f"\n… 외 {len(items) - max_per_group}개"
        blocks.append(f"{title} ({len(items)})\n{body}")

    if not blocks:
        return []

    header = f"🔔 TSI 알림 · {scanned_at.strftime('%m-%d %H:%M')} UTC"
    messages: List[str] = []
    current = header
    for block in blocks:
        if len(current) + len(block) + 2 > max_len:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}"
    messages.append(current)
    return messages


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    """POST a plain-text message to a Telegram chat via the Bot API."""
    import requests

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"Telegram API {resp.status_code}: {resp.text[:200]}")
        return resp.status_code == 200
    except Exception as exc:
        print(f"Telegram send failed: {exc}")
        return False
