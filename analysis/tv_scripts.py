#!/usr/bin/env python3
"""Fetch TradingView's public scripts listing and sort it yourself.

TradingView's /scripts/ pages let you browse Editors' picks / Trending /
Recently published, but you can't get a combined, exportable, custom-sorted
table. This grabs the public listing pages, parses each script card (title,
author, boosts, published date, URL), and lets you sort by boosts or date.

⚠️  TradingView's Terms of Service restrict automated access. Use this only
for light, personal, low-frequency browsing. Do not hammer the site or
redistribute the data. TradingView also uses Cloudflare and renders much of
the page client-side, so a plain HTTP fetch may return a challenge page or no
cards — run with --dump to inspect what actually came back, and the parser can
then be calibrated to the real markup.

Examples
--------
    python analysis/tv_scripts.py --pages 3 --sort boosts
    python analysis/tv_scripts.py --pages 5 --sort date --csv scripts.csv
    python analysis/tv_scripts.py --dump 1 > page1.html   # inspect raw HTML
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re
import sys
import time
from typing import Dict, List, Optional

import requests

BASE = "https://www.tradingview.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def fetch_page(page: int = 1, timeout: int = 25) -> str:
    """Return the raw HTML of scripts listing page `page` (1-indexed)."""
    url = f"{BASE}/scripts/" if page <= 1 else f"{BASE}/scripts/page-{page}/"
    r = requests.get(url, timeout=timeout, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    r.raise_for_status()
    return r.text


# --------------------------------------------------------------------------- #
# Parsing — two strategies, tried in order.
# --------------------------------------------------------------------------- #
def _to_int(s) -> int:
    """'1.2K' / '3,456' / '' -> int."""
    if s is None:
        return 0
    t = str(s).strip().replace(",", "")
    m = re.match(r"([\d.]+)\s*([KkMm]?)", t)
    if not m:
        return 0
    num = float(m.group(1)) if m.group(1) else 0.0
    mult = {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    return int(num * mult)


def _parse_embedded_json(html: str) -> List[Dict]:
    """TradingView often embeds page state as JSON in a <script> tag. Pull any
    objects that look like a script card (have a name/title + author + a
    boost/like/agree count)."""
    out: List[Dict] = []
    # candidate JSON blobs
    blobs = re.findall(r'>\s*(\{.*?\})\s*</script>', html, re.DOTALL)
    blobs += re.findall(r'window\.[\w.]+\s*=\s*(\{.*?\});', html, re.DOTALL)
    for blob in blobs:
        try:
            data = json.loads(blob)
        except Exception:  # noqa: BLE001
            continue
        _walk_json(data, out)
        if out:
            break
    return out


def _walk_json(node, out: List[Dict]) -> None:
    if isinstance(node, dict):
        keys = {k.lower() for k in node.keys()}
        title = node.get("name") or node.get("title") or node.get("scriptName")
        boostish = next((node[k] for k in node
                         if k.lower() in ("agrees", "agree_count", "boosts",
                                          "boost_count", "likes", "likes_count")), None)
        if title and boostish is not None:
            author = node.get("username") or node.get("author") or ""
            if isinstance(author, dict):
                author = author.get("username") or author.get("name") or ""
            url = node.get("published_url") or node.get("url") or node.get("scriptIdPart") or ""
            if url and url.startswith("/"):
                url = BASE + url
            out.append({
                "title": _html.unescape(str(title)).strip(),
                "author": str(author),
                "boosts": _to_int(boostish),
                "published": str(node.get("created") or node.get("date_created")
                                 or node.get("created_at") or ""),
                "url": url,
            })
        for v in node.values():
            _walk_json(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_json(v, out)


def _parse_html_cards(html: str) -> List[Dict]:
    """Fallback: pull /script/ anchors and any nearby boost count. Coarser, but
    works when only rendered HTML links are present."""
    out: List[Dict] = []
    # each script links to /script/<id>-<slug>/
    for m in re.finditer(r'href="(/script/[^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL):
        href, inner = m.group(1), m.group(2)
        title = _html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        if not title:
            continue
        out.append({"title": title, "author": "", "boosts": 0,
                    "published": "", "url": BASE + href})
    # de-dup by url, keep first
    seen, uniq = set(), []
    for r in out:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        uniq.append(r)
    return uniq


def parse_scripts(html: str) -> List[Dict]:
    items = _parse_embedded_json(html)
    if not items:
        items = _parse_html_cards(html)
    return items


def looks_blocked(html: str) -> Optional[str]:
    low = html.lower()
    if "cloudflare" in low and ("captcha" in low or "challenge" in low or "cf-" in low):
        return "Cloudflare challenge page"
    if len(html) < 800:
        return "suspiciously short response"
    if "/script/" not in html and '"agrees"' not in low and "name" not in low:
        return "no script data found (likely client-rendered or blocked)"
    return None


def get_scripts(pages: int = 1, delay: float = 1.0) -> Dict:
    """Fetch `pages` listing pages, parse and merge. Returns dict with rows +
    a diagnostic note if nothing parsed."""
    rows: List[Dict] = []
    note = None
    for p in range(1, pages + 1):
        html = fetch_page(p)
        if p == 1:
            note = looks_blocked(html)
        rows.extend(parse_scripts(html))
        if delay and p < pages:
            time.sleep(delay)
    # de-dup by url
    seen, uniq = set(), []
    for r in rows:
        key = r.get("url") or r.get("title")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    return {"rows": uniq, "note": note}


def sort_rows(rows: List[Dict], by: str) -> List[Dict]:
    if by == "boosts":
        return sorted(rows, key=lambda r: r.get("boosts", 0), reverse=True)
    if by == "date":
        return sorted(rows, key=lambda r: str(r.get("published", "")), reverse=True)
    if by == "title":
        return sorted(rows, key=lambda r: r.get("title", "").lower())
    return rows


# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pages", type=int, default=1, help="listing pages to fetch (default 1)")
    ap.add_argument("--sort", default="boosts", choices=["boosts", "date", "title"])
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between page fetches")
    ap.add_argument("--csv", default="", help="write results to this CSV path")
    ap.add_argument("--dump", type=int, metavar="PAGE",
                    help="print raw HTML of PAGE and exit (for calibrating the parser)")
    args = ap.parse_args(argv)

    if args.dump:
        try:
            sys.stdout.write(fetch_page(args.dump))
        except Exception as exc:  # noqa: BLE001
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        res = get_scripts(args.pages, args.delay)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1
    rows = sort_rows(res["rows"], args.sort)
    if res["note"]:
        print(f"# note: {res['note']} — try --dump 1 to inspect", file=sys.stderr)
    print(f"# {len(rows)} scripts (sorted by {args.sort})", file=sys.stderr)
    print(f"{'BOOSTS':>8}  {'AUTHOR':<18}  TITLE")
    print("-" * 70)
    for r in rows:
        print(f"{r.get('boosts', 0):>8}  {r.get('author', ''):<18.18}  {r.get('title', '')[:44]}")
    if args.csv:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["boosts", "author", "published", "title", "url"])
            for r in rows:
                w.writerow([r.get("boosts", 0), r.get("author", ""), r.get("published", ""),
                            r.get("title", ""), r.get("url", "")])
        print(f"# wrote {args.csv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
