# execution/timemachine.py
"""
execution/timemachine.py

Monte Carlo simulation of trading sessions across 10,000 historical runs.

Workflow:
  1) replay_history(): walk each symbol's candles once with the adaptive
     ICT engine + ExitPlanner + RiskManager to collect one R-multiple per
     potential trade (bar-by-bar simulated via simulate_trade_outcome).
  2) run(): bootstrap 10,000 randomized sessions from the collected
     R-multiples (with optional slippage / jitter) and compute aggregate
     statistics: win rate, expectancy, profit factor, max drawdown in R,
     and a risk-of-ruin estimate.

Falls back to synthetic R-multiples if no candles are supplied, so the
module is testable in isolation.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from analysis.ict_engine import (  # type: ignore
        AdaptiveICTEngine, Candle, Direction, StrategySignal,
    )
except Exception:  # pragma: no cover
    class Candle: ...
    class Direction:
        LONG = "long"; SHORT = "short"; NEUTRAL = "neutral"
    class AdaptiveICTEngine: ...

from execution.entry_exit import ExitPlanner, TradePlan, simulate_trade_outcome
from execution.risk_manager import RiskManager, SymbolSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------
@dataclass
class SimulationResult:
    runs: int
    trades_per_run: int
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_r: float
    expectancy_r: float
    profit_factor: float
    max_drawdown_r_mean: float
    max_drawdown_r_p95: float
    worst_run_r: float
    best_run_r: float
    risk_of_ruin: float
    r_distribution: List[float] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Monte Carlo: {self.runs:,} runs x {self.trades_per_run} trades "
            f"({self.total_trades:,} trades total)",
            f"  Win rate      : {self.win_rate:.2%}  ({self.wins}W / {self.losses}L)",
            f"  Avg R         : {self.avg_r:+.3f}R",
            f"  Expectancy    : {self.expectancy_r:+.3f}R per trade",
            f"  Profit factor : {self.profit_factor:.2f}",
            f"  Max DD (mean) : {self.max_drawdown_r_mean:.2f}R",
            f"  Max DD (p95)  : {self.max_drawdown_r_p95:.2f}R",
            f"  Worst run     : {self.worst_run_r:+.2f}R",
            f"  Best run      : {self.best_run_r:+.2f}R",
            f"  Risk of ruin  : {self.risk_of_ruin:.2%}",
        ]
        for n in self.notes:
            lines.append(f"  note: {n}")
        return "\n".join(lines)


# ---------------------------------------------------------------------
# TimeMachine
# ---------------------------------------------------------------------
class TimeMachine:
    """
    Replays history once to build an R-multiple sample, then bootstraps
    10,000 Monte Carlo sessions to estimate distributional statistics.
    """

    def __init__(
        self,
        engine: AdaptiveICTEngine,
        exit_planner: ExitPlanner,
        risk_manager: RiskManager,
        specs: Optional[Dict[str, SymbolSpec]] = None,
        spread_pips_by_symbol: Optional[Dict[str, float]] = None,
        seed: Optional[int] = 1337,
        timeout_bars: int = 200,
        slippage_r_std: float = 0.05,
    ):
        self.engine = engine
        self.exit_planner = exit_planner
        self.risk = risk_manager
        self.specs = specs or risk_manager.specs
        self.spread_pips = spread_pips_by_symbol or {
            "EUR/USD": 0.8, "GBP/USD": 1.0, "USD/JPY": 0.9,
            "XAU/USD": 2.0, "XAG/USD": 1.5, "WTI/USD": 2.0,
        }
        self.rng = random.Random(seed)
        self.timeout_bars = timeout_bars
        self.slippage_r_std = slippage_r_std

    # -- history replay ----------------------------------------------------
    def replay_history(
        self,
        candles_by_symbol: Dict[str, Sequence[Candle]],
        warmup: int = 50,
    ) -> List[float]:
        """Return an ordered list of simulated R-multiples from history."""
        r_multiples: List[float] = []
        for symbol, candles in candles_by_symbol.items():
            if len(candles) <= warmup + self.timeout_bars:
                logger.warning("Not enough candles for %s (%d)", symbol, len(candles))
                continue
            spec = self.specs.get(symbol)
            if spec is None:
                logger.warning("No symbol spec for %s; skipping", symbol)
                continue

            i = warmup
            while i < len(candles) - self.timeout_bars - 1:
                window = candles[max(0, i - warmup): i]
                try:
                    signal = self.engine.select_best(window, symbol)
                except Exception:
                    logger.exception("Engine failed at %s[%d]", symbol, i)
                    signal = None
                if signal is None:
                    i += 1
                    continue

                plan = self.exit_planner.plan(signal, window, spec)
                if not plan.valid:
                    i += 1
                    continue

                decision = self.risk.size(
                    symbol, plan.direction.value, plan.entry,
                    plan.stop_loss, plan.take_profit,
                )
                if not decision.approved:
                    i += 1
                    continue

                outcome = simulate_trade_outcome(
                    candles=candles,
                    plan=plan,
                    start_index=i,
                    timeout_bars=self.timeout_bars,
                    spread_pips=self.spread_pips.get(symbol, 1.0),
                    pip_size=spec.pip_size,
                )
                r_multiples.append(outcome.r_multiple)
                # feedback the actual outcome into the engine's learning loop
                try:
                    self.engine.record_outcome(plan.strategy_id, outcome.r_multiple)
                except Exception:
                    pass
                i = max(i + 1, outcome.exit_index + 1)  # skip to trade resolution
        if not r_multiples:
            logger.warning("No trades produced by history replay")
        return r_multiples

    # -- bootstrap ---------------------------------------------------------
    def run(
        self,
        candles_by_symbol: Optional[Dict[str, Sequence[Candle]]] = None,
        runs: int = 10_000,
        trades_per_run: int = 40,
        synthetic_fallback: bool = True,
        ruin_threshold_r: float = 10.0,
    ) -> SimulationResult:
        sample = self.replay_history(candles_by_symbol) if candles_by_symbol else []

        if not sample and synthetic_fallback:
            logger.warning(
                "No historical sample; generating synthetic R-multiples "
                "(win_rate=0.45, avg_win=+2R, avg_loss=-1R) for testing."
            )
            sample = [
                (2.0 if self.rng.random() < 0.45 else -1.0)
                for _ in range(500)
            ]

        if not sample:
            return SimulationResult(
                runs=runs, trades_per_run=trades_per_run, total_trades=0,
                wins=0, losses=0, win_rate=0.0, avg_r=0.0, expectancy_r=0.0,
                profit_factor=0.0, max_drawdown_r_mean=0.0,
                max_drawdown_r_p95=0.0, worst_run_r=0.0, best_run_r=0.0,
                risk_of_ruin=0.0,
                notes=["no R-multiples available and synthetic_fallback=False"],
            )

        # ---- bootstrap loop ----
        total_trades = 0
        total_wins = 0
        total_losses = 0
        sum_r = 0.0
        gross_win = 0.0
        gross_loss = 0.0
        dd_per_run: List[float] = []
        run_totals: List[float] = []
        ruin_hits = 0

        for _ in range(runs):
            equity_r = 0.0
            peak_r = 0.0
            max_dd = 0.0
            for _ in range(trades_per_run):
                r = self.rng.choice(sample)
                # optional slippage/jitter
                if self.slippage_r_std > 0:
                    r += self.rng.gauss(0.0, self.slippage_r_std)
                equity_r += r
                peak_r = max(peak_r, equity_r)
                max_dd = max(max_dd, peak_r - equity_r)

                total_trades += 1
                sum_r += r
                if r > 0:
                    total_wins += 1
                    gross_win += r
                else:
                    total_losses += 1
                    gross_loss += -r
            run_totals.append(equity_r)
            dd_per_run.append(max_dd)
            if max_dd >= ruin_threshold_r:
                ruin_hits += 1

        dd_per_run.sort()
        run_totals.sort()
        p95_idx = min(len(dd_per_run) - 1, int(round(0.95 * (len(dd_per_run) - 1))))

        pf = gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win else 0.0
        result = SimulationResult(
            runs=runs,
            trades_per_run=trades_per_run,
            total_trades=total_trades,
            wins=total_wins,
            losses=total_losses,
            win_rate=total_wins / total_trades if total_trades else 0.0,
            avg_r=sum_r / total_trades if total_trades else 0.0,
            expectancy_r=sum_r / total_trades if total_trades else 0.0,
            profit_factor=pf,
            max_drawdown_r_mean=sum(dd_per_run) / len(dd_per_run) if dd_per_run else 0.0,
            max_drawdown_r_p95=dd_per_run[p95_idx] if dd_per_run else 0.0,
            worst_run_r=run_totals[0] if run_totals else 0.0,
            best_run_r=run_totals[-1] if run_totals else 0.0,
            risk_of_ruin=ruin_hits / runs if runs else 0.0,
            r_distribution=sample[:1000],
            notes=[
                f"sample size for bootstrap: {len(sample)}",
                f"ruin defined as max drawdown >= {ruin_threshold_r:.1f}R",
                f"slippage jitter sigma: {self.slippage_r_std:.3f}R",
            ],
        )
        logger.info("TimeMachine completed: %s", result.summary())
        return result


# ---------------------------------------------------------------------
# Convenience helper
# ---------------------------------------------------------------------
def simulate(
    engine: AdaptiveICTEngine,
    exit_planner: ExitPlanner,
    risk_manager: RiskManager,
    candles_by_symbol: Optional[Dict[str, Sequence[Candle]]] = None,
    runs: int = 10_000,
    trades_per_run: int = 40,
    seed: int = 1337,
) -> SimulationResult:
    tm = TimeMachine(engine, exit_planner, risk_manager, seed=seed)
    return tm.run(
        candles_by_symbol=candles_by_symbol,
        runs=runs,
        trades_per_run=trades_per_run,
    )
