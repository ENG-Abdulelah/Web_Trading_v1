
# app.py
"""
Web Trading Bot — Personal Trading Analyst (Flask)
Real market analysis, ICT×20, News guard (5/10), Fundamental Bias, TimeMachine,
Risk gates, Trailing/Breakeven/Partial TP1, Weekend flatten.
+ DXY, i18n (AR/EN), COT (filtered), CPI, Liquidity/Session snapshot.
+ Yahoo Finance price feed (memory-based, no external API 429).

Developed & Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)
"""
from __future__ import annotations

import logging
import math
import threading
import time
import uuid

from analysis_engine import SYMBOL_SPECS, analyze_symbol
from price_feed import PRICES, PRICE_LOCK, start_price_feed, build_calendar, get_news_blackout

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from flask import (
    Flask, jsonify, render_template, request,
    redirect, make_response,
)
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
# HEALTH CHECK (for Render / Gunicorn)
# =============================================================================
@app.route("/api/ping")
def api_ping():
    return "pong", 200


# =============================================================================
# GLOBALS
# =============================================================================
DB = Database()
GUARD = SessionGuard()
RISK = RiskManager(RiskConfig())
FUNDAMENTALS = get_fundamentals_engine()
TIMEMACHINE = TimeMachine(DB)

SYMBOLS = ["XAU/USD", "XAG/USD", "WTI/USD",
           "EUR/USD", "GBP/USD", "USD/JPY", "DXY"]

COT_VALID_MARKETS = [
    "GOLD", "SILVER", "CRUDE OIL",
    "EURO FX", "BRITISH POUND", "JAPANESE YEN",
]

SWING_TF = ["1D", "4H", "1H"]
SCALPING_TF = ["1H", "15M", "5M"]
USER_SETTINGS: Dict[str, Any] = {
    "capital": 50.0,
    "risk_per_trade_pct": 1.5,
    "min_score": 0.50,
    "auto_generate_signals": True,
    "analysis_interval_sec": 90,
    "mode": "single",
    "style_mode": "both",
    "timeframes": ["1H", "4H", "15M"],
    "last_cycle_stats": {},
}

_positions_lock = threading.RLock()

NEWS_BEFORE_MIN = 5
NEWS_AFTER_MIN = 10


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# =============================================================================
# LANGUAGE & I18N
# =============================================================================
TRANSLATIONS = {
    "ar": {
        "title": "محلل التداول",
        "market_open": "السوق مفتوح",
        "market_closed": "السوق مغلق",
        "live_prices": "الأسعار الحية",
        "balance": "الرصيد",
        "pending": "توصيات معلقة",
        "open_pos": "مراكز مفتوحة",
        "risk": "مخاطرة",
        "pnl": "P&L",
        "win_rate": "نجاح",
        "signals": "توصيات ICT",
        "analyze_now": "تحليل الآن",
        "no_signals": "لا توصيات حالياً — راجع لوحة التشخيص",
        "entry": "الدخول", "sl": "وقف الخسارة", "tp": "الهدف",
        "approve": "موافقة وتنفيذ", "reject": "رفض",
        "diagnostics": "لوحة التشخيص",
        "fundamental": "التحليل الأساسي",
        "news": "الأخبار اللحظية",
        "strategies": "أداء الاستراتيجيات",
        "settings": "الإعدادات",
        "save": "حفظ الإعدادات",
        "no_news": "لا توجد أخبار حديثة.",
        "no_strat": "لا توجد بيانات كافية بعد.",
        "session": "الجلسة",
        "liquidity": "السيولة",
        "cot": "تقارير COT",
        "cpi": "مؤشر أسعار المستهلك (CPI)",
        "no_cot": "لا تقارير COT متاحة.",
        "no_cpi": "لا بيانات CPI متاحة.",
    },
    "en": {
        "title": "Trading Analyst",
        "market_open": "Market Open",
        "market_closed": "Market Closed",
        "live_prices": "Live Prices",
        "balance": "Balance",
        "pending": "Pending",
        "open_pos": "Open Positions",
        "risk": "Risk",
        "pnl": "P&L",
        "win_rate": "Win Rate",
        "signals": "ICT Signals",
        "analyze_now": "Analyze Now",
        "no_signals": "No signals currently — check diagnostics",
        "entry": "Entry", "sl": "SL", "tp": "TP",
        "approve": "Approve & Execute", "reject": "Reject",
        "diagnostics": "Diagnostics",
        "fundamental": "Fundamental Bias",
        "news": "Live News",
        "strategies": "Strategy Performance",
        "settings": "Settings",
        "save": "Save Settings",
        "no_news": "No recent news.",
        "no_strat": "Not enough data yet.",
        "session": "Session",
        "liquidity": "Liquidity",
        "cot": "COT Reports",
        "cpi": "Consumer Price Index (CPI)",
        "no_cot": "No COT reports available.",
        "no_cpi": "No CPI data available.",
    }
}


def get_lang():
    return request.args.get("lang") or request.cookies.get("lang") or "ar"


@app.context_processor
def inject_lang():
    lang = get_lang()
    return dict(
        lang=lang,
        t=TRANSLATIONS.get(lang, TRANSLATIONS["ar"]),
        is_rtl=(lang == "ar"),
    )


@app.route("/set-lang/<l>")
def set_lang(l):
    if l not in ["ar", "en"]:
        l = "ar"
    resp = make_response(redirect(request.referrer or "/"))
    resp.set_cookie("lang", l, max_age=30 * 24 * 60 * 60)
    return resp


# =============================================================================
# LIVE PRICES — خفيف من الذاكرة (no network)
# =============================================================================
@app.route("/api/live_prices")
def api_live_prices():
    from price_feed import PRICES
    out = {}
    for sym, data in PRICES.items():
        out[sym] = {
            "price": data.get("price", 0.0),
            "change_pct": 0.0
        }
    return jsonify(out)


@app.route("/api/prices")
def api_prices():
    from price_feed import PRICES
    out = {}
    for sym, data in PRICES.items():
        out[sym] = {
            "price": data.get("price", 0.0),
            "change_pct": 0.0
        }
    return jsonify(out)


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

    news_blackout = get_news_blackout()
    news_event = None
    if news_blackout.get("active"):
        news_event = {
            "title": news_blackout["event_ar"],
            "currency": news_blackout["currency"],
            "event_time": news_blackout["when"],
        }

    if not state.is_open:
        liquidity, liq_color = "مغلقة", "danger"
    elif state.active_kill_zones:
        liquidity, liq_color = "قوية جداً", "success"
    elif getattr(state, "dead_liquidity", False):
        liquidity, liq_color = "ضعيفة", "danger"
    else:
        liquidity, liq_color = "متوسطة", "warning"

    session_name = (
        " + ".join([s["name_ar"] for s in state.active_sessions])
        if state.active_sessions else "لا توجد جلسة"
    )

    next_open_ts = None
    next_session = getattr(state, "next_session", None)
    if not state.is_open and next_session:
        try:
            in_minutes = (
                next_session.get("in_minutes")
                if isinstance(next_session, dict)
                else getattr(next_session, "in_minutes", 0)
            ) or 0
            next_open_ts = (utcnow() + timedelta(minutes=int(in_minutes))).isoformat()
        except Exception as e:
            logger.warning("next_open_ts calc failed: %s", e)

    is_weekend = bool(getattr(state, "is_weekend", False))

    return {
        **state.to_dict(),
        "news_blackout": news_event,
        "liquidity": liquidity,
        "liq_color": liq_color,
        "session_name": session_name,
        "next_open_ts": next_open_ts,
        "is_weekend": is_weekend,
    }


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

    news_check = get_news_blackout()
    news_event = (
        {"title": news_check.get("event_ar", "")}
        if news_check.get("active") else None
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

    biases = {}

    filters = DB.get_filter_weights()
    open_pos = DB.get_open_positions()
    closed_today = today_closed()
    consec = consecutive_losses()
    balance = get_balance()

    style_mode = USER_SETTINGS.get("style_mode", "both")

    valid_entry_tfs = {"1H", "4H", "15M", "5M", "1M"}
    user_tfs = [
        tf for tf in USER_SETTINGS.get("timeframes", [])
        if tf in valid_entry_tfs
    ]

    background_tfs = ["1W", "1D"]
    all_tfs_to_analyze = list(set(user_tfs + background_tfs))

    raw_setups: List[Dict[str, Any]] = []

    for symbol in SYMBOLS:
        if GUARD.is_instrument_closing(symbol):
            continue

        for tf in all_tfs_to_analyze:
            is_background = tf in background_tfs

            styles_to_run = []
            if tf in ("1H", "4H") and style_mode in ("both", "swing"):
                styles_to_run.append("Swing")
            if tf in ("15M", "5M", "1M") and style_mode in ("both", "scalping"):
                styles_to_run.append("Scalping")

            for style in styles_to_run:
                try:
                    res = analyze_symbol(symbol, tf, style, filters)
                    if res.get("ok"):
                        for s in res["setups"]:
                            s["score"] = round(
                                s["confidence"] * s["risk_reward"] / 3.0, 3,
                            )
                            if is_background:
                                continue
                            raw_setups.append(s)
                            stats["raw_setups"] += 1
                except Exception as e:
                    stats["errors"].append(
                        f"{symbol}@{tf}/{style}: {e}",
                    )

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

        fb = biases.get(s["symbol"])
        if fb is not None and abs(fb.bias) > 0.25:
            want_dir = "BUY" if fb.bias > 0 else "SELL"
            if s["direction"] != want_dir:
                stats["rejected"] += 1
                stats["rejections"]["fundamental_misalignment"] += 1
                continue
            s["confidence"] = min(1.0, s["confidence"] + 0.05)

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
# POSITION MANAGEMENT
# =============================================================================
def update_positions() -> None:
    from analysis_engine import CandleFetcher

    with _positions_lock:
        positions = DB.get_open_positions()
        if not positions:
            return

        fetcher = CandleFetcher()

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

    if side == "buy":
        pos["highest_price"] = max(
            float(pos["highest_price"] or 0.0), price,
        )
    else:
        lp = float(pos["lowest_price"] or 0.0)
        pos["lowest_price"] = min(lp, price) if lp > 0 else price

    if current_r >= 1.0 and not int(pos["tp1_done"]):
        if _execute_partial_tp1(pos, price):
            return
        pos["tp1_done"] = 1
        DB.save_open_position(pos)

    if current_r >= 1.0 and not int(pos["breakeven_done"]):
        pos["current_sl"] = entry
        pos["breakeven_done"] = 1
        logger.info("Breakeven: %s @ %.5f", pos["ticket_id"], entry)

    if current_r >= 1.5 and not int(pos["trailing_active"]):
        pos["trailing_active"] = 1
        logger.info("Trailing ON: %s @ %.2fR",
                    pos["ticket_id"], current_r)

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
    if exit_price is None:
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
# COT / CPI FETCHERS
# =============================================================================
def fetch_cot_reports() -> List[Any]:
    try:
        fetcher = getattr(FUNDAMENTALS, "cftc", None)
        if fetcher is None:
            logger.warning("FUNDAMENTALS.cftc غير موجود — تخطي COT")
            return []
        all_cot = fetcher.fetch() or []
        filtered: List[Any] = []
        for c in all_cot:
            market_name = (
                getattr(c, "market", None)
                if not isinstance(c, dict)
                else c.get("market")
            ) or ""
            if not market_name:
                continue
            up = str(market_name).upper()
            if any(v in up for v in COT_VALID_MARKETS):
                filtered.append(c)
        return filtered
    except Exception as e:
        logger.warning("COT fetch failed: %s", e)
        return []


def fetch_cpi_data() -> Optional[Any]:
    try:
        fetcher = getattr(FUNDAMENTALS, "cpi", None)
        if fetcher is None:
            logger.warning("FUNDAMENTALS.cpi غير موجود — تخطي CPI")
            return None
        return fetcher.fetch_latest()
    except Exception as e:
        logger.warning("CPI fetch failed: %s", e)
        return None


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
    from price_feed import PRICES
    # نمرر الأسعار مباشرة للواجهة
    live_prices = {sym: {"price": d.get("price", 0.0)} for sym, d in PRICES.items()}

    state = market_snapshot()
    pending = DB.get_pending_signals()
    open_pos = DB.get_open_positions()
    stats = DB.get_stats()
    strategy_stats = DB.get_filter_stats()

    return render_template(
        "index.html",
        market=state,
        pending_signals=pending,
        open_positions=open_pos,
        stats=stats,
        balance=get_balance(),
        open_risk_pct=0.0,
        strategy_stats=strategy_stats,
        fundamental_biases=DB.get_all_fundamental_bias(),
        news=build_calendar(),
        live_prices=live_prices,
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
        "news": build_calendar(),
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


@app.route("/api/market")
def api_market():
    return jsonify(market_snapshot())


@app.route("/api/news")
def api_news():
    return jsonify(build_calendar())


@app.route("/api/news/live")
def api_news_live():
    return jsonify(build_calendar())


@app.route("/api/strategies")
def api_strategies():
    return jsonify(DB.get_filter_stats())


@app.route("/api/fundamentals")
def api_fundamentals():
    try:
        return jsonify(FUNDAMENTALS.snapshot(SYMBOLS))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/cot")
def api_cot():
    return jsonify(fetch_cot_reports())


@app.route("/api/cpi")
def api_cpi():
    data = fetch_cpi_data()
    return jsonify(data) if data else jsonify({"ok": False, "error": "no_data"}), 200


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


@app.route("/api/diagnostics")
def api_diagnostics():
    return jsonify({
        "settings": USER_SETTINGS,
        "last_cycle": USER_SETTINGS.get("last_cycle_stats", {}),
        "market": market_snapshot(),
    })


@app.route("/api/news/refresh", methods=["POST"])
def api_news_refresh():
    return jsonify({"ok": True, "count": len(build_calendar())})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        for key in ("capital", "risk_per_trade_pct", "min_score",
                    "auto_generate_signals", "analysis_interval_sec",
                    "mode", "style_mode"):
            if key in data:
                try:
                    USER_SETTINGS[key] = type(USER_SETTINGS[key])(data[key])
                except (ValueError, TypeError):
                    pass
        if "timeframes" in data and isinstance(data["timeframes"], list):
            USER_SETTINGS["timeframes"] = [
                tf for tf in data["timeframes"]
                if tf in ("1H", "4H", "15M", "5M", "1M")
            ]
        _scheduler_stop.set()
        time.sleep(1.0)
        start_scheduler()
        return jsonify({"ok": True, "settings": USER_SETTINGS})
    return jsonify(USER_SETTINGS)


# =============================================================================
# BACKGROUND STARTUP (Gunicorn imports this module)
# =============================================================================
def _delayed_start_scheduler():
    import time as _t
    _t.sleep(8)
    try:
        start_scheduler()
        logger.info("Scheduler auto-started on import (delayed)")
    except Exception as e:
        logger.error("Failed to start scheduler on import: %s", e)


threading.Thread(target=_delayed_start_scheduler, daemon=True).start()


try:
    start_price_feed()
    logger.info("Price feed background thread started.")
except Exception as e:
    logger.error("Failed to start price feed: %s", e)

# لا تضع app.run() هنا لأن Gunicorn هو من سيشغل السيرفر
