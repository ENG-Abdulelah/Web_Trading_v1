# analysis/ict_engine.py
"""
analysis/ict_engine.py

Modular ICT (Inner Circle Trader) / SMC strategy implementations and an
adaptive engine that:
  * evaluates every strategy against the current market,
  * picks the most suitable one for the prevailing regime,
  * tracks per-strategy performance, and
  * iteratively re-tunes underperforming strategies.

Strategies included:
  * OrderBlockStrategy    – last opposing candle before a strong impulse.
  * FVGStrategy           – unmitigated Fair Value Gap retest.
  * LiquiditySweepStrategy – stop-hunt wick beyond a prior swing point.

The Candle dataclass is structurally compatible with core.data_feed.Candle
(same attribute names), so instances can be passed in directly.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------
class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class Candle:
    symbol: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return max(self.high - self.low, 1e-12)

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open


class Regime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    VOLATILE = "volatile"
    UNKNOWN = "unknown"


@dataclass
class MarketContext:
    symbol: str
    regime: Regime
    atr: float
    last_price: float
    recent_high: float
    recent_low: float


@dataclass
class StrategySignal:
    strategy_id: str
    symbol: str
    direction: Direction
    confidence: float              # 0..1
    entry: float
    stop_loss: float
    take_profit: float
    reason: str
    metadata: dict = field(default_factory=dict)

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def reward(self) -> float:
        return abs(self.take_profit - self.entry)

    @property
    def rr(self) -> float:
        return self.reward / self.risk if self.risk > 0 else 0.0


# ---------------------------------------------------------------------
# Technical utilities
# ---------------------------------------------------------------------
def _ema(values: Sequence[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _true_range(c: Candle, prev_close: float) -> float:
    return max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))


def atr(candles: Sequence[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    period = min(period, len(candles) - 1)
    trs = [_true_range(candles[i], candles[i - 1].close) for i in range(1, len(candles))]
    return sum(trs[-period:]) / period


def swing_points(candles: Sequence[Candle], lookback: int = 2) -> Tuple[List[int], List[int]]:
    """Return indices of fractal swing highs and lows."""
    highs, lows = [], []
    for i in range(lookback, len(candles) - lookback):
        window = candles[i - lookback : i + lookback + 1]
        c = candles[i]
        if c.high == max(w.high for w in window):
            highs.append(i)
        if c.low == min(w.low for w in window):
            lows.append(i)
    return highs, lows


def detect_regime(candles: Sequence[Candle], ema_period: int = 20, atr_period: int = 14) -> Regime:
    if len(candles) < ema_period + 2:
        return Regime.UNKNOWN
    closes = [c.close for c in candles]
    ema = _ema(closes, ema_period)
    a_short = atr(candles, atr_period)
    a_long = atr(candles, min(len(candles) - 1, atr_period * 3))
    if a_long > 0 and (a_short / a_long) > 1.6:
        return Regime.VOLATILE
    slope = ema[-1] - ema[-5]
    if slope > 0 and closes[-1] > ema[-1]:
        return Regime.TRENDING_UP
    if slope < 0 and closes[-1] < ema[-1]:
        return Regime.TRENDING_DOWN
    return Regime.RANGING


# ---------------------------------------------------------------------
# Strategy base class
# ---------------------------------------------------------------------
class ICTStrategy(ABC):
    strategy_id: str = "base"
    preferred_regimes: Tuple[Regime, ...] = (
        Regime.TRENDING_UP, Regime.TRENDING_DOWN, Regime.RANGING,
    )

    def __init__(self, **params):
        self.params: dict = dict(params)

    @abstractmethod
    def evaluate(
        self, candles: Sequence[Candle], context: MarketContext,
    ) -> Optional[StrategySignal]:
        ...

    def tune(self, perf: "StrategyPerformance") -> None:
        """Adjust self.params based on poor performance. No-op by default."""
        logger.info("Strategy %s has no tuning logic", self.strategy_id)

    def regime_suitable(self, regime: Regime) -> bool:
        return regime in self.preferred_regimes


# ---------------------------------------------------------------------
# Concrete strategies
# ---------------------------------------------------------------------
class OrderBlockStrategy(ICTStrategy):
    """
    Bullish OB: the last bearish candle immediately preceding a strong
    bullish impulse. When price retests the OB zone we look for a long.
    Mirrored for bearish OBs.
    """

    strategy_id = "order_block"
    preferred_regimes = (
        Regime.TRENDING_UP, Regime.TRENDING_DOWN, Regime.RANGING,
    )

    def __init__(
        self,
        impulse_atr_mult: float = 1.5,
        lookback: int = 30,
        retest_tolerance_atr: float = 0.5,
    ):
        super().__init__(
            impulse_atr_mult=impulse_atr_mult,
            lookback=lookback,
            retest_tolerance_atr=retest_tolerance_atr,
        )

    def evaluate(self, candles: Sequence[Candle], context: MarketContext) -> Optional[StrategySignal]:
        if len(candles) < 5 or context.atr <= 0:
            return None

        lookback = min(self.params["lookback"], len(candles) - 2)
        recent = list(candles)[-lookback:]
        a = context.atr
        price = context.last_price
        impulse_mult = self.params["impulse_atr_mult"]
        tol = self.params["retest_tolerance_atr"] * a

        for i in range(len(recent) - 1, 0, -1):
            imp, ob = recent[i], recent[i - 1]
            if imp.body < impulse_mult * a:
                continue

            # Bullish OB
            if imp.bullish and ob.bearish:
                zl, zh = ob.low, ob.high
                if zl - tol <= price <= zh + tol:
                    entry = max(price, zh * 0.999)
                    stop = zl - 0.25 * a
                    tp = entry + 2.0 * (entry - stop)
                    strength = imp.body / (impulse_mult * a)
                    conf = min(0.92, 0.55 + 0.15 * (strength - 1.0))
                    return StrategySignal(
                        strategy_id=self.strategy_id, symbol=context.symbol,
                        direction=Direction.LONG, confidence=conf,
                        entry=entry, stop_loss=stop, take_profit=tp,
                        reason=f"Bullish OB {zl:.5f}-{zh:.5f} retest",
                        metadata={"ob_low": zl, "ob_high": zh, "impulse": strength},
                    )

            # Bearish OB
            if imp.bearish and ob.bullish:
                zl, zh = ob.low, ob.high
                if zl - tol <= price <= zh + tol:
                    entry = min(price, zl * 1.001)
                    stop = zh + 0.25 * a
                    tp = entry - 2.0 * (stop - entry)
                    strength = imp.body / (impulse_mult * a)
                    conf = min(0.92, 0.55 + 0.15 * (strength - 1.0))
                    return StrategySignal(
                        strategy_id=self.strategy_id, symbol=context.symbol,
                        direction=Direction.SHORT, confidence=conf,
                        entry=entry, stop_loss=stop, take_profit=tp,
                        reason=f"Bearish OB {zl:.5f}-{zh:.5f} retest",
                        metadata={"ob_low": zl, "ob_high": zh, "impulse": strength},
                    )
        return None

    def tune(self, perf: "StrategyPerformance") -> None:
        # Demand stronger impulse, tighten recency when losing.
        self.params["impulse_atr_mult"] = min(3.0, self.params["impulse_atr_mult"] * 1.15)
        self.params["lookback"] = max(10, int(self.params["lookback"] * 0.85))
        logger.info("Tuned %s -> %s", self.strategy_id, self.params)


class FVGStrategy(ICTStrategy):
    """
    Fair Value Gap: 3-candle imbalance where candle 1's high is below
    candle 3's low (bullish) or candle 1's low is above candle 3's high
    (bearish). Price returning into an unmitigated gap is the trade.
    """

    strategy_id = "fvg"
    preferred_regimes = (Regime.TRENDING_UP, Regime.TRENDING_DOWN)

    def __init__(self, min_gap_atr_mult: float = 0.3, lookback: int = 40):
        super().__init__(min_gap_atr_mult=min_gap_atr_mult, lookback=lookback)

    def evaluate(self, candles: Sequence[Candle], context: MarketContext) -> Optional[StrategySignal]:
        if len(candles) < 4 or context.atr <= 0:
            return None

        a = context.atr
        min_gap = self.params["min_gap_atr_mult"] * a
        lookback = min(self.params["lookback"], len(candles) - 3)
        recent = list(candles)[-lookback:]
        price = context.last_price

        best: Optional[StrategySignal] = None

        for i in range(2, len(recent)):
            c1, c2, c3 = recent[i - 2], recent[i - 1], recent[i]

            # Bullish FVG
            if c1.high < c3.low:
                gap_low, gap_high = c1.high, c3.low
                if gap_high - gap_low >= min_gap and gap_low - 0.2 * a <= price <= gap_high + 0.2 * a:
                    entry = gap_low + 0.1 * (gap_high - gap_low)
                    stop = gap_low - 0.25 * a
                    tp = entry + 2.0 * (entry - stop)
                    size = (gap_high - gap_low) / (min_gap + 1e-12)
                    conf = min(0.9, 0.55 + 0.1 * min(size, 3.0))
                    best = StrategySignal(
                        strategy_id=self.strategy_id, symbol=context.symbol,
                        direction=Direction.LONG, confidence=conf,
                        entry=entry, stop_loss=stop, take_profit=tp,
                        reason=f"Bullish FVG {gap_low:.5f}-{gap_high:.5f} retest",
                        metadata={"fvg_low": gap_low, "fvg_high": gap_high},
                    )

            # Bearish FVG
            if c1.low > c3.high:
                gap_low, gap_high = c3.high, c1.low
                if gap_high - gap_low >= min_gap and gap_low - 0.2 * a <= price <= gap_high + 0.2 * a:
                    entry = gap_high - 0.1 * (gap_high - gap_low)
                    stop = gap_high + 0.25 * a
                    tp = entry - 2.0 * (stop - entry)
                    size = (gap_high - gap_low) / (min_gap + 1e-12)
                    conf = min(0.9, 0.55 + 0.1 * min(size, 3.0))
                    best = StrategySignal(
                        strategy_id=self.strategy_id, symbol=context.symbol,
                        direction=Direction.SHORT, confidence=conf,
                        entry=entry, stop_loss=stop, take_profit=tp,
                        reason=f"Bearish FVG {gap_low:.5f}-{gap_high:.5f} retest",
                        metadata={"fvg_low": gap_low, "fvg_high": gap_high},
                    )
        return best

    def tune(self, perf: "StrategyPerformance") -> None:
        self.params["min_gap_atr_mult"] = min(1.2, self.params["min_gap_atr_mult"] * 1.2)
        logger.info("Tuned %s -> %s", self.strategy_id, self.params)


class LiquiditySweepStrategy(ICTStrategy):
    """
    Detects a wick beyond a prior swing high/low followed by a close back
    inside the range — classic stop-hunt / inducement, often preceding a
    reversal.
    """

    strategy_id = "liquidity_sweep"
    preferred_regimes = (
        Regime.RANGING, Regime.TRENDING_UP, Regime.TRENDING_DOWN, Regime.VOLATILE,
    )

    def __init__(self, swing_lookback: int = 2, min_sweep_atr: float = 0.15):
        super().__init__(swing_lookback=swing_lookback, min_sweep_atr=min_sweep_atr)

    def evaluate(self, candles: Sequence[Candle], context: MarketContext) -> Optional[StrategySignal]:
        if len(candles) < 10 or context.atr <= 0:
            return None

        a = context.atr
        min_sweep = self.params["min_sweep_atr"] * a
        highs, lows = swing_points(candles, self.params["swing_lookback"])
        last = candles[-1]

        if len(highs) >= 2:
            level = max(candles[i].high for i in highs[:-1])
            if last.high > level + min_sweep and last.close < level:
                entry = last.close
                stop = last.high + 0.2 * a
                tp = entry - 2.0 * (stop - entry)
                return StrategySignal(
                    strategy_id=self.strategy_id, symbol=context.symbol,
                    direction=Direction.SHORT, confidence=0.72,
                    entry=entry, stop_loss=stop, take_profit=tp,
                    reason=f"Sweep above {level:.5f}, closed back inside",
                    metadata={"swept_level": level},
                )

        if len(lows) >= 2:
            level = min(candles[i].low for i in lows[:-1])
            if last.low < level - min_sweep and last.close > level:
                entry = last.close
                stop = last.low - 0.2 * a
                tp = entry + 2.0 * (entry - stop)
                return StrategySignal(
                    strategy_id=self.strategy_id, symbol=context.symbol,
                    direction=Direction.LONG, confidence=0.72,
                    entry=entry, stop_loss=stop, take_profit=tp,
                    reason=f"Sweep below {level:.5f}, closed back inside",
                    metadata={"swept_level": level},
                )
        return None

    def tune(self, perf: "StrategyPerformance") -> None:
        self.params["min_sweep_atr"] = min(0.75, self.params["min_sweep_atr"] * 1.2)
        self.params["swing_lookback"] = min(5, self.params["swing_lookback"] + 1)
        logger.info("Tuned %s -> %s", self.strategy_id, self.params)


# ---------------------------------------------------------------------
# Performance tracking & adaptive engine
# ---------------------------------------------------------------------
@dataclass
class StrategyPerformance:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_r: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def expectancy(self) -> float:
        return self.total_r / self.trades if self.trades else 0.0


class AdaptiveICTEngine:
    """
    Runs all registered strategies, ranks them, and periodically tunes
    ones that underperform. Call `record_outcome()` for each closed trade
    and `optimize()` (or rely on auto-optimization) to iterate.
    """

    def __init__(
        self,
        strategies: Optional[Sequence[ICTStrategy]] = None,
        min_samples: int = 10,
        min_win_rate: float = 0.45,
        auto_optimize: bool = True,
        optimize_every: int = 5,
    ):
        self.strategies: List[ICTStrategy] = list(strategies or [
            OrderBlockStrategy(),
            FVGStrategy(),
            LiquiditySweepStrategy(),
        ])
        self.performance: Dict[str, StrategyPerformance] = {
            s.strategy_id: StrategyPerformance() for s in self.strategies
        }
        self.min_samples = min_samples
        self.min_win_rate = min_win_rate
        self.auto_optimize = auto_optimize
        self.optimize_every = optimize_every

    # -- evaluation -------------------------------------------------------
    def build_context(self, candles: Sequence[Candle], symbol: str) -> MarketContext:
        recent = candles[-30:] if len(candles) >= 30 else list(candles)
        return MarketContext(
            symbol=symbol,
            regime=detect_regime(candles),
            atr=atr(candles),
            last_price=candles[-1].close,
            recent_high=max(c.high for c in recent),
            recent_low=min(c.low for c in recent),
        )

    def evaluate(self, candles: Sequence[Candle], symbol: str) -> List[StrategySignal]:
        if len(candles) < 5:
            return []
        ctx = self.build_context(candles, symbol)
        out: List[StrategySignal] = []

        for strat in self.strategies:
            if not strat.regime_suitable(ctx.regime):
                continue
            try:
                sig = strat.evaluate(candles, ctx)
            except Exception:
                logger.exception("Strategy %s raised during evaluate", strat.strategy_id)
                continue
            if sig is None:
                continue

            perf = self.performance[strat.strategy_id]
            # Historical edge (0.5..1.0). Neutral until 5 samples exist.
            edge = 0.5 + 0.5 * perf.win_rate if perf.trades >= 5 else 0.75
            sig.metadata["adjusted_score"] = sig.confidence * edge
            sig.metadata["regime"] = ctx.regime.value
            out.append(sig)

        out.sort(key=lambda s: s.metadata["adjusted_score"], reverse=True)
        return out

    def select_best(self, candles: Sequence[Candle], symbol: str) -> Optional[StrategySignal]:
        sigs = self.evaluate(candles, symbol)
        return sigs[0] if sigs else None

    # -- feedback loop ----------------------------------------------------
    def record_outcome(self, strategy_id: str, r_multiple: float) -> None:
        perf = self.performance.get(strategy_id)
        if perf is None:
            logger.warning("Unknown strategy %s", strategy_id)
            return
        perf.trades += 1
        perf.total_r += r_multiple
        if r_multiple > 0:
            perf.wins += 1
        else:
            perf.losses += 1

        if (
            self.auto_optimize
            and perf.trades >= self.min_samples
            and perf.trades % self.optimize_every == 0
        ):
            self._tune_one(strategy_id)

    def optimize(self) -> Dict[str, dict]:
        """Tune every underperforming strategy with enough samples."""
        report: Dict[str, dict] = {}
        for strat in self.strategies:
            perf = self.performance[strat.strategy_id]
            if perf.trades < self.min_samples:
                continue
            if perf.win_rate >= self.min_win_rate:
                continue
            report[strat.strategy_id] = self._tune_one(strat.strategy_id)
        return report

    def _tune_one(self, strategy_id: str) -> dict:
        strat = next(s for s in self.strategies if s.strategy_id == strategy_id)
        perf = self.performance[strategy_id]
        before = dict(strat.params)
        try:
            strat.tune(perf)
        except Exception:
            logger.exception("Tune failed for %s", strategy_id)
        after = dict(strat.params)
        logger.info(
            "Optimized %s (win_rate=%.2f): %s -> %s",
            strategy_id, perf.win_rate, before, after,
        )
        return {
            "before": before,
            "after": after,
            "win_rate": perf.win_rate,
            "expectancy": perf.expectancy,
        }

    def performance_snapshot(self) -> Dict[str, dict]:
        return {
            sid: {
                "trades": p.trades,
                "win_rate": round(p.win_rate, 3),
                "expectancy": round(p.expectancy, 3),
                "params": dict(next(s for s in self.strategies if s.strategy_id == sid).params),
            }
            for sid, p in self.performance.items()
        }
