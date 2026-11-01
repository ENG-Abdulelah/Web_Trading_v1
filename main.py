# main.py
# =============================================================================
#   Web Trading Platform — Main Entry Point (Ultimate Glue File)
# -----------------------------------------------------------------------------
#   Developed & Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)
# -----------------------------------------------------------------------------
#   This file connects EVERY component of the platform:
#       1. SQLite persistence  (database/persistence.py)
#       2. Risk Manager        (execution/risk_manager.py)   → $50 @ 1.5%
#       3. Entry/Exit Manager  (execution/entry_exit.py)     → BE / Trail / TP1
#       4. Feedback Loop       (feedback/feedback_loop.py)   → self-learning
#       5. Data Feed           (data/feed.py)
#       6. 20 ICT Strategies   (strategy/ict_strategies.py)
#       7. Scoring Matrix      (strategy/scoring.py)
#       8. Broker Execution    (execution/broker.py)
#       9. FastAPI + Uvicorn web server (real-time UI)
# =============================================================================

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# -----------------------------------------------------------------------------
# CORE MODULES (already built in this project)
# -----------------------------------------------------------------------------
from database.persistence import PersistenceManager
from execution.entry_exit import EntryExitManager, ExitReason
from execution.risk_manager import RiskManager, RiskConfig
from feedback.feedback_loop import FeedbackLoop, FeedbackConfig

# -----------------------------------------------------------------------------
# OPTIONAL MODULES (graceful fallbacks if not present in the repo)
# -----------------------------------------------------------------------------
try:
    from data.feed import MarketDataFeed
except ImportError:
    MarketDataFeed = None  # type: ignore

try:
    from execution.broker import BrokerClient
except ImportError:
    BrokerClient = None  # type: ignore

try:
    from execution.order_manager import OrderManager
except ImportError:
    OrderManager = None  # type: ignore

try:
    from strategy.ict_strategies import ICTStrategyEngine
except ImportError:
    ICTStrategyEngine = None  # type: ignore

try:
    from strategy.scoring import ScoringMatrix
except ImportError:
    ScoringMatrix = None  # type: ignore

# -----------------------------------------------------------------------------
# WEB (FastAPI / Uvicorn)
# -----------------------------------------------------------------------------
try:
    import uvicorn
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, JSONResponse
    HAS_WEB = True
except ImportError:
    HAS_WEB = False
    FastAPI = None  # type: ignore
    uvicorn = None  # type: ignore


# =============================================================================
#   CREDITS BANNER
# =============================================================================
BANNER = r"""
╔══════════════════════════════════════════════════════════════════════════╗
║                                                                          ║
║   ██╗    ██╗███████╗██████╗     ████████╗██████╗  █████╗ ██████╗ ██╗███╗  ║
║   ██║    ██║██╔════╝██╔══██╗    ╚══██╔══╝██╔══██╗██╔══██╗██╔══██╗██║████╗ ║
║   ██║ █╗ ██║█████╗  ██████╔╝       ██║   ██████╔╝███████║██║  ██║██║██╔██╗║
║   ██║███╗██║██╔══╝  ██╔══██╗       ██║   ██╔══██╗██╔══██║██║  ██║██║██║╚██║
║   ╚███╔███╔╝███████╗██████╔╝       ██║   ██║  ██║██║  ██║██████╔╝██║██║ ╚█║
║    ╚══╝╚══╝ ╚══════╝╚═════╝        ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚═╝╚═╝  ║
║                                                                          ║
║           Automated Multi-Strategy Trading Platform  v1.0                ║
║                                                                          ║
║   Developed & Engineered by Engineer Abdulelah Sadiq                     ║
║                            ( المهندس عبد الإله صادق )                    ║
║                                                                          ║
║   Modules  : SQLite • Risk • Entry/Exit • Feedback • ICT • Scoring       ║
║   Web      : FastAPI + Uvicorn (real-time dashboard)                     ║
╚══════════════════════════════════════════════════════════════════════════╝
"""


# =============================================================================
#   LOGGING
# =============================================================================
def setup_logging(level: str = "INFO") -> None:
    Path("logs").mkdir(parents=True, exist_ok=True)
    log_file = f"logs/trading_{datetime.now(timezone.utc):%Y%m%d}.log"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


logger = logging.getLogger("main")


# =============================================================================
#   CONFIG LOADER
# =============================================================================
def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    logger.info("Config loaded from %s", path)
    return cfg


# =============================================================================
#   FALLBACK IMPLEMENTATIONS
#   (used only when the corresponding module is missing)
# =============================================================================
class PaperBroker:
    """Minimal in-memory broker for paper-trading / dry-run."""

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.balance: float = float(self.cfg.get("initial_balance", 50.0))
        self._tickets: Dict[str, dict] = {}
        self._next_id = 1
        logger.warning("PaperBroker aktif — لا يوجد اتصال بوسيط حقيقي (paper mode).")

    def connect(self) -> None: logger.info("[PaperBroker] connected")
    def disconnect(self) -> None: logger.info("[PaperBroker] disconnected")
    def get_balance(self) -> float: return self.balance

    def open_market_order(self, symbol, side, lot_size, sl=None, tp=None, **kw) -> dict:
        ticket = f"PAPER-{self._next_id:06d}"
        self._next_id += 1
        price = float(kw.get("price", 0.0)) or 0.0
        self._tickets[ticket] = dict(symbol=symbol, side=side, lot=lot_size,
                                     sl=sl, tp=tp, entry=price)
        logger.info("[PaperBroker] OPEN %s %s %s lot=%s", side, symbol, ticket, lot_size)
        return {"ticket_id": ticket, "filled_price": price, "status": "filled"}

    def place_pending_order(self, symbol, side, order_type, price, lot_size, sl=None, tp=None, **kw) -> str:
        ticket = f"PEND-{self._next_id:06d}"
        self._next_id += 1
        self._tickets[ticket] = dict(symbol=symbol, side=side, type=order_type,
                                     price=price, lot=lot_size, sl=sl, tp=tp)
        logger.info("[PaperBroker] PENDING %s %s %s @%s", order_type, side, symbol, price)
        return ticket

    def close_position(self, ticket_id: str, price: float, reason: str = "") -> dict:
        info = self._tickets.pop(ticket_id, None)
        if info:
            logger.info("[PaperBroker] CLOSE %s @%s (%s)", ticket_id, price, reason)
        return {"ticket_id": ticket_id, "close_price": price, "status": "closed"}

    def cancel_pending(self, order_id: str) -> None:
        self._tickets.pop(order_id, None)


class SimulatedFeed:
    """Minimal tick/candle feed for testing without external data source."""

    def __init__(self, cfg: dict, symbols: List[str]):
        self.symbols = symbols or ["XAU/USD"]
        self._base = {s: 2350.0 if "XAU" in s else (1.10 if "EUR/USD" in s else 65000.0)
                      for s in self.symbols}
        self._candles: Dict[str, dict] = {}
        logger.warning("SimulatedFeed aktif — أسعار محاكاة عشوائية.")

    def connect(self) -> None: logger.info("[Feed] connected (simulated)")
    def disconnect(self) -> None: pass

    def get_current_prices(self, symbols: List[str]) -> Dict[str, float]:
        import random
        return {s: round(self._base[s] * (1 + random.uniform(-0.001, 0.001)), 5)
                for s in symbols if s in self._base}

    def get_atr(self, symbols: List[str], period: int = 14) -> Dict[str, float]:
        return {s: 1.2 if "XAU" in s else 0.001 for s in symbols if s in self._base}

    def get_last_closed_candle(self, symbol: str) -> Optional[dict]:
        if symbol not in self._base:
            return None
        import random
        price = self._base[symbol]
        now_min = int(time.time() // 60)
        if self._candles.get(symbol, {}).get("time_min") == now_min:
            return None  # already served
        candle = {
            "time": datetime.now(timezone.utc),
            "open": price * (1 + random.uniform(-0.0005, 0.0005)),
            "high": price * (1 + random.uniform(0, 0.001)),
            "low":  price * (1 - random.uniform(0, 0.001)),
            "close": price * (1 + random.uniform(-0.0005, 0.0005)),
            "volume": random.randint(100, 1000),
        }
        self._candles[symbol] = {"time_min": now_min, **candle}
        return candle


# ---- 20 ICT strategies fallback ------------------------------------------------
ICT_STRATEGY_NAMES = [
    "order_blocks", "fair_value_gap", "liquidity_sweep", "break_of_structure",
    "change_of_character", "market_structure_shift", "optimal_trade_entry",
    "premium_discount", "killzone_london", "killzone_newyork", "judas_swing",
    "power_of_three", "mitigation_block", "imbalance", "volume_imbalance",
    "liquidity_void", "daily_open", "previous_day_hl", "asian_range",
    "silver_bullet",
]


class FallbackICTEngine:
    """Placeholder ICT engine registering all 20 strategies with simple heuristics."""

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.strategies = list(ICT_STRATEGY_NAMES)
        logger.warning("FallbackICTEngine aktif — 20 ICT strategies (heuristics).")

    def analyze(self, symbol: str, candle: dict, current_price: float) -> List[dict]:
        import random
        results: List[dict] = []
        for name in self.strategies:
            score = random.uniform(-1, 1)
            side = "buy" if score > 0.3 else ("sell" if score < -0.3 else None)
            results.append({
                "strategy": name,
                "symbol": symbol,
                "score": round(score, 4),
                "side": side,
                "strength": abs(score),
            })
        return results


class FallbackScoring:
    """Weighted-average scoring matrix fallback."""

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.min_strength = float(self.cfg.get("min_signal_strength", 0.55))

    def combine(self, analyses: List[dict], weights: Optional[Dict[str, float]] = None) -> dict:
        weights = weights or {}
        total_w = 0.0
        net = 0.0
        buy_score = 0.0
        sell_score = 0.0
        for a in analyses:
            w = float(weights.get(a["strategy"], 1.0))
            net += a["score"] * w
            total_w += w
            if a["score"] > 0:
                buy_score += a["score"] * w
            else:
                sell_score += -a["score"] * w
        if total_w == 0:
            return {"side": None, "strength": 0.0}
        normalized = net / total_w
        strength = abs(normalized)
        side = "buy" if normalized > 0 else ("sell" if normalized < 0 else None)
        if strength < self.min_strength:
            return {"side": None, "strength": strength, "buy_score": buy_score,
                    "sell_score": sell_score}
        return {"side": side, "strength": strength,
                "buy_score": round(buy_score, 3), "sell_score": round(sell_score, 3)}


# =============================================================================
#   NEWS PROVIDER (ForexFactory + CPI filter, cache-backed)
# =============================================================================
class NewsProvider:
    """Fetches economic calendar events (ForexFactory JSON, cached 5 min)."""

    FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.cache_ttl = float(self.cfg.get("cache_ttl", 300))
        self._cache: List[dict] = []
        self._last_fetch: float = 0.0
        self._lock = threading.Lock()

    def get_events(self, impact: Optional[str] = None,
                   keyword: Optional[str] = None) -> List[dict]:
        with self._lock:
            now = time.time()
            if now - self._last_fetch > self.cache_ttl:
                try:
                    req = urllib.request.Request(self.FF_URL, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=8) as r:
                        data = json.loads(r.read().decode("utf-8"))
                    self._cache = data if isinstance(data, list) else []
                    self._last_fetch = now
                except Exception as e:
                    logger.warning("news fetch failed: %s", e)
            events = list(self._cache)

        if impact:
            events = [e for e in events if str(e.get("impact", "")).lower() == impact.lower()]
        if keyword:
            k = keyword.lower()
            events = [e for e in events
                      if k in str(e.get("title", "")).lower()
                      or k in str(e.get("country", "")).lower()]
        return events

    def cpi_events(self) -> List[dict]:
        return self.get_events(keyword="CPI")

    def high_impact(self) -> List[dict]:
        return self.get_events(impact="High")


# =============================================================================
#   TRADING APPLICATION (core orchestrator, thread-safe)
# =============================================================================
class TradingApplication:
    """The live trading bot. Runs in a background thread."""

    def __init__(self, config: dict):
        self.config = config
        self.symbols: List[str] = config.get("symbols", ["XAU/USD"])
        self.loop_interval: float = float(config.get("loop_interval_sec", 5.0))
        self.default_lot: float = float(config.get("default_lot", 0.001))
        self.db_path: str = config.get("db_path", "data/trading.db")

        # ---- 1) Persistence ----
        self.persistence = PersistenceManager(db_path=self.db_path)

        # ---- 2) Feedback Loop (attached to persistence file) ----
        self.feedback = FeedbackLoop(
            self.persistence,
            FeedbackConfig(**(config.get("feedback", {}) or {})),
        )

        # ---- 3) Risk Manager ----
        self.risk_manager = RiskManager.from_config(
            self.persistence, config.get("risk_management", {}) or {}
        )

        # ---- 4) Entry/Exit Manager ----
        self.exit_manager = EntryExitManager(
            persistence=self.persistence,
            feedback=self.feedback,   # optional kwarg if entry_exit supports it
            breakeven_at_r=float(config.get("breakeven_at_r", 1.0)),
            trailing_start_r=float(config.get("trailing_start_r", 1.5)),
            trailing_distance_pips=float(config.get("trailing_distance_pips", 10.0)),
            partial_tp1_ratio=float(config.get("partial_tp1_ratio", 0.5)),
            pip_value_per_lot=config.get("pip_value_per_lot", {}) or {},
        ) if "feedback" in EntryExitManager.__init__.__code__.co_varnames else EntryExitManager(
            persistence=self.persistence,
            breakeven_at_r=float(config.get("breakeven_at_r", 1.0)),
            trailing_start_r=float(config.get("trailing_start_r", 1.5)),
            trailing_distance_pips=float(config.get("trailing_distance_pips", 10.0)),
            partial_tp1_ratio=float(config.get("partial_tp1_ratio", 0.5)),
            pip_value_per_lot=config.get("pip_value_per_lot", {}) or {},
        )

        # ---- 5) Broker ----
        broker_cfg = config.get("broker", {}) or {}
        if BrokerClient is not None:
            self.broker = BrokerClient(broker_cfg)
        else:
            self.broker = PaperBroker(broker_cfg)

        # ---- 6) Data feed ----
        feed_cfg = config.get("data_feed", {}) or {}
        if MarketDataFeed is not None:
            self.feed = MarketDataFeed(feed_cfg, self.symbols)
        else:
            self.feed = SimulatedFeed(feed_cfg, self.symbols)

        # ---- 7) Order manager (pending orders) ----
        if OrderManager is not None:
            self.order_manager = OrderManager(
                persistence=self.persistence, broker=self.broker
            )
        else:
            self.order_manager = None

        # ---- 8) 20 ICT strategies + scoring matrix ----
        strat_cfg = config.get("strategy", {}) or {}
        self.ict_engine = (ICTStrategyEngine(strat_cfg)
                           if ICTStrategyEngine is not None
                           else FallbackICTEngine(strat_cfg))
        self.scoring = (ScoringMatrix(strat_cfg.get("scoring", {}))
                        if ScoringMatrix is not None
                        else FallbackScoring(strat_cfg))

        # ---- 9) News ----
        self.news = NewsProvider(config.get("news", {}) or {})

        # ---- State shared with web UI ----
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._started_at = datetime.now(timezone.utc)
        self._last_cycle_at: Optional[datetime] = None
        self._last_cycle_duration: float = 0.0
        self._cycle_count: int = 0
        self._last_signals: Dict[str, dict] = {}
        self._strategy_rankings: List[dict] = []
        self._errors: List[str] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def request_shutdown(self) -> None:
        logger.warning("Shutdown requested.")
        self._shutdown.set()

    def _safe_call(self, label: str, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            msg = f"{label}: {type(e).__name__}: {e}"
            logger.exception(msg)
            with self._lock:
                self._errors.append(msg)
                self._errors = self._errors[-50:]
            return None

    def _connect(self) -> None:
        logger.info("Connecting broker & feed ...")
        self._safe_call("broker.connect", self.broker.connect)
        self._safe_call("feed.connect", self.feed.connect)
        logger.info("Connections established.")

    def _disconnect(self) -> None:
        logger.info("Disconnecting ...")
        self._safe_call("feed.disconnect", self.feed.disconnect)
        self._safe_call("broker.disconnect", self.broker.disconnect)
        try:
            self.persistence.close()
        except Exception:
            pass
        try:
            self.feedback.close()
        except Exception:
            pass
        logger.info("Disconnected.")

    # ------------------------------------------------------------------
    # Main loop (runs in worker thread)
    # ------------------------------------------------------------------
    def run(self) -> None:
        self._connect()
        logger.info("Trading loop started. Symbols=%s, interval=%.1fs",
                    self.symbols, self.loop_interval)
        try:
            while not self._shutdown.is_set():
                cycle_start = time.time()
                try:
                    self._run_cycle()
                except Exception as e:
                    logger.exception("Cycle error: %s", e)
                    with self._lock:
                        self._errors.append(f"cycle: {type(e).__name__}: {e}")
                        self._errors = self._errors[-50:]
                finally:
                    with self._lock:
                        self._cycle_count += 1
                        self._last_cycle_at = datetime.now(timezone.utc)
                        self._last_cycle_duration = time.time() - cycle_start

                # Sleep with early-exit on shutdown
                slept = 0.0
                while slept < self.loop_interval and not self._shutdown.is_set():
                    time.sleep(0.2)
                    slept += 0.2
        finally:
            self._graceful_shutdown()

    def _graceful_shutdown(self) -> None:
        logger.warning("Graceful shutdown: closing all open positions ...")
        try:
            prices = self.feed.get_current_prices(self.symbols) or {}
            self.exit_manager.close_all_positions(prices)
        except Exception as e:
            logger.error("close_all_positions failed: %s", e)
        try:
            for pos in self.exit_manager.open_positions.values():
                self.persistence.save_open_position(pos)
        except Exception as e:
            logger.error("persist state failed: %s", e)
        self._disconnect()
        logger.info("Shutdown complete.")

    # ------------------------------------------------------------------
    # One cycle
    # ------------------------------------------------------------------
    def _run_cycle(self) -> None:
        # 1) current prices + ATR
        prices = self.feed.get_current_prices(self.symbols) or {}
        if not prices:
            return
        atrs = self._safe_call("feed.atr", self.feed.get_atr, self.symbols) or {}

        # 2) Manage open positions (BE / trail / TP1 / SL / TP)
        closed_actions = self.exit_manager.update_open_positions(
            current_prices=prices, atr_values=atrs
        )
        for ticket_id, reason, exit_price in closed_actions:
            self._safe_call(
                f"broker.close_position({ticket_id})",
                self.broker.close_position, ticket_id, exit_price, reason.value,
            )

        # 3) Process pending orders (if OrderManager exists)
        if self.order_manager is not None:
            filled = self._safe_call(
                "order_manager.process_pending_orders",
                self.order_manager.process_pending_orders, prices,
            ) or []
            for order in filled:
                self.exit_manager.register_new_position(
                    ticket_id=order["ticket_id"], symbol=order["symbol"],
                    side=order["side"], entry_price=order["filled_price"],
                    sl=order["sl"], tp=order["tp"], lot_size=order["lot_size"],
                    strategy_name=order.get("strategy_name", "ict"),
                    risk_reward=order.get("risk_reward", 0.0),
                )

        # 4) Analyze candles with 20 ICT strategies
        for symbol in self.symbols:
            candle = self._safe_call(
                f"feed.candle({symbol})", self.feed.get_last_closed_candle, symbol
            )
            if not candle:
                continue
            analyses = self._safe_call(
                f"ict.analyze({symbol})", self.ict_engine.analyze,
                symbol, candle, prices[symbol],
            ) or []
            weights = self.feedback.get_all_weights() if self.feedback else {}
            combined = self.scoring.combine(analyses, weights) if analyses else {}
            with self._lock:
                self._last_signals[symbol] = {
                    "price": prices.get(symbol),
                    "combined": combined,
                    "top_strategies": sorted(
                        analyses, key=lambda x: x["strength"], reverse=True
                    )[:5],
                    "at": datetime.now(timezone.utc).isoformat(),
                }

            # 5) If a strong signal → risk check → execute
            side = combined.get("side") if combined else None
            strength = float(combined.get("strength", 0.0)) if combined else 0.0
            if side and strength >= float(self.config.get("min_signal_strength", 0.55)):
                self._try_execute(symbol, side, prices[symbol], candle)

        # 6) Refresh strategy rankings
        self._refresh_rankings()

    def _try_execute(self, symbol: str, side: str, price: float, candle: dict) -> None:
        # Compute SL/TP from candle + ATR
        atr = (self.feed.get_atr([symbol]) or {}).get(symbol, 0.0)
        sl_dist = atr * 1.5 if atr else max(price * 0.002, 0.001)
        tp_dist = sl_dist * 2.0  # 1:2 R:R

        sl = price - sl_dist if side == "buy" else price + sl_dist
        tp = price + tp_dist if side == "buy" else price - tp_dist

        balance = self._safe_call("broker.get_balance", self.broker.get_balance) or 0.0

        validation = self.risk_manager.validate_new_trade(
            symbol=symbol, side=side, entry_price=price,
            stop_loss=sl, take_profit=tp, balance=balance,
            open_positions=list(self.exit_manager.open_positions.values()),
        )
        if not validation.approved:
            logger.info("[RISK BLOCK] %s %s | %s", symbol, side, validation.reason)
            return

        lot = validation.lot_sizing.lot_size
        result = self._safe_call(
            "broker.open_market_order", self.broker.open_market_order,
            symbol=symbol, side=side, lot_size=lot, sl=sl, tp=tp, price=price,
        )
        if not result:
            return

        self.exit_manager.register_new_position(
            ticket_id=result["ticket_id"], symbol=symbol, side=side,
            entry_price=result.get("filled_price", price), sl=sl, tp=tp,
            lot_size=lot, strategy_name="ict_scored",
            risk_reward=abs(tp - price) / max(abs(price - sl), 1e-9),
        )
        # Store context on position for feedback loop
        pos = self.exit_manager.open_positions.get(result["ticket_id"])
        if pos:
            pos.metadata["filters_active"] = {
                s["strategy"]: s["score"]
                for s in (self._last_signals.get(symbol, {}).get("top_strategies") or [])
            }
            pos.metadata["signal_strength"] = (
                self._last_signals.get(symbol, {}).get("combined", {}).get("strength", 0.0)
            )
            self.persistence.save_open_position(pos)
        logger.info("[OPEN] %s %s lot=%s ticket=%s", side.upper(), symbol, lot,
                    result["ticket_id"])

    def _refresh_rankings(self) -> None:
        try:
            weights = self.feedback.get_detailed_weights() if self.feedback else []
        except Exception:
            weights = []
        rankings = sorted(
            [{
                "strategy": w.filter_name,
                "weight": round(w.weight, 3),
                "win_rate": round(w.win_rate * 100, 2),
                "avg_r": round(w.avg_r, 3),
                "trades": w.total_trades,
                "sample_ready": w.sample_ready,
            } for w in weights],
            key=lambda x: (x["weight"], x["avg_r"]),
            reverse=True,
        )
        with self._lock:
            self._strategy_rankings = rankings

    # ------------------------------------------------------------------
    # Read-only snapshots for the web API
    # ------------------------------------------------------------------
    def snapshot_stats(self) -> dict:
        balance = self._safe_call("broker.get_balance", self.broker.get_balance) or 0.0
        open_pos = list(self.exit_manager.open_positions.values())
        try:
            daily_ok, daily_pnl, daily_pct = self.risk_manager.check_daily_loss(balance)
            port_ok, port_pct = self.risk_manager.check_portfolio_risk(open_pos, balance)
        except Exception:
            daily_pnl, daily_pct = 0.0, 0.0
            port_pct = 0.0
        with self._lock:
            return {
                "developer": "Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)",
                "started_at": self._started_at.isoformat(),
                "uptime_sec": (datetime.now(timezone.utc) - self._started_at).total_seconds(),
                "balance": round(balance, 2),
                "open_positions": len(open_pos),
                "pending_orders": len(self.persistence.get_pending_orders())
                                  if hasattr(self.persistence, "get_pending_orders") else 0,
                "daily_pnl_usd": round(daily_pnl, 4),
                "daily_pnl_pct": round(daily_pct * 100, 3),
                "portfolio_risk_pct": round(port_pct * 100, 3),
                "cycles": self._cycle_count,
                "last_cycle_at": self._last_cycle_at.isoformat()
                                 if self._last_cycle_at else None,
                "last_cycle_duration_sec": round(self._last_cycle_duration, 3),
                "shutdown_requested": self._shutdown.is_set(),
                "errors_tail": self._errors[-5:],
            }

    def snapshot_open(self) -> List[dict]:
        out = []
        for p in self.exit_manager.open_positions.values():
            out.append({
                "ticket_id": p.ticket_id, "symbol": p.symbol, "side": p.side,
                "entry": p.entry_price, "current_sl": p.current_sl,
                "current_tp": p.current_tp, "lot": p.lot_size,
                "breakeven_done": p.breakeven_done,
                "trailing_active": p.trailing_active,
                "partial_tp1_done": p.partial_tp1_done,
                "open_time": p.open_time.isoformat() if p.open_time else None,
            })
        return out

    def snapshot_pending(self) -> List[dict]:
        try:
            rows = self.persistence.get_pending_orders() if hasattr(
                self.persistence, "get_pending_orders") else []
        except Exception:
            rows = []
        return [dict(r) for r in rows]

    def snapshot_closed(self, limit: int = 100) -> List[dict]:
        try:
            rows = self.persistence.get_closed_trades(limit=limit)
        except Exception:
            rows = []
        return [dict(r) for r in rows]

    def snapshot_rankings(self) -> List[dict]:
        with self._lock:
            return list(self._strategy_rankings)

    def snapshot_signals(self) -> Dict[str, dict]:
        with self._lock:
            return dict(self._last_signals)


# =============================================================================
#   FASTAPI APP FACTORY
# =============================================================================
DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Web Trading · Dashboard</title>
<style>
 body{font-family:system-ui,Segoe UI,sans-serif;margin:0;background:#0b0f17;color:#e6edf3}
 header{padding:16px 24px;background:#111827;border-bottom:1px solid #1f2937}
 h1{margin:0;font-size:18px} .credit{color:#22d3ee;font-size:12px;margin-top:4px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;padding:16px}
 .card{background:#111827;border:1px solid #1f2937;border-radius:8px;padding:12px}
 .k{color:#9ca3af;font-size:12px} .v{font-size:20px;font-weight:600;margin-top:4px}
 .tabs{display:flex;gap:8px;padding:0 16px;margin-top:8px}
 .tab{padding:8px 14px;background:#111827;border:1px solid #1f2937;border-radius:6px;cursor:pointer}
 .tab.active{background:#1f2937}
 table{width:calc(100% - 32px);margin:8px 16px;border-collapse:collapse;font-size:13px}
 th,td{padding:6px 8px;text-align:left;border-bottom:1px solid #1f2937}
 .pos{color:#22c55e} .neg{color:#ef4444}
</style>
</head>
<body>
<header>
  <h1>Web Trading · Live Dashboard</h1>
  <div class="credit">Developed &amp; Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)</div>
</header>
<div class="grid" id="stats"></div>
<div class="tabs">
  <div class="tab active" data-tab="open">Open</div>
  <div class="tab" data-tab="pending">Pending</div>
  <div class="tab" data-tab="closed">Closed</div>
</div>
<table id="tbl"><thead></thead><tbody></tbody></table>
<div class="grid" id="rankings" style="margin-top:24px"></div>
<script>
async function j(u){const r=await fetch(u);return await r.json();}
function row(r,cols){return '<tr>'+cols.map(c=>'<td>'+String(r[c]??'')+'</td>').join('')+'</tr>';}
async function refreshStats(){
 const s=await j('/api/stats');
 const items=[['Balance',s.balance],['Open',s.open_positions],['Pending',s.pending_orders],
  ['Daily PnL $',s.daily_pnl_usd],['Daily PnL %',s.daily_pnl_pct],
  ['Portfolio Risk %',s.portfolio_risk_pct],['Cycles',s.cycles]];
 document.getElementById('stats').innerHTML=items.map(([k,v])=>
  `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');
}
async function refreshTable(tab){
 let data=[],cols=[];
 if(tab==='open'){data=await j('/api/positions/open');
   cols=['ticket_id','symbol','side','entry','current_sl','current_tp','lot','breakeven_done','trailing_active'];}
 if(tab==='pending'){data=await j('/api/positions/pending');
   cols=['order_id','symbol','side','order_type','price','lot_size','sl','tp'];}
 if(tab==='closed'){data=await j('/api/positions/closed');
   cols=['ticket_id','symbol','side','entry_price','exit_price','realized_r','exit_reason','close_time'];}
 document.querySelector('#tbl thead').innerHTML='<tr>'+cols.map(c=>'<th>'+c+'</th>').join('')+'</tr>';
 document.querySelector('#tbl tbody').innerHTML=data.map(r=>row(r,cols)).join('');
}
async function refreshRankings(){
 const r=await j('/api/strategies/rankings');
 document.getElementById('rankings').innerHTML=r.slice(0,12).map(x=>
  `<div class="card"><div class="k">${x.strategy}</div>
   <div class="v">w=${x.weight}</div>
   <div class="k">win ${x.win_rate}% · avgR ${x.avg_r} · n=${x.trades}</div></div>`).join('');
}
document.querySelectorAll('.tab').forEach(t=>{
 t.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');refreshTable(t.dataset.tab);};
});
refreshStats();refreshTable('open');refreshRankings();
setInterval(refreshStats,3000);
setInterval(()=>refreshTable(document.querySelector('.tab.active').dataset.tab),3000);
setInterval(refreshRankings,10000);
</script>
</body>
</html>
"""


def create_app(state: TradingApplication) -> "FastAPI":
    if not HAS_WEB:
        raise RuntimeError("FastAPI/uvicorn not installed. `pip install fastapi uvicorn`")

    app = FastAPI(
        title="Web Trading Platform",
        version="1.0.0",
        description="Developed & Engineered by Engineer Abdulelah Sadiq "
                    "(المهندس عبد الإله صادق)",
    )

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

    @app.get("/api/stats")
    async def stats():
        return state.snapshot_stats()

    @app.get("/api/positions/open")
    async def positions_open():
        return state.snapshot_open()

    @app.get("/api/positions/pending")
    async def positions_pending():
        return state.snapshot_pending()

    @app.get("/api/positions/closed")
    async def positions_closed(limit: int = 100):
        return state.snapshot_closed(limit=limit)

    @app.get("/api/strategies/rankings")
    async def strategy_rankings():
        return state.snapshot_rankings()

    @app.get("/api/signals")
    async def signals():
        return state.snapshot_signals()

    @app.get("/api/news/indicators")
    async def news_indicators(impact: Optional[str] = None,
                              keyword: Optional[str] = None):
        events = state.news.get_events(impact=impact, keyword=keyword)
        return {
            "source": "forexfactory",
            "count": len(events),
            "cpi": state.news.cpi_events()[:20],
            "high_impact": state.news.high_impact()[:20],
            "events": events[:100],
        }

    @app.post("/api/shutdown")
    async def shutdown():
        state.request_shutdown()
        return {"status": "shutdown_requested"}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        return HTMLResponse(DASHBOARD_HTML)

    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                payload = {
                    "stats": state.snapshot_stats(),
                    "open": state.snapshot_open(),
                    "pending": state.snapshot_pending(),
                    "rankings": state.snapshot_rankings(),
                    "time": datetime.now(timezone.utc).isoformat(),
                }
                await ws.send_text(json.dumps(payload, default=str))
                await asyncio.sleep(2.0)
        except WebSocketDisconnect:
            return
        except Exception:
            return

    return app


# =============================================================================
#   ASYNC RUNTIME (trading thread + uvicorn)
# =============================================================================
async def run_runtime(state: TradingApplication, web_cfg: dict) -> None:
    host = web_cfg.get("host", "0.0.0.0")
    port = int(web_cfg.get("port", 8000))
    log_level = web_cfg.get("log_level", "info")

    # Start trading loop in a background thread
    loop = asyncio.get_running_loop()
    trading_future = loop.run_in_executor(None, state.run)

    if not HAS_WEB:
        logger.error("FastAPI not installed — running trading loop only (headless).")
        try:
            await trading_future
        except asyncio.CancelledError:
            pass
        return

    app = create_app(state)
    uv_config = uvicorn.Config(
        app=app, host=host, port=port, log_level=log_level, access_log=False,
    )
    uv_server = uvicorn.Server(uv_config)

    logger.info("Web dashboard: http://%s:%d", host if host != "0.0.0.0" else "127.0.0.1", port)

    try:
        await uv_server.serve()
    finally:
        state.request_shutdown()
        try:
            await asyncio.wait_for(trading_future, timeout=60)
        except asyncio.TimeoutError:
            logger.warning("Trading thread did not stop in time; forcing exit.")
        except Exception as e:
            logger.error("Runtime shutdown error: %s", e)


# =============================================================================
#   SIGNAL HANDLING
# =============================================================================
_APP_REF: Dict[str, Optional[TradingApplication]] = {"app": None}


def _signal_handler(signum, frame):
    logger.warning("Signal %s received — initiating graceful shutdown.", signum)
    if _APP_REF["app"] is not None:
        _APP_REF["app"].request_shutdown()


# =============================================================================
#   ENTRY POINT
# =============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Web Trading Platform — Developed & Engineered by "
                    "Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)"
    )
    parser.add_argument("--config", default="config/settings.yaml",
                        help="Path to settings.yaml")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--web-host", default=None)
    parser.add_argument("--web-port", default=None, type=int)
    parser.add_argument("--headless", action="store_true",
                        help="Run without web server")
    args = parser.parse_args()

    setup_logging(args.log_level)
    print(BANNER)
    logger.info("Booting Web Trading Platform v1.0")
    logger.info("Developed & Engineered by Engineer Abdulelah Sadiq "
                "(المهندس عبد الإله صادق)")

    try:
        config = load_config(args.config)
    except Exception as e:
        logger.critical("Cannot load config: %s", e)
        sys.exit(1)

    # Build application
    try:
        state = TradingApplication(config)
    except Exception as e:
        logger.critical("Failed to build TradingApplication: %s", e)
        raise
    _APP_REF["app"] = state

    # Attach signal handlers
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Web config overrides
    web_cfg = dict(config.get("web", {}) or {})
    if args.web_host:
        web_cfg["host"] = args.web_host
    if args.web_port:
        web_cfg["port"] = args.web_port
    if args.headless:
        HAS_EFFECTIVE_WEB = False
    else:
        HAS_EFFECTIVE_WEB = HAS_WEB

    logger.info("Startup complete. Entering runtime loop.")

    try:
        if HAS_EFFECTIVE_WEB:
            asyncio.run(run_runtime(state, web_cfg))
        else:
            # Headless: run trading loop directly (blocking)
            state.run()
    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt caught at top level.")
        state.request_shutdown()
    finally:
        _APP_REF["app"] = None
        logger.info("Platform stopped. Farewell.")
        logger.info("— Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)")


if __name__ == "__main__":
    main()
