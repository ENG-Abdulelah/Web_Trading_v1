# app.py
"""
Web Trading Bot — Personal Trading Analyst (Flask)
Real market analysis, ICT×20, News guard, Fundamental Bias, TimeMachine,
Risk gates, Trailing/Breakeven/Partial TP1, Weekend flatten.

Developed & Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)
"""
from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from analysis_engine import (
    NewsFetcher, SYMBOL_SPECS, analyze_symbol,
    get_live_prices, fetch_news_with_fallback,
)

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from flask import Flask, jsonify, render_template, request
from database import Database
from fundamentals import get_engine as get_fundamentals_engine
from risk_manager import RiskManager, RiskConfig
from session_guard import SessionGuard
from timemachine import TimeMachine

# =============================================================================
# SETUP
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)-15s | %(message)s",
)
logger = logging.getLogger("app")

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# =============================================================================
# GLOBALS
# =============================================================================
DB = Database()
GUARD = SessionGuard()
NEWS = NewsFetcher()
RISK = RiskManager(RiskConfig())
FUNDAMENTALS = get_fundamentals_engine()
TIMEMACHINE = TimeMachine(DB)

SYMBOLS = ["XAU/USD", "XAG/USD", "WTI/USD",
           "EUR/USD", "GBP/USD", "USD/JPY"]
SWING_TF = ["1D", "4H", "1H"]
SCALPING_TF = ["1H", "15M", "5M"]
USER_SETTINGS: Dict[str, Any] = {
    "capital": 50.0,
    "risk_per_trade_pct": 1.5,
    "min_score": 0.50,
    "auto_generate_signals": True,
    "analysis_interval_sec": 90,
    "mode": "single",
    # 👇 جديد
    "style_mode": "both",                              # "swing" | "scalping" | "both"
    "timeframes": ["1D", "4H", "1H", "15M"],            # multi-select
    "last_cycle_stats": {},                            # populated each cycle
}

# thread safety for position management
_positions_lock = threading.RLock()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# =============================================================================
# STATE HELPERS
# =============================================================================
def get_balance() -> float:
    stats = DB.get_stats()
    return round(USER_SETTINGS["capital"] + stats["closed_pnl"], 2)


def consecutive_losses() -> int:
    closed = DB.get_closed_trades(limit=20)
    n = 0
    for t in closed:
        if float(t["realized_r"]) < 0:
            n += 1
        else:
            break
    return n


def today_closed() -> List[Dict[str, Any]]:
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    closed = DB.get_closed_trades(limit=200)
    return [t for t in closed
            if datetime.fromisoformat(t["close_time"]) >= today]


def market_snapshot() -> Dict[str, Any]:
    state = GUARD.state()
    news_blackout = NEWS.high_impact_within(
        minutes_before=30, minutes_after=10,
    )
    return {**state.to_dict(), "news_blackout": news_blackout}


# =============================================================================
# SIGNAL GENERATION
# =============================================================================
 # =============================================================================
# SIGNAL GENERATION (with diagnostics)
# =============================================================================
SWING_TFS_SET = {"1W", "1D", "4H"}
SCALPING_TFS_SET = {"1H", "15M", "5M"}


def run_analysis_cycle() -> Dict[str, Any]:
    started = utcnow()
    stats = {
        "started_at": started.isoformat(),
        "generated": 0,
        "rejected": 0,
        "blocked_by_market": False,
        "blocked_reason": "",
        "raw_setups": 0,
        "rejections": {
            "low_score": 0,
            "fundamental_misalignment": 0,
            "risk_gate": 0,
            "timemachine_weak": 0,
            "duplicate_symbol_dir": 0,
            "invalid_sltp": 0,
        },
        "errors": [],
    }

    DB.expire_old_signals(max_age_minutes=90)

    state = GUARD.state()
    if not state.is_open:
        DB.save_market_snapshot({
            "is_open": False,
            "sessions": [s["name"] for s in state.active_sessions],
            "kill_zones": [k["name"] for k in state.active_kill_zones],
            "blocked_reason": state.blocked_reason,
            "news_blackout": 0,
        })
        stats["blocked_by_market"] = True
        stats["blocked_reason"] = state.blocked_reason
        USER_SETTINGS["last_cycle_stats"] = stats
        return stats

    news_event = NEWS.high_impact_within(
        minutes_before=30, minutes_after=10,
    )
    if news_event:
        DB.save_market_snapshot({
            "is_open": True,
            "sessions": [s["name"] for s in state.active_sessions],
            "kill_zones": [k["name"] for k in state.active_kill_zones],
            "blocked_reason": f"News blackout: {news_event['title']}",
            "news_blackout": 1,
        })
        stats["blocked_by_market"] = True
        stats["blocked_reason"] = f"News blackout: {news_event['title']}"
        USER_SETTINGS["last_cycle_stats"] = stats
        return stats

    # Refresh news
    try:
        events = fetch_news_with_fallback()
        if events:
            DB.replace_news_cache(events)
    except Exception as e:
        logger.warning("news cache refresh failed: %s", e)

    # Fundamental Bias
    try:
        biases = FUNDAMENTALS.bias_for_all(SYMBOLS)
        for sym, b in biases.items():
            DB.save_fundamental_bias(
                sym, b.bias, b.confidence, b.components, b.reason,
            )
    except Exception as e:
        logger.warning("fundamentals bias failed: %s", e)
        biases = {}

    filters = DB.get_filter_weights()
    open_pos = DB.get_open_positions()
    closed_today = today_closed()
    consec = consecutive_losses()
    balance = get_balance()

    # 👇 User-selected settings
    style_mode = USER_SETTINGS.get("style_mode", "both")
    user_tfs = USER_SETTINGS.get("timeframes", ["1D", "4H", "1H"])

    raw_setups: List[Dict[str, Any]] = []

    for symbol in SYMBOLS:
        if GUARD.is_instrument_closing(symbol):
            continue

        for tf in user_tfs:
            styles_to_run = []
            # Determine style for this TF
            if tf in SWING_TFS_SET and style_mode in ("both", "swing"):
                styles_to_run.append("Swing")
            if tf in SCALPING_TFS_SET and style_mode in ("both", "scalping"):
                if tf != "1H" or style_mode == "scalping" or state.can_trade_scalping:
                    styles_to_run.append("Scalping")

            for style in styles_to_run:
                try:
                    res = analyze_symbol(symbol, tf, style, filters)
                    if res.get("ok"):
                        for s in res["setups"]:
                            s["score"] = round(
                                s["confidence"] * s["risk_reward"] / 3.0, 3,
                            )
                            raw_setups.append(s)
                            stats["raw_setups"] += 1
                except Exception as e:
                    stats["errors"].append(f"{symbol}@{tf}/{style}: {e}")

    raw_setups.sort(key=lambda s: s["score"], reverse=True)

    seen: set = set()
    for s in raw_setups:
        key = (s["symbol"], s["direction"])
        if key in seen:
            stats["rejected"] += 1
            stats["rejections"]["duplicate_symbol_dir"] += 1
            continue

        if s["score"] < USER_SETTINGS["min_score"]:
            stats["rejected"] += 1
            stats["rejections"]["low_score"] += 1
            continue
        seen.add(key)

        # Fundamental alignment
        fb = biases.get(s["symbol"])
        if fb is not None and abs(fb.bias) > 0.25:
            want_dir = "BUY" if fb.bias > 0 else "SELL"
            if s["direction"] != want_dir:
                stats["rejected"] += 1
                stats["rejections"]["fundamental_misalignment"] += 1
                continue
            s["confidence"] = min(1.0, s["confidence"] + 0.05)

        # Risk gate
        decision = RISK.validate_trade(
            symbol=s["symbol"], direction=s["direction"],
            entry=s["entry"], sl=s["stop_loss"], tp=s["take_profit"],
            balance=balance, open_positions=open_pos,
            closed_trades_today=closed_today,
            consecutive_losses=consec,
        )
        if not decision.approved:
            stats["rejected"] += 1
            stats["rejections"]["risk_gate"] += 1
            s["reject_reason"] = decision.reason
            continue

        # TimeMachine gate
        tm_result = TIMEMACHINE.simulate(
            strategy=s["strategy"], runs=10_000, trades_per_run=20,
        )
        if tm_result.verdict == "WEAK":
            stats["rejected"] += 1
            stats["rejections"]["timemachine_weak"] += 1
            continue

        s["meta"] = {
            "lot_size": decision.lot_size,
            "risk_usd": decision.risk_usd,
            "risk_pct": decision.risk_pct,
            "reward_usd": decision.reward_usd,
            "rr": decision.rr,
            "checks": decision.checks,
            "fundamental_bias": fb.to_dict() if fb else None,
            "timemachine": tm_result.to_dict(),
        }
        try:
            DB.insert_signal(s)
            DB.save_timemachine(s["signal_uid"], tm_result.to_dict())
            stats["generated"] += 1
        except Exception as e:
            stats["errors"].append(f"insert {s['symbol']}: {e}")

    stats["finished_at"] = utcnow().isoformat()
    USER_SETTINGS["last_cycle_stats"] = stats
    return stats 


# =============================================================================
# POSITION MANAGEMENT (Trailing + Breakeven + Partial TP1 + Weekend Flatten)
# =============================================================================
def update_positions() -> None:
    from analysis_engine import CandleFetcher

    with _positions_lock:
        positions = DB.get_open_positions()
        if not positions:
            return

        fetcher = CandleFetcher()

        # ---- Weekend flatten check ----
        if GUARD.should_flatten_positions():
            logger.warning(
                "Weekend flatten triggered — closing %d positions",
                len(positions),
            )
            for pos in positions:
                try:
                    _close_position_now(pos, fetcher, reason="weekend_flatten")
                except Exception as e:
                    logger.error("flatten failed for %s: %s",
                                 pos.get("ticket_id"), e)
            return

        # ---- Per-position management ----
        for pos in positions:
            try:
                _manage_position(pos, fetcher)
            except Exception as e:
                logger.exception("manage failed for %s: %s",
                                 pos.get("ticket_id"), e)


def _manage_position(pos: Dict[str, Any], fetcher) -> None:
    df = (fetcher.get(pos["symbol"], "5M")
          or fetcher.get(pos["symbol"], "1H"))
    if df is None or df.empty:
        return
    price = float(df["close"].iloc[-1])
    side = pos["side"]
    entry = float(pos["entry_price"])
    initial_sl = float(pos["initial_sl"])
    risk_per_unit = abs(entry - initial_sl)
    if risk_per_unit <= 1e-9:
        return

    if side == "buy":
        current_r = (price - entry) / risk_per_unit
    else:
        current_r = (entry - price) / risk_per_unit

    # Track high/low
    if side == "buy":
        pos["highest_price"] = max(
            float(pos["highest_price"] or 0.0), price,
        )
    else:
        lp = float(pos["lowest_price"] or 0.0)
        pos["lowest_price"] = min(lp, price) if lp > 0 else price

    # Partial TP1 at +1R
    if current_r >= 1.0 and not int(pos["tp1_done"]):
        if _execute_partial_tp1(pos, price):
            return  # fully closed
        pos["tp1_done"] = 1
        DB.save_open_position(pos)

    # Breakeven at +1R
    if current_r >= 1.0 and not int(pos["breakeven_done"]):
        pos["current_sl"] = entry
        pos["breakeven_done"] = 1
        logger.info("Breakeven: %s @ %.5f", pos["ticket_id"], entry)

    # Trailing activate at +1.5R
    if current_r >= 1.5 and not int(pos["trailing_active"]):
        pos["trailing_active"] = 1
        logger.info("Trailing ON: %s @ %.2fR",
                    pos["ticket_id"], current_r)

    # Update trailing SL
    if int(pos["trailing_active"]):
        trail_dist = 1.0 * risk_per_unit
        if side == "buy":
            new_sl = float(pos["highest_price"]) - trail_dist
            if new_sl > float(pos["current_sl"]):
                pos["current_sl"] = new_sl
        else:
            new_sl = float(pos["lowest_price"]) + trail_dist
            if new_sl < float(pos["current_sl"]):
                pos["current_sl"] = new_sl

    # Check SL/TP hit
    sl = float(pos["current_sl"])
    tp = float(pos["current_tp"])
    hit = None
    exit_px = price

    if side == "buy":
        if price <= sl:
            hit, exit_px = "stop_loss", sl
        elif price >= tp:
            hit, exit_px = "take_profit", tp
    else:
        if price >= sl:
            hit, exit_px = "stop_loss", sl
        elif price <= tp:
            hit, exit_px = "take_profit", tp

    if hit:
        _close_position_now(pos, fetcher, reason=hit, exit_price=exit_px)
    else:
        DB.save_open_position(pos)


def _execute_partial_tp1(pos: Dict[str, Any], price: float) -> bool:
    """
    Close 50% of lot at +1R.
    Returns True if position is fully closed (remaining lot == 0).
    """
    total_lot = float(pos["lot_size"])
    partial_lot = round(total_lot * 0.5, 3)
    if partial_lot <= 0 or partial_lot >= total_lot:
        return False

    entry = float(pos["entry_price"])
    side = pos["side"]
    spec = SYMBOL_SPECS.get(pos["symbol"], {})
    pip_value = spec.get("pip_value_per_lot", 10.0)
    pip_size = spec.get("pip_size", 0.0001)

    pips = abs(price - entry) / pip_size
    pnl_usd = pips * pip_value * partial_lot
    if (side == "buy" and price < entry) or \
            (side == "sell" and price > entry):
        pnl_usd = -pnl_usd

    DB.save_partial_tp(pos["ticket_id"], partial_lot, price, pnl_usd)

    remaining = round(total_lot - partial_lot, 3)
    if remaining <= 0.001:
        _close_position_now(pos, None, reason="tp1_full_close",
                             exit_price=price)
        return True

    pos["lot_size"] = remaining
    pos["partial_pnl_usd"] = (
        float(pos.get("partial_pnl_usd", 0.0)) + pnl_usd
    )
    logger.info("Partial TP1: %s closed %.3f lot @ %.5f ($%.2f)",
                pos["ticket_id"], partial_lot, price, pnl_usd)
    return False


def _close_position_now(pos: Dict[str, Any], fetcher,
                         reason: str,
                         exit_price: Optional[float] = None) -> None:
    """Close the full position; realizes PnL and clears from DB."""
    if exit_price is None:
        # Fetch market price (fallback to current_sl if unavailable)
        if fetcher is not None:
            df = (fetcher.get(pos["symbol"], "5M")
                  or fetcher.get(pos["symbol"], "1H"))
            if df is not None and not df.empty:
                exit_price = float(df["close"].iloc[-1])
        if exit_price is None:
            exit_price = float(pos["current_sl"])
            logger.warning(
                "No price available for %s — closing at SL %.5f",
                pos["ticket_id"], exit_price,
            )

    entry = float(pos["entry_price"])
    side = pos["side"]
    initial_sl = float(pos["initial_sl"])
    risk_per_unit = abs(entry - initial_sl)
    if risk_per_unit <= 1e-9:
        return

    if side == "buy":
        realized_r = (exit_price - entry) / risk_per_unit
    else:
        realized_r = (entry - exit_price) / risk_per_unit

    spec = SYMBOL_SPECS.get(pos["symbol"], {})
    pip_value = spec.get("pip_value_per_lot", 10.0)
    pip_size = spec.get("pip_size", 0.0001)
    pips = abs(exit_price - entry) / pip_size
    pnl_usd = pips * pip_value * float(pos["lot_size"])
    if realized_r < 0:
        pnl_usd = -pnl_usd

    # Include previously-realized partial PnL
    pnl_usd += float(pos.get("partial_pnl_usd", 0.0))

    DB.close_trade({
        "ticket_id": pos["ticket_id"],
        "signal_uid": pos.get("signal_uid"),
        "symbol": pos["symbol"],
        "side": side,
        "entry_price": entry,
        "exit_price": exit_price,
        "initial_sl": initial_sl,
        "initial_tp": float(pos["initial_tp"]),
        "lot_size": float(pos["initial_lot_size"] or pos["lot_size"]),
        "open_time": pos["open_time"],
        "realized_r": round(realized_r, 3),
        "realized_pnl_usd": round(pnl_usd, 2),
        "exit_reason": reason,
        "strategy": pos["strategy"],
        "meta": {
            "partial_pnl": float(pos.get("partial_pnl_usd", 0.0)),
        },
    })
    DB.delete_open_position(pos["ticket_id"])
    DB.log_feedback({
        "ticket_id": pos["ticket_id"],
        "strategy": pos["strategy"],
        "symbol": pos["symbol"],
        "side": side,
        "realized_r": realized_r,
        "exit_reason": reason,
        "filters": pos.get("meta", {}).get("checks", {}),
    })
    _update_filter_weight(pos["strategy"], realized_r)
    logger.info("Closed %s (%s): %.2fR ($%.2f)",
                pos["symbol"], reason, realized_r, pnl_usd)


def _update_filter_weight(strategy: str, realized_r: float) -> None:
    """Adaptive EMA-style weight update based on rolling performance."""
    stats = {s["strategy"]: s for s in DB.get_filter_stats()}
    s = stats.get(strategy, {
        "total_trades": 0, "wins": 0, "losses": 0, "total_r": 0.0,
    })
    total = int(s["total_trades"]) + 1
    wins = int(s["wins"]) + (1 if realized_r > 0 else 0)
    losses = int(s["losses"]) + (1 if realized_r <= 0 else 0)
    total_r = float(s["total_r"]) + realized_r

    avg_r = total_r / total if total else 0.0
    w = 1.0 + math.tanh(avg_r / 2.0) * 0.5
    w = max(0.5, min(1.5, w))
    DB.upsert_filter_weight(strategy, round(w, 3),
                             total, wins, losses, total_r)


# =============================================================================
# BACKGROUND SCHEDULER
# =============================================================================
_scheduler_lock = threading.Lock()
_scheduler_thread: Optional[threading.Thread] = None
_scheduler_stop = threading.Event()


def _scheduler_loop() -> None:
    logger.info("Scheduler started (interval=%ss)",
                USER_SETTINGS["analysis_interval_sec"])
    while not _scheduler_stop.is_set():
        try:
            if USER_SETTINGS["auto_generate_signals"]:
                res = run_analysis_cycle()
                logger.info(
                    "Cycle: generated=%s rejected=%s blocked=%s",
                    res["generated"], res["rejected"],
                    res["blocked_by_market"],
                )
            try:
                update_positions()
            except Exception as e:
                logger.exception("update_positions error: %s", e)
        except Exception as e:
            logger.exception("scheduler error: %s", e)

        for _ in range(int(USER_SETTINGS["analysis_interval_sec"])):
            if _scheduler_stop.is_set():
                break
            time.sleep(1.0)
    logger.info("Scheduler stopped")


def start_scheduler() -> None:
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return
        _scheduler_stop.clear()
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop, daemon=True,
        )
        _scheduler_thread.start()


# =============================================================================
# ROUTES
# =============================================================================
@app.route("/")
def index():
    update_positions()

    state = market_snapshot()
    pending = DB.get_pending_signals()
    open_pos = DB.get_open_positions()
    closed = DB.get_closed_trades(limit=50)
    stats = DB.get_stats()
    strategy_stats = DB.get_filter_stats()
    news = DB.get_recent_news(hours=24)

    fundamental_biases = DB.get_all_fundamental_bias()
    timemachine_cache = {
        s["signal_uid"]: DB.get_timemachine(s["signal_uid"])
        for s in pending
    }

    balance = get_balance()
    open_risk_pct = RISK._portfolio_risk_pct(open_pos, balance)

    return render_template(
        "index.html",
        market=state,
        pending_signals=pending,
        open_positions=open_pos,
        closed_trades=closed,
        stats=stats,
        balance=balance,
        open_risk_pct=round(open_risk_pct, 2),
        strategy_stats=strategy_stats,
        news=news,
        fundamental_biases=fundamental_biases,
        timemachine_cache=timemachine_cache,
        settings=USER_SETTINGS,
        symbols=SYMBOLS,
        now=utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    )


@app.route("/api/state")
def api_state():
    update_positions()
    state = market_snapshot()
    return jsonify({
        "market": state,
        "pending": DB.get_pending_signals(),
        "open": DB.get_open_positions(),
        "closed": DB.get_closed_trades(limit=30),
        "stats": DB.get_stats(),
        "balance": get_balance(),
        "news": DB.get_recent_news(hours=6),
        "now": utcnow().isoformat(),
    })


@app.route("/api/approve/<signal_uid>", methods=["POST"])
def api_approve(signal_uid: str):
    sig = DB.get_signal(signal_uid)
    if not sig or sig["status"] != "pending":
        return jsonify({"ok": False, "error": "signal_not_pending"}), 400

    decision = RISK.validate_trade(
        symbol=sig["symbol"],
        direction=sig["direction"],
        entry=float(sig["entry"]),
        sl=float(sig["stop_loss"]),
        tp=float(sig["take_profit"]),
        balance=get_balance(),
        open_positions=DB.get_open_positions(),
        closed_trades_today=today_closed(),
        consecutive_losses=consecutive_losses(),
    )
    if not decision.approved:
        return jsonify({"ok": False, "error": decision.reason}), 400

    ticket = f"T-{uuid.uuid4().hex[:8].upper()}"
    DB.save_open_position({
        "ticket_id": ticket,
        "signal_uid": signal_uid,
        "symbol": sig["symbol"],
        "side": sig["direction"].lower(),
        "entry_price": float(sig["entry"]),
        "current_sl": float(sig["stop_loss"]),
        "current_tp": float(sig["take_profit"]),
        "initial_sl": float(sig["stop_loss"]),
        "initial_tp": float(sig["take_profit"]),
        "lot_size": decision.lot_size,
        "initial_lot_size": decision.lot_size,
        "risk_usd": decision.risk_usd,
        "open_time": utcnow().isoformat(),
        "strategy": sig["strategy"],
        "highest_price": float(sig["entry"]),
        "lowest_price": float(sig["entry"]),
        "meta": {
            "score": sig["score"],
            "confidence": sig["confidence"],
            "checks": decision.checks,
        },
    })
    DB.update_signal_status(signal_uid, "approved")
    logger.info("Approved %s: %s %s lot=%s ticket=%s",
                sig["symbol"], sig["direction"], sig["strategy"],
                decision.lot_size, ticket)
    return jsonify({
        "ok": True, "ticket": ticket, "lot": decision.lot_size,
    })


@app.route("/api/reject/<signal_uid>", methods=["POST"])
def api_reject(signal_uid: str):
    sig = DB.get_signal(signal_uid)
    if not sig:
        return jsonify({"ok": False, "error": "not_found"}), 404
    DB.update_signal_status(signal_uid, "rejected")
    return jsonify({"ok": True})


@app.route("/api/run-analysis", methods=["POST"])
def api_run_analysis():
    res = run_analysis_cycle()
    return jsonify(res)


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        for key in ("capital", "risk_per_trade_pct", "min_score",
                    "auto_generate_signals", "analysis_interval_sec",
                    "mode"):
            if key in data:
                try:
                    USER_SETTINGS[key] = type(USER_SETTINGS[key])(data[key])
                except (ValueError, TypeError):
                    pass
        _scheduler_stop.set()
        time.sleep(1.0)
        start_scheduler()
        return jsonify({"ok": True, "settings": USER_SETTINGS})
    return jsonify(USER_SETTINGS)


@app.route("/api/market")
def api_market():
    return jsonify(market_snapshot())


@app.route("/api/news")
def api_news():
    return jsonify(DB.get_recent_news(hours=48))


@app.route("/api/strategies")
def api_strategies():
    return jsonify(DB.get_filter_stats())


@app.route("/api/fundamentals")
def api_fundamentals():
    try:
        return jsonify(FUNDAMENTALS.snapshot(SYMBOLS))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/timemachine/<signal_uid>")
def api_timemachine(signal_uid: str):
    sig = DB.get_signal(signal_uid)
    if not sig:
        return jsonify({"ok": False, "error": "not_found"}), 404

    cached = DB.get_timemachine(signal_uid)
    if cached:
        return jsonify({"ok": True, "cached": True, "result": cached})

    result = TIMEMACHINE.simulate(
        strategy=sig["strategy"], runs=10_000, trades_per_run=20,
    )
    DB.save_timemachine(signal_uid, result.to_dict())
    return jsonify({
        "ok": True, "cached": False, "result": result.to_dict(),
    })


@app.route("/api/ping")
def api_ping():
    """Lightweight keep-alive endpoint for external cron."""
    return jsonify({"ok": True, "t": utcnow().isoformat()})


# =============================================================================
# BOOT
# =============================================================================
if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Web Trading Bot — Personal Trading Analyst")
    logger.info("Developed & Engineered by Engineer Abdulelah Sadiq")
    logger.info("             (المهندس عبد الإله صادق)")
    logger.info("=" * 60)

    st = GUARD.state()
    logger.info(
        "Market open=%s sessions=%s kill_zones=%s",
        st.is_open,
        [s["name"] for s in st.active_sessions],
        [k["name"] for k in st.active_kill_zones],
    )

    start_scheduler()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)


# Start scheduler when gunicorn imports the app (production)
# Delayed by 8s so gunicorn can boot cleanly first
def _delayed_start_scheduler():
    import time as _t
    _t.sleep(8)
    try:
        start_scheduler()
        logger.info("Scheduler auto-started on import (delayed)")
    except Exception as e:
        logger.error("Failed to start scheduler on import: %s", e)


threading.Thread(target=_delayed_start_scheduler, daemon=True).start()

