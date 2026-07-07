#!/usr/bin/env python3
"""Pulse Entry Engine [trade_w_samet] — signal logic ported to Python.

Screens for the indicator's LONG / SHORT "pulse" entry on the latest *closed*
bar. The engine's core oscillator is Martin Pring's Special K (the same series
TradingView's `ta.specialK` returns); the 724-bar history requirement in the
original Pine (longest ROC 530 + SMA 195) confirms the classic fixed-weight
formula. The signal line is an SMA of Special K (length1, default 100).

Everything downstream of Special K — distance/stretch, zero & EMA reversal
filters, 0–100 reversal score, the mode presets, the signal-state machine and
the TP/SL trade-block gate — is ported faithfully from the Pine source.

⚠️ specialK note: the classic Special K formula (below) matches Pring and the
724-bar warmup. If TradingView's `ta.specialK` smooths the signal line
differently (e.g. a second pass with length2), paste the library source and the
`_signal_line` function can be made exact. Everything else is a 1:1 port.

Dependency-free (stdlib only) so the math can be unit-tested offline.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

NAN = float("nan")


def _isnan(x: float) -> bool:
    return x != x


# --------------------------------------------------------------------------- #
# Series helpers (index 0 = oldest bar), NaN during warmup — mirrors Pine na.
# --------------------------------------------------------------------------- #
def roc(src: List[float], length: int) -> List[float]:
    out = [NAN] * len(src)
    for i in range(length, len(src)):
        p = src[i - length]
        out[i] = 100.0 * (src[i] - p) / p if p != 0 else NAN
    return out


def sma(src: List[float], length: int) -> List[float]:
    """Rolling SMA, O(n). NaN in the stream resets the window (matches Pine na)."""
    from collections import deque
    out = [NAN] * len(src)
    dq: deque = deque()
    run = 0.0
    for i, v in enumerate(src):
        if _isnan(v):
            dq.clear()
            run = 0.0
            continue
        dq.append(v)
        run += v
        if len(dq) > length:
            run -= dq.popleft()
        if len(dq) == length:
            out[i] = run / length
    return out


def ema(src: List[float], length: int) -> List[float]:
    out = [NAN] * len(src)
    alpha = 2.0 / (length + 1)
    prev = NAN
    for i, v in enumerate(src):
        if _isnan(v):
            continue
        prev = v if _isnan(prev) else alpha * v + (1 - alpha) * prev
        out[i] = prev
    return out


def rma(src: List[float], length: int) -> List[float]:
    """Wilder's smoothing (used by ta.atr), seeded with the first SMA."""
    out = [NAN] * len(src)
    prev = NAN
    for i in range(len(src)):
        if _isnan(src[i]):
            continue
        if _isnan(prev):
            window = src[max(0, i - length + 1):i + 1]
            if len(window) >= length and not any(_isnan(v) for v in window):
                prev = sum(window) / length
                out[i] = prev
        else:
            prev = (prev * (length - 1) + src[i]) / length
            out[i] = prev
    return out


def atr(high: List[float], low: List[float], close: List[float], length: int) -> List[float]:
    n = len(close)
    tr = [NAN] * n
    for i in range(n):
        if i == 0:
            tr[i] = high[i] - low[i]
        else:
            tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    return rma(tr, length)


# --------------------------------------------------------------------------- #
# Pring's Special K — fixed 12-component weighted sum of SMA(ROC).
# --------------------------------------------------------------------------- #
# (roc_len, sma_len, weight) — exact weights from TradingView's ta.specialK:
#   sum group ×1 + group ×2 + group ×3 + group ×4  (NOT classic Pring weights).
_SK_COMPONENTS = [
    (10, 10, 1), (40, 50, 1), (195, 130, 1),
    (15, 10, 2), (65, 65, 2), (265, 130, 2),
    (20, 10, 3), (75, 75, 3), (390, 130, 3),
    (30, 15, 4), (100, 100, 4), (530, 195, 4),
]


def special_k(close: List[float]) -> List[float]:
    n = len(close)
    comps = []
    for roc_len, sma_len, weight in _SK_COMPONENTS:
        comps.append((sma(roc(close, roc_len), sma_len), weight))
    out = [NAN] * n
    for i in range(n):
        total = 0.0
        ok = True
        for series, weight in comps:
            v = series[i]
            if _isnan(v):
                ok = False
                break
            total += v * weight
        if ok:
            out[i] = total
    return out


def _signal_line(sk: List[float], length1: int, length2: int) -> List[float]:
    # ta.specialK signal = double SMA: sma(sma(sk, sigLen1), sigLen2).
    return sma(sma(sk, length1), length2)


# --------------------------------------------------------------------------- #
# Mode presets (from the Pine `modeX` ternaries).
# --------------------------------------------------------------------------- #
MODES = {
    "Aggressive":     dict(dist=1.10, ext=1.70, minScore=45.0, strict=False),
    "Balanced":       dict(dist=1.50, ext=2.00, minScore=55.0, strict=False),
    "Scalping":       dict(dist=1.25, ext=1.85, minScore=60.0, strict=False),
    "Swing":          dict(dist=1.70, ext=2.20, minScore=70.0, strict=True),
    "Funded Account": dict(dist=1.90, ext=2.35, minScore=78.0, strict=True),
}


def stars(score: float) -> str:
    """Star rating exactly as the indicator maps its reversal score."""
    return ("★★★★★" if score >= 96 else "★★★★" if score >= 92 else "★★★" if score >= 88
            else "★★" if score >= 84 else "★" if score >= 80 else "☆")


def _stretch_state(ratio: float, dist: float, ext: float) -> str:
    if ratio >= ext:
        return "EXTREME"
    if ratio >= dist:
        return "STRONG"
    if ratio >= 1.0:
        return "NORMAL"
    return "WEAK"


# --------------------------------------------------------------------------- #
# Full per-bar evaluation with the signal-state machine + TP/SL block gate.
# --------------------------------------------------------------------------- #
def evaluate(open_: List[float], high: List[float], low: List[float], close: List[float],
             mode: str = "Balanced",
             use_ema: bool = True, ema_length: int = 100,
             use_zero: bool = True,
             distance_length: int = 50, length1: int = 100, length2: int = 100,
             reset_when_weak: bool = True, reset_mult: float = 0.80,
             risk_atr_length: int = 14, sl_atr_mult: float = 2.5, tp_rr: float = 1.5,
             same_bar_result: str = "SL") -> Optional[Dict]:
    """Run the engine over history; return the latest *closed* bar's state, or
    None if there isn't enough history for a valid Special K value."""
    n = len(close)
    m = MODES.get(mode, MODES["Balanced"])
    strict = m["strict"]
    dist_mult, ext_mult, min_score = m["dist"], m["ext"], m["minScore"]
    eff_ema = use_ema or strict
    eff_zero = use_zero or strict

    sk = special_k(close)
    sig = _signal_line(sk, length1, length2)
    ema_c = ema(close, ema_length)
    risk_atr = atr(high, low, close, risk_atr_length)

    distance = [abs(sk[i] - sig[i]) if not (_isnan(sk[i]) or _isnan(sig[i])) else NAN for i in range(n)]
    avg_dist = sma(distance, distance_length)

    # first bar where everything needed is valid
    start = next((i for i in range(n) if not (_isnan(sk[i]) or _isnan(sig[i]) or _isnan(ema_c[i])
                  or _isnan(avg_dist[i]) or avg_dist[i] == 0)), None)
    if start is None:
        return None

    signal_state = 0
    # single active trade model (blocking = active and no TP hit yet)
    tr_active = False
    tr_dir = 0
    tr_entry = tr_sl = tr_tp1 = tr_tp2 = tr_tp3 = 0.0
    tr_maxtp = 0
    tr_score = 0
    tr_start = 0

    last = None
    for i in range(start, n):
        ratio = distance[i] / avg_dist[i]
        dist_strong = ratio >= dist_mult
        dist_weak = ratio <= reset_mult
        setup_dir = 1 if sk[i] < sig[i] else -1 if sk[i] > sig[i] else 0

        long_zero_ok = (not eff_zero) or sk[i] < 0
        short_zero_ok = (not eff_zero) or sk[i] > 0
        long_ema_ok = (not eff_ema) or close[i] < ema_c[i]
        short_ema_ok = (not eff_ema) or close[i] > ema_c[i]

        # 0–100 reversal score
        direction_score = 15.0 if setup_dir != 0 else 0.0
        stretch_score = min(ratio / ext_mult, 1.0) * 40.0
        zero_score = 15.0 if ((setup_dir == 1 and long_zero_ok) or (setup_dir == -1 and short_zero_ok)
                              or not eff_zero) else 0.0
        ema_score = 20.0 if ((setup_dir == 1 and long_ema_ok) or (setup_dir == -1 and short_ema_ok)
                             or not eff_ema) else 0.0
        strength_bonus = 10.0 if dist_strong else 0.0
        score = max(0.0, min(100.0, direction_score + stretch_score + zero_score + ema_score + strength_bonus))

        long_base = sk[i] < sig[i] and dist_strong and long_zero_ok and long_ema_ok
        short_base = sk[i] > sig[i] and dist_strong and short_zero_ok and short_ema_ok
        mode_score_ok = score >= min_score
        long_cond = long_base and mode_score_ok
        short_cond = short_base and mode_score_ok

        # --- update active trade first (hits use THIS bar's high/low) ---
        if tr_active and i > 0:
            if tr_dir == 1:
                tp1h, tp2h, tp3h = high[i] >= tr_tp1, high[i] >= tr_tp2, high[i] >= tr_tp3
                slh = low[i] <= tr_sl
            else:
                tp1h, tp2h, tp3h = low[i] <= tr_tp1, low[i] <= tr_tp2, low[i] <= tr_tp3
                slh = high[i] >= tr_sl
            same_bar = slh and (tp1h or tp2h or tp3h)
            prior = tr_maxtp
            if not same_bar:
                if tp1h:
                    tr_maxtp = max(tr_maxtp, 1)
                if tp2h:
                    tr_maxtp = max(tr_maxtp, 2)
                if tp3h:
                    tr_maxtp = max(tr_maxtp, 3)
            result = 0
            if same_bar:
                if same_bar_result == "TP1":
                    tr_maxtp = max(prior, 1)
                    result = tr_maxtp
                else:
                    result = prior if prior > 0 else -1
            else:
                if tp3h:
                    result = 3
                elif slh:
                    result = tr_maxtp if tr_maxtp > 0 else -1
            if result != 0:
                tr_active = False

        blocking = tr_active and tr_maxtp == 0
        replaceable = tr_active and tr_maxtp > 0

        if reset_when_weak and dist_weak and not long_cond and not short_cond:
            signal_state = 0

        cand_long = long_cond and signal_state != 1
        cand_short = short_cond and signal_state != -1
        new_long = cand_long and not blocking
        new_short = cand_short and not blocking
        if new_long:
            signal_state = 1
        if new_short:
            signal_state = -1

        # open / replace trades
        if (new_long or new_short) and replaceable:
            tr_active = False
        if new_long:
            entry = close[i]
            sl = entry - risk_atr[i] * sl_atr_mult
            tp3 = entry + (entry - sl) * tp_rr
            tr_active, tr_dir, tr_entry, tr_sl = True, 1, entry, sl
            tr_tp3, tr_tp1, tr_tp2 = tp3, entry + (tp3 - entry) * 0.25, entry + (tp3 - entry) * 0.50
            tr_maxtp, tr_score, tr_start = 0, round(score), i
        elif new_short:
            entry = close[i]
            sl = entry + risk_atr[i] * sl_atr_mult
            tp3 = entry - (sl - entry) * tp_rr
            tr_active, tr_dir, tr_entry, tr_sl = True, -1, entry, sl
            tr_tp3, tr_tp1, tr_tp2 = tp3, entry - (entry - tp3) * 0.25, entry - (entry - tp3) * 0.50
            tr_maxtp, tr_score, tr_start = 0, round(score), i

        last = {
            "new_long": new_long, "new_short": new_short,
            "long_cond": long_cond, "short_cond": short_cond,
            "score": round(score), "ratio": round(ratio, 2),
            "stretch": _stretch_state(ratio, dist_mult, ext_mult),
            "specialK": sk[i], "signal": sig[i], "close": close[i],
            "state": signal_state,
        }

    # active-trade snapshot as of the latest bar (an open TP/SL projection)
    if last is not None:
        last["pos_active"] = tr_active
        last["pos_dir"] = tr_dir if tr_active else 0
        last["pos_entry"] = tr_entry if tr_active else None
        last["pos_sl"] = tr_sl if tr_active else None
        last["pos_tp1"] = tr_tp1 if tr_active else None
        last["pos_tp2"] = tr_tp2 if tr_active else None
        last["pos_tp3"] = tr_tp3 if tr_active else None
        last["pos_score"] = tr_score if tr_active else 0
        last["pos_maxtp"] = tr_maxtp if tr_active else 0
        last["pos_bars"] = (n - 1 - tr_start) if tr_active else 0
    return last


# --------------------------------------------------------------------------- #
def self_test() -> int:
    import random
    random.seed(3)
    # random-walk price with enough bars for Special K + double-SMA signal (~924)
    n = 1400
    c = [100.0]
    for _ in range(n - 1):
        c.append(max(1.0, c[-1] * (1 + random.gauss(0, 0.01))))
    o = [c[max(0, i - 1)] for i in range(n)]
    h = [max(o[i], c[i]) * (1 + abs(random.gauss(0, 0.003))) for i in range(n)]
    l = [min(o[i], c[i]) * (1 - abs(random.gauss(0, 0.003))) for i in range(n)]

    sk = special_k(c)
    assert not _isnan(sk[-1]), "Special K should be valid at the last bar"
    # 724-bar warmup: index 724 valid, earlier not
    assert _isnan(sk[700]) and not _isnan(sk[730]), "Special K warmup should land near bar 724"
    res = evaluate(o, h, l, c, mode="Balanced")
    assert res is not None and "new_long" in res and 0 <= res["score"] <= 100
    print(f"self-test OK — last bar: score={res['score']} stretch={res['stretch']} "
          f"long={res['new_long']} short={res['new_short']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
