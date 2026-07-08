"""Smart Money Concepts engine — Python port of pine/smart_money_concepts_strategy.pine.

Processes closed candles bar-by-bar in the same execution order as the Pine
strategy and returns the full trading state at the last bar:

- simulated position (LONG/SHORT/FLAT) with entry, stop loss & take profit
- swing / internal market structure trends and the last BOS/CHoCH signal
- premium/discount zone location within the trailing swing range
- trailing strong/weak high & low levels and the equilibrium level
- nearest unmitigated order blocks and fair value gaps
- EQH/EQL detection and simple in-window simulation stats

Pure standard library — no network, no third-party deps — so it can be unit
tested offline with :func:`tsi_signal.data.synthetic_candles`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .data import Candle

BULLISH = 1
BEARISH = -1

BULLISH_LEG = 1
BEARISH_LEG = 0


# ---------------------------------------------------------------------------
# Parameters (defaults mirror the Pine strategy inputs)
# ---------------------------------------------------------------------------

@dataclass
class SMCParams:
    swing_length: int = 50          # swingsLengthInput
    internal_length: int = 5        # fixed in the Pine script
    equal_length: int = 3           # equalHighsLowsLengthInput
    equal_threshold: float = 0.1    # equalHighsLowsThresholdInput
    atr_length: int = 200           # atrMeasure = ta.atr(200)
    entry_structure: str = "internal"   # internal | swing | both
    entry_signal: str = "all"           # all | bos | choch
    trend_filter: bool = False          # swing-trend alignment for internal entries
    zone_filter: bool = False           # long below / short above equilibrium
    close_on_opposite: bool = True
    stop_type: str = "swing"            # swing | atr | percent | none
    atr_multiplier: float = 2.0
    stop_percent: float = 2.0
    tp_type: str = "rr"                 # rr | equilibrium | extreme | none
    risk_reward: float = 2.0
    allow_long: bool = True
    allow_short: bool = True


# ---------------------------------------------------------------------------
# Internal state containers
# ---------------------------------------------------------------------------

@dataclass
class _Pivot:
    level: Optional[float] = None
    last_level: Optional[float] = None
    crossed: bool = False
    bar_index: int = 0
    bar_time: int = 0
    prev_level: Optional[float] = None   # series value at the end of the previous bar


@dataclass
class _OrderBlock:
    high: float
    low: float
    time: int
    bias: int


@dataclass
class _FVG:
    # Fields stored exactly like the Pine UDT: for a bearish gap `top` is the
    # gap's LOWER edge (currentHigh) and `bottom` its upper edge (last2Low).
    top: float
    bottom: float
    bias: int
    time: int


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class SMCResult:
    symbol: str = ""
    tf: str = ""
    ts: int = 0                      # open time (ms) of the last closed bar
    price: float = 0.0
    atr: Optional[float] = None
    # simulated position -----------------------------------------------------
    position: str = "FLAT"           # LONG | SHORT | FLAT
    entry_price: Optional[float] = None
    entry_time: Optional[int] = None
    bars_in_trade: Optional[int] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    sl_pct: Optional[float] = None   # distance from current price, signed %
    tp_pct: Optional[float] = None
    rr: Optional[float] = None       # planned reward:risk at entry
    pnl_pct: Optional[float] = None  # unrealized, direction adjusted
    r_multiple: Optional[float] = None
    # market structure --------------------------------------------------------
    swing_trend: int = 0             # +1 bullish / -1 bearish / 0 undefined
    internal_trend: int = 0
    last_signal: Optional[str] = None            # "BOS" | "CHoCH"
    last_signal_dir: int = 0                     # +1 / -1
    last_signal_structure: Optional[str] = None  # "internal" | "swing"
    last_signal_bars_ago: Optional[int] = None
    # zones & levels -----------------------------------------------------------
    trailing_top: Optional[float] = None
    trailing_bottom: Optional[float] = None
    equilibrium: Optional[float] = None
    zone: Optional[str] = None       # PREMIUM | EQUILIBRIUM | DISCOUNT
    range_pos: Optional[float] = None  # 0..100 position within the trailing range
    strong_high: bool = False        # top is a Strong High (swing trend bearish)
    strong_low: bool = False         # bottom is a Strong Low (swing trend bullish)
    # order blocks (nearest unmitigated) ---------------------------------------
    ob_support_top: Optional[float] = None
    ob_support_bottom: Optional[float] = None
    ob_resist_top: Optional[float] = None
    ob_resist_bottom: Optional[float] = None
    # fair value gaps (nearest unmitigated) ------------------------------------
    fvg_below_top: Optional[float] = None
    fvg_below_bottom: Optional[float] = None
    fvg_above_top: Optional[float] = None
    fvg_above_bottom: Optional[float] = None
    # equal highs / lows --------------------------------------------------------
    eqh_bars_ago: Optional[int] = None
    eql_bars_ago: Optional[int] = None
    # simulation stats over the analysed window ---------------------------------
    trades: int = 0
    wins: int = 0
    cum_pnl_pct: float = 0.0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class _Engine:
    def __init__(self, params: SMCParams):
        self.p = params
        # pivots
        self.swing_high = _Pivot()
        self.swing_low = _Pivot()
        self.internal_high = _Pivot()
        self.internal_low = _Pivot()
        self.equal_high = _Pivot()
        self.equal_low = _Pivot()
        # legs (Pine: `var leg = 0` per call site)
        self.legs = {"swing": 0, "internal": 0, "equal": 0}
        # trends
        self.swing_bias = 0
        self.internal_bias = 0
        # trailing extremes
        self.tr_top: Optional[float] = None
        self.tr_bottom: Optional[float] = None
        self.tr_bar_time = 0
        self.tr_bar_index = 0
        self.tr_last_top_time = 0
        self.tr_last_bottom_time = 0
        # series stores
        self.parsed_highs: List[float] = []
        self.parsed_lows: List[float] = []
        # ATR (RMA of true range)
        self.atr: Optional[float] = None
        self._tr_sum = 0.0
        self._tr_count = 0
        # order blocks & FVGs
        self.internal_obs: List[_OrderBlock] = []
        self.swing_obs: List[_OrderBlock] = []
        self.fvgs: List[_FVG] = []
        self._cum_delta_abs = 0.0
        # last signal info
        self.last_sig: Optional[Tuple[str, int, str, int]] = None  # (kind, dir, structure, bar)
        self.eqh_idx: Optional[int] = None
        self.eql_idx: Optional[int] = None
        # per-bar alert flags (reset each bar)
        self.sig = {}
        # position simulation
        self.pos = 0
        self.entry_price: Optional[float] = None
        self.entry_index: Optional[int] = None
        self.entry_time: Optional[int] = None
        self.sl: Optional[float] = None
        self.tp: Optional[float] = None
        self.trade_pnls: List[float] = []
        # previous-bar snapshots (for Pine crossover semantics)
        self.prev_close: Optional[float] = None

    # -- helpers ------------------------------------------------------------

    def _leg_update(self, name: str, size: int, highs, lows, i) -> int:
        """Return ta.change(leg) for this bar; 0 if unchanged."""
        prev = self.legs[name]
        leg = prev
        if i >= size:
            window_high = max(highs[i - size + 1:i + 1])
            window_low = min(lows[i - size + 1:i + 1])
            if highs[i - size] > window_high:
                leg = BEARISH_LEG
            elif lows[i - size] < window_low:
                leg = BULLISH_LEG
        self.legs[name] = leg
        return leg - prev

    def _update_atr(self, high, low, prev_close) -> None:
        tr = high - low if prev_close is None else max(
            high - low, abs(high - prev_close), abs(low - prev_close))
        n = self.p.atr_length
        if self.atr is None:
            self._tr_sum += tr
            self._tr_count += 1
            if self._tr_count >= n:
                self.atr = self._tr_sum / n
        else:
            self.atr = (self.atr * (n - 1) + tr) / n

    def _new_pivot(self, pivot: _Pivot, level: float, idx: int, ts: int) -> None:
        pivot.last_level = pivot.level
        pivot.level = level
        pivot.crossed = False
        pivot.bar_index = idx
        pivot.bar_time = ts

    def _store_order_block(self, pivot: _Pivot, internal: bool, bias: int,
                           times, i: int) -> None:
        lo, hi = pivot.bar_index, i          # Pine: slice(barIndex, bar_index)
        if hi <= lo:
            return
        if bias == BEARISH:
            seg = self.parsed_highs[lo:hi]
            rel = max(range(len(seg)), key=seg.__getitem__)
        else:
            seg = self.parsed_lows[lo:hi]
            rel = min(range(len(seg)), key=seg.__getitem__)
        idx = lo + rel
        ob = _OrderBlock(self.parsed_highs[idx], self.parsed_lows[idx], times[idx], bias)
        target = self.internal_obs if internal else self.swing_obs
        if len(target) >= 100:
            target.pop()
        target.insert(0, ob)

    def _structure_step(self, internal: bool, close: float, times, i: int) -> None:
        """Port of displayStructure(): BOS/CHoCH detection on pivot crosses."""
        struct = "internal" if internal else "swing"
        hi_p = self.internal_high if internal else self.swing_high
        lo_p = self.internal_low if internal else self.swing_low

        # bullish break (ta.crossover of close over the pivot-high level)
        extra = True
        if internal:  # Pine: internalHigh.currentLevel != swingHigh.currentLevel
            extra = (hi_p.level is not None and self.swing_high.level is not None
                     and hi_p.level != self.swing_high.level)
        crossed_up = (hi_p.level is not None and close > hi_p.level
                      and self.prev_close is not None and hi_p.prev_level is not None
                      and self.prev_close <= hi_p.prev_level)
        if crossed_up and not hi_p.crossed and extra:
            bias = self.internal_bias if internal else self.swing_bias
            tag = "CHoCH" if bias == BEARISH else "BOS"
            self.sig[(struct, BULLISH, tag)] = True
            self.last_sig = (tag, BULLISH, struct, i)
            hi_p.crossed = True
            if internal:
                self.internal_bias = BULLISH
            else:
                self.swing_bias = BULLISH
            self._store_order_block(hi_p, internal, BULLISH, times, i)

        # bearish break (ta.crossunder of close under the pivot-low level)
        extra = True
        if internal:
            extra = (lo_p.level is not None and self.swing_low.level is not None
                     and lo_p.level != self.swing_low.level)
        crossed_dn = (lo_p.level is not None and close < lo_p.level
                      and self.prev_close is not None and lo_p.prev_level is not None
                      and self.prev_close >= lo_p.prev_level)
        if crossed_dn and not lo_p.crossed and extra:
            bias = self.internal_bias if internal else self.swing_bias
            tag = "CHoCH" if bias == BULLISH else "BOS"
            self.sig[(struct, BEARISH, tag)] = True
            self.last_sig = (tag, BEARISH, struct, i)
            lo_p.crossed = True
            if internal:
                self.internal_bias = BEARISH
            else:
                self.swing_bias = BEARISH
            self._store_order_block(lo_p, internal, BEARISH, times, i)

    # -- strategy execution ---------------------------------------------------

    def _matches_entry_signal(self, struct: str, direction: int) -> bool:
        want = self.p.entry_signal
        bos = self.sig.get((struct, direction, "BOS"), False)
        choch = self.sig.get((struct, direction, "CHoCH"), False)
        if want == "bos":
            return bos
        if want == "choch":
            return choch
        return bos or choch

    def _get_stop(self, is_long: bool, close: float) -> Optional[float]:
        p = self.p
        if p.stop_type == "none":
            return None
        atr_risk = None if self.atr is None else p.atr_multiplier * self.atr
        if p.stop_type == "swing":
            stop = self.tr_bottom if is_long else self.tr_top
        elif p.stop_type == "atr":
            stop = None if atr_risk is None else (close - atr_risk if is_long else close + atr_risk)
        else:  # percent
            stop = close * (1 - p.stop_percent / 100) if is_long else close * (1 + p.stop_percent / 100)
        invalid = stop is None or (stop >= close if is_long else stop <= close)
        if invalid:
            stop = None if atr_risk is None else (close - atr_risk if is_long else close + atr_risk)
        return stop

    def _get_target(self, is_long: bool, close: float, stop: Optional[float]) -> Optional[float]:
        p = self.p
        if p.tp_type == "none":
            return None
        if stop is not None:
            risk = abs(close - stop)
        elif self.atr is not None:
            risk = p.atr_multiplier * self.atr
        else:
            return None
        rr_target = close + risk * p.risk_reward if is_long else close - risk * p.risk_reward
        if p.tp_type == "equilibrium":
            target = self._equilibrium()
        elif p.tp_type == "extreme":
            target = self.tr_top if is_long else self.tr_bottom
        else:
            target = rr_target
        if target is None or (target <= close if is_long else target >= close):
            target = rr_target
        return target

    def _equilibrium(self) -> Optional[float]:
        if self.tr_top is None or self.tr_bottom is None:
            return None
        return 0.5 * (self.tr_top + self.tr_bottom)

    def _close_position(self, exit_price: float) -> None:
        if self.pos != 0 and self.entry_price:
            pnl = (exit_price / self.entry_price - 1.0) * 100.0 * self.pos
            self.trade_pnls.append(pnl)
        self.pos = 0
        self.entry_price = None
        self.entry_index = None
        self.entry_time = None
        self.sl = None
        self.tp = None

    def _open_position(self, direction: int, close: float, i: int, ts: int) -> None:
        is_long = direction == BULLISH
        stop = self._get_stop(is_long, close)
        target = self._get_target(is_long, close, stop)
        self.pos = direction
        self.entry_price = close
        self.entry_index = i
        self.entry_time = ts
        self.sl = stop
        self.tp = target

    def _strategy_step(self, candle: Candle, i: int) -> None:
        p = self.p
        high, low, close = candle.high, candle.low, candle.close

        # 1) stop / limit orders are live from the bar after entry (intrabar)
        if self.pos != 0 and self.entry_index is not None and i > self.entry_index:
            if self.pos > 0:
                hit_sl = self.sl is not None and low <= self.sl
                hit_tp = self.tp is not None and high >= self.tp
            else:
                hit_sl = self.sl is not None and high >= self.sl
                hit_tp = self.tp is not None and low <= self.tp
            if hit_sl:  # conservative: stop first when both hit in one bar
                self._close_position(self.sl)
            elif hit_tp:
                self._close_position(self.tp)

        # 2) entry signals from this bar's structure breaks
        use_internal = p.entry_structure != "swing"
        use_swing = p.entry_structure != "internal"

        eq = self._equilibrium()
        trend_ok_long = not p.trend_filter or self.swing_bias == BULLISH
        trend_ok_short = not p.trend_filter or self.swing_bias == BEARISH
        zone_ok_long = not p.zone_filter or (eq is not None and close < eq)
        zone_ok_short = not p.zone_filter or (eq is not None and close > eq)

        long_signal = ((use_internal and self._matches_entry_signal("internal", BULLISH) and trend_ok_long)
                       or (use_swing and self._matches_entry_signal("swing", BULLISH)))
        short_signal = ((use_internal and self._matches_entry_signal("internal", BEARISH) and trend_ok_short)
                        or (use_swing and self._matches_entry_signal("swing", BEARISH)))

        long_condition = p.allow_long and long_signal and zone_ok_long
        short_condition = p.allow_short and short_signal and zone_ok_short

        if long_condition and self.pos <= 0 and (self.pos == 0 or p.close_on_opposite):
            if self.pos < 0:
                self._close_position(close)
            self._open_position(BULLISH, close, i, candle.open_time)
        elif short_condition and self.pos >= 0 and (self.pos == 0 or p.close_on_opposite):
            if self.pos > 0:
                self._close_position(close)
            self._open_position(BEARISH, close, i, candle.open_time)

        # 3) opposite break that did not trigger a reversal entry
        if p.close_on_opposite:
            if short_signal and self.pos > 0 and not short_condition:
                self._close_position(close)
            elif long_signal and self.pos < 0 and not long_condition:
                self._close_position(close)

    # -- main per-bar processing -----------------------------------------------

    def process(self, candles: List[Candle]) -> None:
        p = self.p
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        times = [c.open_time for c in candles]

        for i, c in enumerate(candles):
            self.sig = {}
            prev_close = self.prev_close

            # ATR & volatility-parsed highs/lows (order block filtering)
            self._update_atr(c.high, c.low, prev_close)
            hv = self.atr is not None and (c.high - c.low) >= 2 * self.atr
            self.parsed_highs.append(c.low if hv else c.high)
            self.parsed_lows.append(c.high if hv else c.low)

            # trailing extremes (updated every bar once initialised)
            if self.tr_top is not None:
                if c.high >= self.tr_top:
                    self.tr_top = c.high
                    self.tr_last_top_time = c.open_time
                if c.low <= self.tr_bottom:
                    self.tr_bottom = c.low
                    self.tr_last_bottom_time = c.open_time

            # FVG mitigation runs before detection (as in the Pine flow)
            kept = []
            for g in self.fvgs:
                mitigated = ((g.bias == BULLISH and c.low < g.bottom)
                             or (g.bias == BEARISH and c.high > g.top))
                if not mitigated:
                    kept.append(g)
            self.fvgs = kept

            # swing structure pivots (also drive the trailing extremes)
            ch = self._leg_update("swing", p.swing_length, highs, lows, i)
            if ch == +1:  # new pivot LOW
                idx = i - p.swing_length
                self._new_pivot(self.swing_low, lows[idx], idx, times[idx])
                self.tr_bottom = self.swing_low.level
                self.tr_bar_time = self.swing_low.bar_time
                self.tr_bar_index = self.swing_low.bar_index
                self.tr_last_bottom_time = self.swing_low.bar_time
                if self.tr_top is None:
                    self.tr_top = max(highs[idx:i + 1])
                    self.tr_last_top_time = times[idx]
            elif ch == -1:  # new pivot HIGH
                idx = i - p.swing_length
                self._new_pivot(self.swing_high, highs[idx], idx, times[idx])
                self.tr_top = self.swing_high.level
                self.tr_bar_time = self.swing_high.bar_time
                self.tr_bar_index = self.swing_high.bar_index
                self.tr_last_top_time = self.swing_high.bar_time
                if self.tr_bottom is None:
                    self.tr_bottom = min(lows[idx:i + 1])
                    self.tr_last_bottom_time = times[idx]

            # internal structure pivots
            ch = self._leg_update("internal", p.internal_length, highs, lows, i)
            if ch == +1:
                idx = i - p.internal_length
                self._new_pivot(self.internal_low, lows[idx], idx, times[idx])
            elif ch == -1:
                idx = i - p.internal_length
                self._new_pivot(self.internal_high, highs[idx], idx, times[idx])

            # equal highs / lows
            ch = self._leg_update("equal", p.equal_length, highs, lows, i)
            if ch == +1:
                idx = i - p.equal_length
                if (self.equal_low.level is not None and self.atr is not None
                        and abs(self.equal_low.level - lows[idx]) < p.equal_threshold * self.atr):
                    self.eql_idx = i
                self._new_pivot(self.equal_low, lows[idx], idx, times[idx])
            elif ch == -1:
                idx = i - p.equal_length
                if (self.equal_high.level is not None and self.atr is not None
                        and abs(self.equal_high.level - highs[idx]) < p.equal_threshold * self.atr):
                    self.eqh_idx = i
                self._new_pivot(self.equal_high, highs[idx], idx, times[idx])

            # BOS / CHoCH detection (internal first, then swing — Pine order)
            self._structure_step(True, c.close, times, i)
            self._structure_step(False, c.close, times, i)

            # order block mitigation (High/Low source, as the Pine default)
            for obs in (self.internal_obs, self.swing_obs):
                obs[:] = [ob for ob in obs
                          if not ((ob.bias == BEARISH and c.high > ob.high)
                                  or (ob.bias == BULLISH and c.low < ob.low))]

            # FVG detection (chart timeframe)
            if i >= 1 and candles[i - 1].open != 0:
                delta = (candles[i - 1].close - candles[i - 1].open) / (candles[i - 1].open * 100)
                self._cum_delta_abs += abs(delta)
                if i >= 2:
                    threshold = self._cum_delta_abs / i * 2
                    h2, l2 = highs[i - 2], lows[i - 2]
                    if c.low > h2 and candles[i - 1].close > h2 and delta > threshold:
                        self.fvgs.insert(0, _FVG(c.low, h2, BULLISH, c.open_time))
                    if c.high < l2 and candles[i - 1].close < l2 and -delta > threshold:
                        self.fvgs.insert(0, _FVG(c.high, l2, BEARISH, c.open_time))

            # strategy orders
            self._strategy_step(c, i)

            # end-of-bar snapshots (Pine series semantics for crossovers)
            self.prev_close = c.close
            for piv in (self.swing_high, self.swing_low,
                        self.internal_high, self.internal_low):
                piv.prev_level = piv.level


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze(candles: List[Candle], symbol: str = "", tf: str = "",
            params: Optional[SMCParams] = None) -> SMCResult:
    """Run the SMC engine over closed candles and return the last-bar state."""
    p = params or SMCParams()
    res = SMCResult(symbol=symbol, tf=tf)
    if not candles:
        res.error = "no data"
        return res

    eng = _Engine(p)
    eng.process(candles)

    last = candles[-1]
    n = len(candles)
    price = last.close
    res.ts = last.open_time
    res.price = price
    res.atr = eng.atr

    # position ---------------------------------------------------------------
    if eng.pos != 0:
        res.position = "LONG" if eng.pos > 0 else "SHORT"
        res.entry_price = eng.entry_price
        res.entry_time = eng.entry_time
        res.bars_in_trade = (n - 1) - (eng.entry_index or 0)
        res.stop_loss = eng.sl
        res.take_profit = eng.tp
        if eng.sl is not None and price:
            res.sl_pct = (eng.sl / price - 1.0) * 100.0
        if eng.tp is not None and price:
            res.tp_pct = (eng.tp / price - 1.0) * 100.0
        if eng.sl is not None and eng.tp is not None and eng.entry_price:
            risk = abs(eng.entry_price - eng.sl)
            if risk > 0:
                res.rr = abs(eng.tp - eng.entry_price) / risk
        if eng.entry_price:
            res.pnl_pct = (price / eng.entry_price - 1.0) * 100.0 * eng.pos
            if eng.sl is not None:
                risk = abs(eng.entry_price - eng.sl)
                if risk > 0:
                    res.r_multiple = (price - eng.entry_price) * eng.pos / risk

    # structure ----------------------------------------------------------------
    res.swing_trend = eng.swing_bias
    res.internal_trend = eng.internal_bias
    if eng.last_sig:
        kind, direction, struct, idx = eng.last_sig
        res.last_signal = kind
        res.last_signal_dir = direction
        res.last_signal_structure = struct
        res.last_signal_bars_ago = (n - 1) - idx

    # zones & levels -------------------------------------------------------------
    res.trailing_top = eng.tr_top
    res.trailing_bottom = eng.tr_bottom
    res.equilibrium = eng._equilibrium()
    if eng.tr_top is not None and eng.tr_bottom is not None and eng.tr_top > eng.tr_bottom:
        pos_pct = (price - eng.tr_bottom) / (eng.tr_top - eng.tr_bottom) * 100.0
        res.range_pos = max(0.0, min(100.0, pos_pct))
        res.zone = ("PREMIUM" if pos_pct > 52.5
                    else "DISCOUNT" if pos_pct < 47.5
                    else "EQUILIBRIUM")
    res.strong_high = eng.swing_bias == BEARISH
    res.strong_low = eng.swing_bias == BULLISH

    # nearest unmitigated order blocks (internal + swing merged) -----------------
    all_obs = eng.internal_obs + eng.swing_obs
    supports = [ob for ob in all_obs if ob.bias == BULLISH and ob.low < price]
    resists = [ob for ob in all_obs if ob.bias == BEARISH and ob.high > price]
    if supports:
        ob = max(supports, key=lambda o: o.high)
        res.ob_support_top, res.ob_support_bottom = ob.high, ob.low
    if resists:
        ob = min(resists, key=lambda o: o.low)
        res.ob_resist_top, res.ob_resist_bottom = ob.high, ob.low
    # nearest unmitigated fair value gaps -----------------------------------------
    bull_gaps = [g for g in eng.fvgs if g.bias == BULLISH and max(g.top, g.bottom) <= price]
    bear_gaps = [g for g in eng.fvgs if g.bias == BEARISH and min(g.top, g.bottom) >= price]
    if bull_gaps:
        g = max(bull_gaps, key=lambda g: max(g.top, g.bottom))
        res.fvg_below_top, res.fvg_below_bottom = max(g.top, g.bottom), min(g.top, g.bottom)
    if bear_gaps:
        g = min(bear_gaps, key=lambda g: min(g.top, g.bottom))
        res.fvg_above_top, res.fvg_above_bottom = max(g.top, g.bottom), min(g.top, g.bottom)

    # EQH / EQL --------------------------------------------------------------------
    if eng.eqh_idx is not None:
        res.eqh_bars_ago = (n - 1) - eng.eqh_idx
    if eng.eql_idx is not None:
        res.eql_bars_ago = (n - 1) - eng.eql_idx

    # simulation stats ----------------------------------------------------------------
    res.trades = len(eng.trade_pnls)
    res.wins = sum(1 for x in eng.trade_pnls if x > 0)
    res.cum_pnl_pct = round(sum(eng.trade_pnls), 2)
    return res
