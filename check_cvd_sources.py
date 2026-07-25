#!/usr/bin/env python3
"""거래소 데이터 소스 자체 점검 (CVD 스캐너용).

바이낸스/빗썸 REST 응답 형식과 CVD 다이버전스 계산을 실제 네트워크로
확인한다. 개발 샌드박스에서는 거래소 접속이 차단되므로, VM에서 이 스크립트로
검증한다::

    python check_cvd_sources.py                 # 두 거래소 모두
    python check_cvd_sources.py --exchange bithumb --symbol KRW-BTC

문제가 있으면 종료 코드가 1이고, 어떤 타임프레임이 어떤 이유로 실패했는지
출력한다.
"""
from __future__ import annotations

import argparse
import datetime
import sys
import traceback

from tsi_signal.cvd import scan_divergence, strength_label
from tsi_signal.exchanges import (
    BINANCE, BITHUMB, EXCHANGE_LABELS, TIMEFRAMES,
    get_candles, get_universe,
)

DEFAULT_SYMBOL = {BINANCE: "BTCUSDT", BITHUMB: "KRW-BTC"}


def _iso(ms: int) -> str:
    return datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M UTC")


def check(exchange: str, symbol: str | None, need: int) -> int:
    print(f"\n=== {EXCHANGE_LABELS[exchange]} ===")
    fails = 0

    try:
        uni = get_universe(exchange, 5)
        print(f"종목 목록 OK — 거래대금 상위 5: {', '.join(uni)}")
    except Exception as exc:
        print(f"❌ 종목 목록 실패: {exc}")
        traceback.print_exc(limit=2)
        return 1

    sym = symbol or DEFAULT_SYMBOL.get(exchange) or uni[0]
    print(f"기준 심볼: {sym}")

    for tf in TIMEFRAMES:
        try:
            candles = get_candles(exchange, sym, tf, need)
        except Exception as exc:
            print(f"  ❌ {tf:>4}: 캔들 조회 실패 — {exc}")
            fails += 1
            continue

        if len(candles) < 90:
            print(f"  ⚠️  {tf:>4}: 봉 {len(candles)}개 — 지표 최소치(90) 미달")
            fails += 1
            continue

        times = [c.open_time for c in candles]
        if times != sorted(times):
            print(f"  ❌ {tf:>4}: 시간순 정렬 아님")
            fails += 1
            continue
        gaps = {times[i + 1] - times[i] for i in range(len(times) - 1)}
        sig = scan_divergence(candles)
        desc = ("신호 없음" if sig is None else
                f"{sig.direction} {sig.bars_ago}봉전 "
                f"강도{sig.strength}({strength_label(sig.strength)})"
                f"{'' if sig.active else ' [만료]'}"
                f"{' [미확정]' if sig.provisional else ''}")
        print(f"  ✅ {tf:>4}: {len(candles):>4}봉  마지막 {_iso(times[-1])}  "
              f"간격 {sorted(gaps)[:2]}ms  → {desc}")

    return fails


def main() -> None:
    ap = argparse.ArgumentParser(description="CVD 데이터 소스 점검")
    ap.add_argument("--exchange", choices=[BINANCE, BITHUMB, "all"], default="all")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--need", type=int, default=150)
    args = ap.parse_args()

    targets = [BINANCE, BITHUMB] if args.exchange == "all" else [args.exchange]
    total = sum(check(ex, args.symbol, args.need) for ex in targets)

    print()
    if total:
        print(f"❌ 실패 {total}건 — 위 오류 메시지를 확인하세요.")
        sys.exit(1)
    print("✅ 모든 점검 통과")


if __name__ == "__main__":
    main()
