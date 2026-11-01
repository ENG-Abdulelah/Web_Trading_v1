# main.py
"""
main.py — master orchestrator for the automated trading bot.

Wires together every module:
    config                  → capital, assets, automation mode
    core.sessions           → session tracking + shutdown callbacks
    core.data_feed          → price/news feeds + trading guard
    analysis.ict_engine     → adaptive SMC strategies
    analysis.fundamentals   → macro / geopolitical bias
    analysis.scorer         → alignment score & 5.5/10 gate
    execution.risk_manager  → position sizing + concurrency
    execution.entry_exit    → SL/TP planning + bar-by-bar simulation
    execution.broker_connector → order submission + transparent tickets
    web_app                 → live dashboard (optional)

Run modes
---------
    python main.py                         # demo paper-trading (synthetic prices)
    python main.py --mode multi            # allow up to 3 concurrent trades
    python main.py --with-web              # also start the dashboard on :8000
    python main.py --sim                   # 10,000-run Monte Carlo then exit
    python main.py --live --with-web       # requires a wired broker adapter

Signals are generated on bar close, sized by RiskManager, scored by
TradeScorer, and only executed when ScoredTrade.approved is True
(total ≥ 5.5/10 by default). Positions are managed bar-by-bar; on SL/TP
hit the trade closes through SimulatedBroker, updates the risk ledger,
and feeds its R-multiple back into AdaptiveICTEngine for iterative
strategy tuning.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# -------------------------------------------------------------------------
# Path bootstrap so `python main.py` works from any cwd
# -------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# -------------------------------------------------------------------------
# Project imports
# -------------------------------------------------------------------------
from config import (  # noqa: E402
    ASSETS,
    ASSET_SYMBOLS,
    AUTOMATION_MODES,
    DEFAULT_CAPITAL,
    RISK_PER_TRADE,
    TIMEFRAMES,
)

from core.sessions import SessionManager  # noqa: E402
from core.data_feed import (  # noqa: E402
    NewsEvent,
    NewsImpact,
    NewsStreamParser,
    TradingGuard,
)

from analysis.ict_engine import (  # noqa: E402
    AdaptiveICTEngine,
    Candle,
    Direction,
    FVGStrategy,
    LiquiditySweepStrategy,
    OrderBlockStrategy,
    atr,
    detect_regime,
)
from analysis.fundamentals import (  # noqa: E402
    FundamentalAnalyzer,
    GeopoliticalEvent,
    MacroSnapshot,
    RiskAssessment,
    RiskRegime,
)
from analysis.scorer import (  # noqa: E402
    ScoredTrade,
    TradeScorer,
    ValidationError,
)

from execution.risk_manager import RiskManager, SymbolSpec  # noqa: E402
from execution.entry_exit import ExitPlanner  # noqa: E402
from execution.broker_connector import (  # noqa: E402
    PlaceholderBroker,
    SimulatedBroker,
    ticket_from_plan,
)
from execution.timemachine import TimeMachine  # noqa: E402

try:
    from web_app import app as web_app_module  # noqa: E402
    WEB_AVAILABLE = True
except Exception:  # pragma: no cover
    web_app_module = None
    WEB_AVAILABLE = False


# -------------------------------------------------------------------------
# Logging
# -------------------------------------------------------------------------
def configure_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-22s :: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # Quiet down chatty third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


logger = logging.getLogger("main")


# -------------------------------------------------------------------------
# Seed prices / volatility for the demo feed
# -------------------------------------------------------------------------
SEED_PRICES: Dict[str, float] = {
    "XAU/USD": 2348.50,
    "XAG/USD": 27.42,
    "WTI/USD": 78.15,
    "EUR/USD": 1.08500,
    "GBP/USD": 1.27310,
    "USD/JPY": 149.85,
}

BASE_VOL: Dict[str, float] = {
    "XAU/USD": 0.0016,
    "XAG/USD": 0.0024,
    "WTI/USD": 0.0030,
    "EUR/USD": 0.0004,
    "GBP/USD": 0.0005,
    "USD/JPY": 0.0004,
}


# -------------------------------------------------------------------------
# TradingBot
# -------------------------------------------------------------------------
@dataclass
class OpenTrade:
    ticket_id: str
    plan: Any                       # TradePlan
    decision: Any                   # SizingDecision
    position: Any                   # OpenPosition (from RiskManager)
    scored: ScoredTrade
    opened_at: datetime
    bars_open: int = 0


class TradingBot:
    """Master orchestrator. See module docstring for the wiring summary."""

    def __init__(
        self,
        capital: float = DEFAULT_CAPITAL,
        automation_mode: str = "single",
        paper: bool = True,
        demo: bool = True,
        bar_seconds: int = 30,
        tick_seconds: float = 1.0,
        max_history: int = 500,
        seed: Optional[int] = 42,
        with_web: bool = False,
        web_host: str = "0.0.0.0",
        web_port: int = 8000,
    ) -> None:
        self.capital = float(capital)
        self.automation_mode = automation_mode
        self.paper = paper
        self.demo = demo
        self.bar_seconds = int(bar_seconds)
        self.tick_seconds = float(tick_seconds)
        self.max_history = int(max_history)
        self.rng = random.Random(seed)
        self.with_web = with_web
        self.web_host = web_host
        self.web_port = web_port

        self.symbols: List[str] = list(ASSET_SYMBOLS)

        # ---- Core subsystems --------------------------------------------
        self.session_mgr = SessionManager(
            shutdown_callbacks=[self._on_market_shutdown],
        )
        self.risk = RiskManager(
            capital=self.capital,
            risk_per_trade=RISK_PER_TRADE,
            automation_mode=automation_mode,
            strict_min_lot=True,
        )

        # ---- Analysis ----------------------------------------------------
        self.engine = AdaptiveICTEngine(
            strategies=[
                OrderBlockStrategy(),
                FVGStrategy(),
                LiquiditySweepStrategy(),
            ],
            min_samples=8,
            min_win_rate=0.45,
            auto_optimize=True,
            optimize_every=4,
        )
        self.fundamentals = FundamentalAnalyzer()
        self.scorer = TradeScorer(
            technical_weight=0.5,
            macro_weight=0.3,
            geopolitical_weight=0.2,
            min_score=TradeScorer.MIN_SCORE,
            require_alignment=0,
        )

        # ---- Execution ---------------------------------------------------
        self.exit_planner = ExitPlanner(
            min_rr=1.5,
            sl_atr_floor_mult=0.6,
            structure_lookback=5,
            partial_exits=True,
            trailing=True,
        )
        self.broker = (
            SimulatedBroker(starting_balance=self.capital)
            if paper
            else PlaceholderBroker()
        )

        # ---- News guard --------------------------------------------------
        self.guard = TradingGuard(
            symbols=self.symbols,
            currencies=("USD", "EUR", "GBP", "JPY"),
            pre_event_window=timedelta(minutes=5),
            post_event_stabilization=timedelta(minutes=10),
            suspension_callbacks=[self._on_news_suspend],
            resume_callbacks=[self._on_news_resume],
        )
        self.news_parser: Optional[NewsStreamParser] = None

        # ---- Runtime state ----------------------------------------------
        self.history: Dict[str, List[Candle]] = {s: [] for s in self.symbols}
        self.positions: Dict[str, OpenTrade] = {}
        self._last_scores: Dict[str, ScoredTrade] = {}
        self._stop = asyncio.Event()
        self._started_at = datetime.now(timezone.utc)
        self._bars_closed = 0
        self._signals_evaluated = 0
        self._signals_executed = 0

    # ==================================================================
    # Lifecycle
    # ==================================================================
    async def run(self) -> None:
        self._install_signal_handlers()
        await self._startup()
        web_task: Optional[asyncio.Task] = None
        try:
            if self.with_web and WEB_AVAILABLE:
                self._attach_web_state()
                web_task = asyncio.create_task(self._run_web())
            await self._main_loop()
        finally:
            if web_task is not None:
                web_task.cancel()
                try:
                    await web_task
                except asyncio.CancelledError:
                    pass
            await self._shutdown()

    async def _startup(self) -> None:
        logger.info("=" * 74)
        logger.info("Trading bot starting")
        logger.info(
            "  mode            : %s (%s)",
            self.automation_mode,
            "paper" if self.paper else "live",
        )
        logger.info(
            "  feed            : %s",
            "synthetic (demo)" if self.demo else "live",
        )
        logger.info(
            "  capital         : $%.2f  risk/trade: %.2f%% ($%.4f)",
            self.capital,
            RISK_PER_TRADE * 100,
            self.capital * RISK_PER_TRADE,
        )
        logger.info(
            "  max concurrent  : %d (%s)",
            self.risk.max_concurrent,
            AUTOMATION_MODES[self.automation_mode]["description"],
        )
        logger.info(
            "  min align score : %.2f / 10",
            self.scorer.min_score,
        )
        logger.info("  symbols         : %s", ", ".join(self.symbols))
        logger.info("  web dashboard   : %s", "enabled" if self.with_web else "disabled")
        logger.info("=" * 74)

        # Seed history so strategies have enough structure on the first bar.
        for sym in self.symbols:
            self.history[sym] = self._seed_history(sym, n=200)

        # Start news parser only in live mode (needs vendor wiring).
        if not self.demo:
            self._start_news_parser()

    async def _shutdown(self) -> None:
        logger.info("-" * 74)
        logger.info("Trading bot shutting down")
        # Close any open positions at last known price.
        for tid, trade in list(self.positions.items()):
            last = self.history[trade.plan.symbol][-1].close
            self._close_trade(tid, exit_price=last, reason="shutdown")
        runtime = (datetime.now(timezone.utc) - self._started_at).total_seconds()
        logger.info(
            "  runtime         : %.0fs (%d bars, %d signals, %d executed)",
            runtime, self._bars_closed, self._signals_evaluated, self._signals_executed,
        )
        logger.info("  final capital   : $%.2f", self.risk.capital)
        logger.info("  strategy perf   : %s", self.engine.performance_snapshot())
        logger.info("-" * 74)

    def _install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, self._stop.set)
                except NotImplementedError:  # Windows
                    signal.signal(sig, lambda *_: self._stop.set())
        except Exception:
            logger.debug("Signal handler install failed", exc_info=True)

    # ==================================================================
    # Main loop
    # ==================================================================
    async def _main_loop(self) -> None:
        last_bar_time: Dict[str, datetime] = {
            s: datetime.now(timezone.utc) for s in self.symbols
        }
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            try:
                self.session_mgr.check_market_and_shutdown(now)

                for sym in self.symbols:
                    if (now - last_bar_time[sym]).total_seconds() >= self.bar_seconds:
                        await self._on_bar_close(sym, now)
                        last_bar_time[sym] = now
            except Exception:
                logger.exception("Error in main loop tick")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)
            except asyncio.TimeoutError:
                pass

    # ==================================================================
    # Bar close handling
    # ==================================================================
    async def _on_bar_close(self, symbol: str, now: datetime) -> None:
        bar = self._gen_candle(symbol, now)
        self._append_history(symbol, bar)
        self._bars_closed += 1

        # Manage any open position on this symbol first.
        self._update_open_trades(symbol, bar)

        # Market-hours guard
        if not self.session_mgr.is_market_open(symbol, now):
            return
        # News guard
        if self.guard.is_suspended(now):
            return
        # Concurrency guard (redundant with risk manager, but cheap)
        ok, why = self.risk.can_open_new_trade(symbol)
        if not ok:
            logger.debug("Skip %s: %s", symbol, why)
            return

        # ---- Pipeline -----------------------------------------------
        candles = self.history[symbol]
        if len(candles) < 60:
            return

        try:
            signal = self.engine.select_best(candles, symbol)
        except Exception:
            logger.exception("Engine error on %s", symbol)
            return
        if signal is None or signal.direction == Direction.NEUTRAL:
            return
        self._signals_evaluated += 1

        spec = self.risk.specs.get(symbol)
        if spec is None:
            return

        plan = self.exit_planner.plan(signal, candles, spec)
        if not plan.valid:
            logger.debug("Invalid plan %s: %s", symbol, plan.validation_reason)
            return

        decision = self.risk.size(
            symbol=plan.symbol,
            direction=plan.direction.value,
            entry=plan.entry,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
        )
        if not decision.approved:
            logger.debug("Rejected size %s: %s", symbol, decision.reason)
            return

        # ---- Score the trade ---------------------------------------
        bias = self.fundamentals.symbol_bias(
            symbol, self._macro_snapshot(), self._recent_news(), self._recent_geo(),
        )
        risk_assessment = self.fundamentals.geopolitical_risk(self._recent_geo())
        scored = self.scorer.score(signal, bias, risk_assessment)
        self._last_scores[symbol] = scored

        try:
            self.scorer.validate_or_raise(scored)
        except ValidationError as exc:
            logger.info("Trade rejected by scorer on %s: %s", symbol, exc)
            return

        # ---- Execute ------------------------------------------------
        request = ticket_from_plan(plan, decision, score=scored.total)
        try:
            ticket = self.broker.submit(request, spec, decision)
        except NotImplementedError:
            logger.error("Broker not wired for live execution; aborting trade.")
            return

        position = self.risk.reserve(
            decision,
            entry=plan.entry,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            strategy_id=plan.strategy_id,
        )
        self.positions[ticket.ticket_id] = OpenTrade(
            ticket_id=ticket.ticket_id,
            plan=plan,
            decision=decision,
            position=position,
            scored=scored,
            opened_at=now,
        )
        self._signals_executed += 1

        logger.info(
            "OPEN %-8s %-5s %.2f lots @ %.5f  SL=%.5f  TP=%.5f  "
            "R:R 1:%.2f  score=%.2f",
            ticket.symbol, ticket.side, ticket.lots, ticket.filled_entry,
            plan.stop_loss, plan.take_profit, plan.rr, scored.total,
        )
        logger.info("\n%s", ticket.render())

    # ==================================================================
    # Position management
    # ==================================================================
    def _update_open_trades(self, symbol: str, bar: Candle) -> None:
        for tid, trade in list(self.positions.items()):
            plan = trade.plan
            if plan.symbol != symbol:
                continue
            is_long = plan.direction.value == "long"

            # Stop-loss checked first (conservative assumption)
            if is_long and bar.low <= plan.stop_loss:
                self._close_trade(tid, plan.stop_loss, "stop")
                continue
            if not is_long and bar.high >= plan.stop_loss:
                self._close_trade(tid, plan.stop_loss, "stop")
                continue

            # Take-profit
            if is_long and bar.high >= plan.take_profit:
                self._close_trade(tid, plan.take_profit, "take_profit")
                continue
            if not is_long and bar.low <= plan.take_profit:
                self._close_trade(tid, plan.take_profit, "take_profit")
                continue

            # Timeout (bar-count, ~4 hours at 1m bars in prod; demo scales)
            trade.bars_open += 1
            if trade.bars_open >= 200:
                self._close_trade(tid, bar.close, "timeout")

    def _close_trade(self, ticket_id: str, exit_price: float, reason: str) -> None:
        trade = self.positions.pop(ticket_id, None)
        if trade is None:
            return
        plan = trade.plan
        risk_dist = abs(plan.entry - plan.stop_loss)
        is_long = plan.direction.value == "long"
        if risk_dist <= 0:
            r = 0.0
        elif is_long:
            r = (exit_price - plan.entry) / risk_dist
        else:
            r = (plan.entry - exit_price) / risk_dist

        try:
            self.broker.close(ticket_id, exit_price, r)
        except Exception:
            logger.exception("Broker close failed for %s", ticket_id)

        self.risk.release(trade.position.position_id, r)
        try:
            self.engine.record_outcome(plan.strategy_id, r)
        except Exception:
            logger.exception("record_outcome failed")

        logger.info(
            "CLOSE %-8s %-5s @ %.5f  %+.2fR  reason=%s  capital=$%.2f",
            plan.symbol, plan.direction.value, exit_price, r, reason,
            self.risk.capital,
        )

    # ==================================================================
    # Callbacks
    # ==================================================================
    def _on_market_shutdown(self, reason: str) -> None:
        logger.warning("Market shutdown triggered: %s", reason)
        for tid, trade in list(self.positions.items()):
            try:
                last = self.history[trade.plan.symbol][-1].close
            except Exception:
                last = trade.plan.entry
            self._close_trade(tid, last, "market_shutdown")

    def _on_news_suspend(self, event: NewsEvent) -> None:
        logger.warning("News suspension: %s (impact=%s)",
                       event.title, getattr(event.impact, "value", event.impact))

    def _on_news_resume(self) -> None:
        logger.info("News suspension lifted; trading resumed")

    # ==================================================================
    # Demo data generation
    # ==================================================================
    def _seed_history(self, symbol: str, n: int = 200) -> List[Candle]:
        price = SEED_PRICES.get(symbol, 100.0)
        vol = BASE_VOL.get(symbol, 0.001)
        start = datetime.now(timezone.utc) - timedelta(minutes=n)
        candles: List[Candle] = []
        for i in range(n):
            o = price
            drift = self.rng.gauss(0.0, vol)
            c = o * (1.0 + drift)
            hi = max(o, c) * (1.0 + abs(self.rng.gauss(0.0, vol * 0.6)))
            lo = min(o, c) * (1.0 - abs(self.rng.gauss(0.0, vol * 0.6)))
            candles.append(Candle(
                symbol=symbol,
                open_time=start + timedelta(minutes=i),
                open=o, high=hi, low=lo, close=c,
                volume=self.rng.uniform(100, 1000),
            ))
            price = c
        return candles

    def _gen_candle(self, symbol: str, now: datetime) -> Candle:
        last = self.history[symbol][-1] if self.history[symbol] else None
        vol = BASE_VOL.get(symbol, 0.001)
        o = last.close if last else SEED_PRICES.get(symbol, 100.0)
        drift = self.rng.gauss(0.0, vol)
        c = o * (1.0 + drift)
        hi = max(o, c) * (1.0 + abs(self.rng.gauss(0.0, vol * 0.6)))
        lo = min(o, c) * (1.0 - abs(self.rng.gauss(0.0, vol * 0.6)))
        return Candle(
            symbol=symbol,
            open_time=now,
            open=o, high=hi, low=lo, close=c,
            volume=self.rng.uniform(100, 1000),
        )

    def _append_history(self, symbol: str, candle: Candle) -> None:
        h = self.history[symbol]
        h.append(candle)
        if len(h) > self.max_history:
            del h[: len(h) - self.max_history]

    # ==================================================================
    # News parser (live mode only)
    # ==================================================================
    def _start_news_parser(self) -> None:
        self.news_parser = NewsStreamParser()
        asyncio.create_task(self._news_loop())
        logger.info("News stream parser started")

    async def _news_loop(self) -> None:
        assert self.news_parser is not None
        try:
            await self.news_parser.start()
            async for event in self.news_parser.stream():
                await self.guard.handle_event(event)
        except NotImplementedError:
            logger.info("News parser not wired; running without news guard")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("News loop error")

    # ==================================================================
    # Fundamental inputs (static demo values)
    # ==================================================================
    @staticmethod
    def _macro_snapshot() -> MacroSnapshot:
        return MacroSnapshot(
            interest_rates={"USD": 5.25, "EUR": 4.00, "GBP": 5.25, "JPY": -0.10},
            inflation_yoy={"USD": 3.2, "EUR": 2.4, "GBP": 3.4, "JPY": 2.8},
            gdp_growth={"USD": 2.4, "EUR": 0.6, "GBP": 0.3, "JPY": 0.9},
            unemployment={"USD": 3.9, "EUR": 6.5, "GBP": 4.2, "JPY": 2.6},
        )

    @staticmethod
    def _recent_news() -> List[NewsEvent]:
        now = datetime.now(timezone.utc)
        return [
            NewsEvent(
                id="demo-cpi",
                title="US CPI in line with expectations",
                source="demo",
                published_at=now - timedelta(hours=1),
                impact=NewsImpact.MEDIUM,
                currencies=("USD",),
                tags=("cpi",),
            ),
        ]

    @staticmethod
    def _recent_geo() -> List[GeopoliticalEvent]:
        now = datetime.now(timezone.utc)
        return [
            GeopoliticalEvent(
                severity=0.35,
                region="Middle East",
                tag="energy",
                description="Regional supply tension (demo)",
                published_at=now - timedelta(hours=2),
            ),
        ]

    # ==================================================================
    # Web dashboard integration
    # ==================================================================
    def _attach_web_state(self) -> None:
        if not WEB_AVAILABLE or web_app_module is None:
            logger.warning("Web app not available; skipping dashboard wiring")
            return
        try:
            bot = self
            def build() -> Dict[str, Any]:
                return bot.build_web_state()
            web_app_module.provider.build_state = build  # type: ignore[assignment]
            logger.info("Live state wired into web dashboard")
        except Exception:
            logger.exception("Failed to wire web state")

    def build_web_state(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        sessions: List[Dict[str, Any]] = []
        for sess in self.session_mgr.sessions:
            active = sess.contains(now)
            ref = sess.next_close_utc(now) if active else sess.next_open_utc(now)
            sessions.append({
                "id": sess.name.value,
                "label_key": f"session_{sess.name.value}",
                "active": active,
                "open": sess.open_utc.strftime("%H:%M"),
                "close": sess.close_utc.strftime("%H:%M"),
                "next_change_label_key": "closes_in" if active else "next_open_in",
                "next_change_secs": int((ref - now).total_seconds()),
            })

        positions: List[Dict[str, Any]] = []
        for trade in self.positions.values():
            plan = trade.plan
            last_close = self.history[plan.symbol][-1].close
            risk = abs(plan.entry - plan.stop_loss) or 1e-9
            is_long = plan.direction.value == "long"
            pnl_r = ((last_close - plan.entry) if is_long else (plan.entry - last_close)) / risk
            positions.append({
                "id": trade.ticket_id,
                "symbol": plan.symbol,
                "side": plan.direction.value,
                "lots": trade.decision.lots,
                "entry": plan.entry,
                "stop_loss": plan.stop_loss,
                "take_profit": plan.take_profit,
                "risk_usd": trade.decision.risk_usd,
                "reward_usd": trade.decision.reward_usd,
                "rr": plan.rr,
                "strategy": plan.strategy_id,
                "pnl_r": round(pnl_r, 2),
                "pnl_usd": round(pnl_r * trade.decision.risk_usd, 2),
                "opened_at": trade.opened_at.isoformat(),
            })

        assets: List[Dict[str, Any]] = []
        for sym in self.symbols:
            h = self.history[sym]
            if not h:
                continue
            last = h[-1]
            prev = h[-2] if len(h) > 1 else last
            chg = (last.close - prev.open) / prev.open * 100 if prev.open else 0.0
            assets.append({
                "symbol": sym,
                "price": round(last.close, 5),
                "change_pct": round(chg, 3),
                "open": self.session_mgr.is_market_open(sym, now),
            })

        scores: List[Dict[str, Any]] = []
        for sym, scored in self._last_scores.items():
            scores.append({
                "symbol": sym,
                "direction": scored.direction.value,
                "technical": scored.technical,
                "macro": scored.macro,
                "geopolitical": scored.geopolitical,
                "total": scored.total,
                "approved": scored.approved,
            })

        return {
            "timestamp": now.isoformat(),
            "market_open": self.session_mgr.any_market_open(now),
            "suspended": self.guard.is_suspended(now),
            "capital": {
                "balance": round(self.risk.capital, 2),
                "risk_pct": self.risk.risk_per_trade,
                "risk_usd": round(self.risk.capital * self.risk.risk_per_trade, 4),
                "max_concurrent": self.risk.max_concurrent,
                "mode": self.risk.automation_mode,
            },
            "sessions": sessions,
            "positions": positions,
            "assets": assets,
            "scores": scores,
            "min_score": self.scorer.min_score,
            "demo": self.demo,
        }

    async def _run_web(self) -> None:
        try:
            import uvicorn
        except ImportError:
            logger.error("uvicorn not installed; dashboard disabled")
            return
        config = uvicorn.Config(
            "web_app.app:app",
            host=self.web_host,
            port=self.web_port,
            log_level="info",
            access_log=False,
        )
        server = uvicorn.Server(config)
        logger.info("Web dashboard at http://%s:%d", self.web_host, self.web_port)
        try:
            await server.serve()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Web server crashed")


# -------------------------------------------------------------------------
# One-shot Monte Carlo simulation
# -------------------------------------------------------------------------
def run_simulation(
    capital: float,
    automation_mode: str,
    runs: int,
    trades_per_run: int,
    seed: int,
) -> None:
    logger.info("Running Monte Carlo: %d runs x %d trades", runs, trades_per_run)
    engine = AdaptiveICTEngine(
        strategies=[
            OrderBlockStrategy(),
            FVGStrategy(),
            LiquiditySweepStrategy(),
        ]
    )
    planner = ExitPlanner(min_rr=1.5)
    risk = RiskManager(
        capital=capital,
        risk_per_trade=RISK_PER_TRADE,
        automation_mode=automation_mode,
    )
    tm = TimeMachine(engine, planner, risk, seed=seed)
    result = tm.run(runs=runs, trades_per_run=trades_per_run, synthetic_fallback=True)
    print()
    print(result.summary())
    print()
    print("Strategy performance snapshot:")
    for sid, snap in engine.performance_snapshot().items():
        print(f"  {sid:<16} {snap}")


# -------------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------------
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="Automated ICT/SMC trading bot orchestrator.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--capital", type=float, default=DEFAULT_CAPITAL,
                   help="Starting capital in USD")
    p.add_argument("--mode", choices=list(AUTOMATION_MODES), default="single",
                   help="Automation mode (concurrency cap)")
    p.add_argument("--live", action="store_true",
                   help="Use live broker (requires wiring)")
    p.add_argument("--paper", action="store_true", default=False,
                   help="Use simulated broker (default)")
    p.add_argument("--demo", action="store_true", default=True,
                   help="Use synthetic feed (default when --live is absent)")
    p.add_argument("--bar-seconds", type=int, default=30,
                   help="Seconds between synthetic bar closes (demo)")
    p.add_argument("--tick-seconds", type=float, default=1.0,
                   help="Main loop tick interval")
    p.add_argument("--with-web", action="store_true",
                   help="Start the FastAPI dashboard alongside the bot")
    p.add_argument("--web-host", default="0.0.0.0")
    p.add_argument("--web-port", type=int, default=8000)
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--log-file", default=None)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--sim", action="store_true",
                   help="Run Monte Carlo simulation then exit")
    p.add_argument("--sim-runs", type=int, default=10_000)
    p.add_argument("--sim-trades", type=int, default=40)
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level, args.log_file)

    # Paper/live resolution: live requires explicitly opting in AND a wired
    # broker adapter; otherwise we default to paper.
    paper = not args.live

    if args.sim:
        run_simulation(
            capital=args.capital,
            automation_mode=args.mode,
            runs=args.sim_runs,
            trades_per_run=args.sim_trades,
            seed=args.seed,
        )
        return 0

    bot = TradingBot(
        capital=args.capital,
        automation_mode=args.mode,
        paper=paper,
        demo=paper and not args.live,
        bar_seconds=args.bar_seconds,
        tick_seconds=args.tick_seconds,
        seed=args.seed,
        with_web=args.with_web,
        web_host=args.web_host,
        web_port=args.web_port,
    )

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
