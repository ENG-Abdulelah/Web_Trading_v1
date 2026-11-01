# execution/entry_exit.py
"""
execution/entry_exit.py

Converts a raw StrategySignal into a refined, executable TradePlan:

  * structural stop-loss (recent swing ± buffer), floored by ATR so it is
    never unrealistically tight,
  * take-profit(s) anchored on R multiples with optional partial exits,
  * breakeven + trailing-stop rules,
  * pip-rounding to the symbol's tick,
  * validation of R:R and SL/TP side-consistency.

Also provides `simulate_trade_outcome()` used by execution.timemachine for
bar-by-bar outcome replay (SL/TP/trailing/breakeven/timeout).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

try:
    from analysis.ict_engine import (  # type: ignore
        Candle, Direction, StrategySignal, atr, swing_points,
    )
except Exception:  # pragma: no cover
    from dataclasses import dataclass as _dc
    from datetime import datetime as _dt
    from enum import Enum

    class Direction(str, Enum):
        LONG = "long"; SHORT = "short"; NEUTRAL = "neutral"

    @_dc
    class Candle:
        symbol: str; open_time: _dt
        open: float; high: float; low: float; close: float; volume: float = 0.0

    class StrategySignal:  # minimal shim
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def atr(candles, period=14):
        if len(candles) < 2: return 0.0
        trs = [max(c.high - c.low, abs(c.high - candles[i-1].close),
                   abs(c.low - candles[i-1].close))
               for i, c in enumerate(candles) if i > 0]
        n = min(period, len(trs))
        return sum(trs[-n:]) / n

    def swing_points(candles, lookback=2):
        highs, lows = [], []
        for i in range(lookback, len(candles) - lookback):
            w = candles[i - lookback : i + lookback + 1]
            c = candles[i]
            if c.high == max(x.high for x in w): highs.append(i)
            if c.low == min(x.low for x in w): lows.append(i)
        return highs, lows

from execution.risk_manager import SymbolSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Plan dataclass
# ---------------------------------------------------------------------
@dataclass
class TradePlan:
    symbol: str
    direction: Direction
    entry: float
    stop_loss: float
    take_profit: float
    tp1: Optional[float] = None          # optional partial
    tp2: Optional[float] = None
    breakeven_trigger_r: Optional[float] = 1.0
    trail_start_r: Optional[float] = None
    trail_distance_atr: Optional[float] = None
    risk_pips: float = 0.0
    reward_pips: float = 0.0
    rr: float = 0.0
    strategy_id: str = ""
    reason: str = ""
    valid: bool = True
    validation_reason: str = ""

    def breakeven_price(self) -> Optional[float]:
        if self.breakeven_trigger_r is None:
            return None
        sign = 1 if self.direction == Direction.LONG else -1
        r = abs(self.entry - self.stop_loss)
        return self.entry + sign * self.breakeven_trigger_r * r


# ---------------------------------------------------------------------
# ExitPlanner
# ---------------------------------------------------------------------
class ExitPlanner:
    """
    Refines a StrategySignal into a TradePlan.

    Parameters:
        min_rr              – reject plans with R:R below this (default 1.5)
        sl_atr_floor_mult   – minimum SL distance expressed in ATR (default 0.6)
        structure_lookback  – candles searched for the protective swing (default 5)
        structure_buffer_atr– buffer beyond the swing for SL (default 0.15)
        partial_exits       – if True, splits at TP1 = 1R and TP2 = plan.take_profit
        trailing            – if True, enables trailing stop after TP1
    """

    def __init__(
        self,
        min_rr: float = 1.5,
        sl_atr_floor_mult: float = 0.6,
        structure_lookback: int = 5,
        structure_buffer_atr: float = 0.15,
        partial_exits: bool = False,
        trailing: bool = False,
    ):
        self.min_rr = min_rr
        self.sl_atr_floor_mult = sl_atr_floor_mult
        self.structure_lookback = structure_lookback
        self.structure_buffer_atr = structure_buffer_atr
        self.partial_exits = partial_exits
        self.trailing = trailing

    # -- rounding ---------------------------------------------------------
    @staticmethod
    def _round_price(price: float, pip: float) -> float:
        if pip <= 0:
            return price
        return round(round(price / pip) * pip, 10)

    # -- planning ---------------------------------------------------------
    def plan(
        self,
        signal: StrategySignal,
        candles: Sequence[Candle],
        spec: SymbolSpec,
    ) -> TradePlan:
        direction = signal.direction
        if direction == Direction.NEUTRAL:
            return TradePlan(
                symbol=signal.symbol, direction=direction,
                entry=signal.entry, stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                valid=False, validation_reason="neutral direction",
                strategy_id=signal.strategy_id, reason=signal.reason,
            )

        a = atr(list(candles)) if candles else 0.0
        entry = self._round_price(signal.entry, spec.pip_size)
        stop = signal.stop_loss
        tp = signal.take_profit

        # --- structural SL check -------------------------------------
        if candles and len(candles) > self.structure_lookback:
            recent = list(candles)[-self.structure_lookback * 2:]
            highs, lows = swing_points(recent, lookback=1)
            buf = self.structure_buffer_atr * a
            if direction == Direction.LONG and lows:
                structural = min(recent[i].low for i in lows) - buf
                # Only tighten, never widen — keep the raw SL if it is safer.
                stop = max(stop, structural) if stop > structural else stop
            elif direction == Direction.SHORT and highs:
                structural = max(recent[i].high for i in highs) + buf
                stop = min(stop, structural) if stop < structural else stop

        # --- ATR floor -----------------------------------------------
        if a > 0:
            sign = -1 if direction == Direction.LONG else 1
            floor_stop = entry + sign * self.sl_atr_floor_mult * a
            if direction == Direction.LONG:
                stop = min(stop, floor_stop)
            else:
                stop = max(stop, floor_stop)

        stop = self._round_price(stop, spec.pip_size)
        tp = self._round_price(tp, spec.pip_size)

        # --- side consistency ----------------------------------------
        valid, why = True, ""
        if direction == Direction.LONG:
            if not (stop < entry < tp):
                valid, why = False, "invalid long geometry"
        else:
            if not (tp < entry < stop):
                valid, why = False, "invalid short geometry"

        risk = abs(entry - stop)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0.0
        if valid and rr < self.min_rr:
            valid, why = False, f"R:R {rr:.2f} below min {self.min_rr:.2f}"

        risk_pips = risk / spec.pip_size if spec.pip_size else 0.0
        reward_pips = reward / spec.pip_size if spec.pip_size else 0.0

        tp1: Optional[float] = None
        tp2: Optional[float] = None
        trail_start_r: Optional[float] = None
        trail_distance: Optional[float] = None
        if self.partial_exits and valid:
            sign = 1 if direction == Direction.LONG else -1
            tp1 = self._round_price(entry + sign * 1.0 * risk, spec.pip_size)
            tp2 = tp
        if self.trailing and valid:
            trail_start_r = 1.0
            trail_distance = max(0.35, self.sl_atr_floor_mult) * a

        return TradePlan(
            symbol=signal.symbol,
            direction=direction,
            entry=entry,
            stop_loss=stop,
            take_profit=tp,
            tp1=tp1, tp2=tp2,
            breakeven_trigger_r=1.0,
            trail_start_r=trail_start_r,
            trail_distance_atr=trail_distance,
            risk_pips=round(risk_pips, 3),
            reward_pips=round(reward_pips, 3),
            rr=round(rr, 3),
            strategy_id=signal.strategy_id,
            reason=signal.reason,
            valid=valid,
            validation_reason=why or "ok",
        )


# ---------------------------------------------------------------------
# Bar-by-bar trade outcome simulation
# ---------------------------------------------------------------------
@dataclass
class SimOutcome:
    exit_price: float
    exit_index: int
    exit_reason: str          # "stop" | "take_profit" | "tp1_partial+timeout" | "timeout" | "be_stop"
    r_multiple: float
    partial_fraction: float = 0.0  # fraction closed at TP1 (0..1)


def simulate_trade_outcome(
    candles: Sequence[Candle],
    plan: TradePlan,
    start_index: int,
    timeout_bars: int = 200,
    spread_pips: float = 0.0,
    pip_size: float = 0.0001,
    apply_breakeven: bool = True,
) -> SimOutcome:
    """
    Bar-by-bar walk of a single trade. Conservative on ambiguous bars:
    if both SL and TP are inside a single candle, the SL is assumed to
    fill first.

    Returns the R-multiple outcome (including partials if plan.tp1 is set).
    """
    if plan.direction == Direction.NEUTRAL or not plan.valid:
        return SimOutcome(plan.entry, start_index, "invalid", 0.0)

    is_long = plan.direction == Direction.LONG
    entry = plan.entry
    sl = plan.stop_loss
    tp = plan.take_profit
    risk = abs(entry - sl)
    if risk <= 0:
        return SimOutcome(entry, start_index, "invalid_risk", 0.0)

    spread_cost = spread_pips * pip_size if pip_size else 0.0
    partial_frac = 0.5 if plan.tp1 is not None else 0.0
    realized_r = 0.0
    tp1_hit = False
    current_sl = sl

    be = plan.breakeven_price() if apply_breakeven else None
    trail_on = plan.trail_start_r is not None and plan.trail_distance_atr is not None

    last_idx = min(len(candles) - 1, start_index + timeout_bars)
    for i in range(start_index, last_idx + 1):
        c = candles[i]

        # --- check SL first (conservative) ---
        if is_long and c.low <= current_sl:
            exit_price = current_sl - spread_cost
            r = (exit_price - entry) / risk
            final_r = realized_r + (1.0 - partial_frac) * r
            reason = "be_stop" if current_sl >= entry else "stop"
            return SimOutcome(exit_price, i, reason, round(final_r, 4), partial_frac)
        if not is_long and c.high >= current_sl:
            exit_price = current_sl + spread_cost
            r = (entry - exit_price) / risk
            final_r = realized_r + (1.0 - partial_frac) * r
            reason = "be_stop" if current_sl <= entry else "stop"
            return SimOutcome(exit_price, i, reason, round(final_r, 4), partial_frac)

        # --- TP1 partial ---
        if plan.tp1 is not None and not tp1_hit:
            hit1 = (is_long and c.high >= plan.tp1) or (not is_long and c.low <= plan.tp1)
            if hit1:
                r1 = (plan.tp1 - entry) / risk if is_long else (entry - plan.tp1) / risk
                realized_r += partial_frac * r1
                tp1_hit = True
                # move SL to breakeven after TP1
                current_sl = entry

        # --- final TP ---
        if is_long and c.high >= tp:
            exit_price = tp - spread_cost
            r = (exit_price - entry) / risk
            final_r = realized_r + (1.0 - partial_frac) * r
            reason = "tp1_partial+tp2" if tp1_hit else "take_profit"
            return SimOutcome(exit_price, i, reason, round(final_r, 4), partial_frac)
        if not is_long and c.low <= tp:
            exit_price = tp + spread_cost
            r = (entry - exit_price) / risk
            final_r = realized_r + (1.0 - partial_frac) * r
            reason = "tp1_partial+tp2" if tp1_hit else "take_profit"
            return SimOutcome(exit_price, i, reason, round(final_r, 4), partial_frac)

        # --- breakeven / trailing ---
        if be is not None:
            if is_long and c.high >= be:
                current_sl = max(current_sl, entry)
            elif not is_long and c.low <= be:
                current_sl = min(current_sl, entry)

        if trail_on and tp1_hit:
            dist = plan.trail_distance_atr or 0.0
            if is_long:
                new_stop = c.close - dist
                current_sl = max(current_sl, new_stop)
            else:
                new_stop = c.close + dist
                current_sl = min(current_sl, new_stop)

    # --- timeout ---
    last_close = candles[last_idx].close
    r_last = (last_close - entry) / risk if is_long else (entry - last_close) / risk
    final_r = realized_r + (1.0 - partial_frac) * r_last
    return SimOutcome(
        last_close, last_idx, "timeout", round(final_r, 4), partial_frac,
    )
