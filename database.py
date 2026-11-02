# database.py
"""
Persistence layer for Web Trading Bot (Personal Trading Analyst).
Thread-safe SQLite wrapper (RLock + WAL + busy_timeout).

Developed & Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)
"""
from __future__ import annotations

import os
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("DB_PATH", "data/trading.db"))
#DB_PATH = Path("data/trading.db")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or utcnow()).isoformat()


class Database:
    """Thread-safe SQLite wrapper."""

    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute("PRAGMA busy_timeout=30000;")
        self._create_tables()
        logger.info("Database ready at %s (thread-safe)", self.db_path)

    # ==================================================================
    # SCHEMA
    # ==================================================================
    def _create_tables(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_uid TEXT UNIQUE NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    style TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    entry REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    confidence REAL NOT NULL,
                    risk_reward REAL NOT NULL,
                    score REAL NOT NULL,
                    reason TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    approved_at TEXT,
                    approved_by TEXT,
                    meta TEXT DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
                CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);

                CREATE TABLE IF NOT EXISTS open_positions (
                    ticket_id TEXT PRIMARY KEY,
                    signal_uid TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    current_sl REAL NOT NULL,
                    current_tp REAL NOT NULL,
                    initial_sl REAL NOT NULL,
                    initial_tp REAL NOT NULL,
                    lot_size REAL NOT NULL,
                    initial_lot_size REAL NOT NULL DEFAULT 0,
                    risk_usd REAL NOT NULL,
                    open_time TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    breakeven_done INTEGER DEFAULT 0,
                    tp1_done INTEGER DEFAULT 0,
                    trailing_active INTEGER DEFAULT 0,
                    highest_price REAL DEFAULT 0,
                    lowest_price REAL DEFAULT 0,
                    partial_pnl_usd REAL DEFAULT 0,
                    meta TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS partial_tp_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL,
                    lot_size REAL NOT NULL,
                    price REAL NOT NULL,
                    pnl_usd REAL NOT NULL,
                    filled_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_partial_tp_ticket
                    ON partial_tp_fills(ticket_id);

                CREATE TABLE IF NOT EXISTS closed_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL,
                    signal_uid TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    initial_sl REAL NOT NULL,
                    initial_tp REAL NOT NULL,
                    lot_size REAL NOT NULL,
                    open_time TEXT NOT NULL,
                    close_time TEXT NOT NULL,
                    realized_r REAL NOT NULL,
                    realized_pnl_usd REAL NOT NULL,
                    exit_reason TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    meta TEXT DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_closed_close_time
                    ON closed_trades(close_time);
                CREATE INDEX IF NOT EXISTS idx_closed_strategy
                    ON closed_trades(strategy);

                CREATE TABLE IF NOT EXISTS news_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    title TEXT NOT NULL,
                    currency TEXT,
                    impact TEXT,
                    event_time TEXT NOT NULL,
                    forecast TEXT,
                    previous TEXT,
                    actual TEXT,
                    fetched_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_at TEXT NOT NULL,
                    is_open INTEGER NOT NULL,
                    sessions TEXT,
                    kill_zones TEXT,
                    blocked_reason TEXT,
                    news_blackout INTEGER DEFAULT 0,
                    meta TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS filter_weights (
                    strategy TEXT PRIMARY KEY,
                    weight REAL DEFAULT 1.0,
                    total_trades INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    total_r REAL DEFAULT 0,
                    last_updated TEXT
                );

                CREATE TABLE IF NOT EXISTS feedback_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    realized_r REAL NOT NULL,
                    exit_reason TEXT NOT NULL,
                    filters TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS fundamental_bias_cache (
                    symbol TEXT PRIMARY KEY,
                    bias REAL NOT NULL,
                    confidence REAL NOT NULL,
                    components TEXT DEFAULT '{}',
                    reason TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS timemachine_cache (
                    signal_uid TEXT PRIMARY KEY,
                    runs INTEGER NOT NULL,
                    win_rate REAL NOT NULL,
                    expectancy_r REAL NOT NULL,
                    profit_factor REAL NOT NULL,
                    max_dd_p95 REAL NOT NULL,
                    risk_of_ruin REAL NOT NULL,
                    verdict TEXT NOT NULL,
                    computed_at TEXT NOT NULL
                );
            """)

        # ✅ بذر الـ 20 استراتيجية بوزن افتراضي 1.0 عند التشغيل الأول
        self._seed_strategies()

    def _seed_strategies(self):
        """تسجيل الـ 20 استراتيجية بوزن افتراضي 1.0 عند التشغيل الأول"""
        strategies = [
            "Order Blocks", "Fair Value Gap", "Liquidity Sweep", "Market Structure Shift",
            "Break of Structure", "Kill Zones", "Judas Swing", "Balanced Price Range",
            "Breaker Block", "Mitigation Block", "Silver Bullet", "Optimal Trade Entry",
            "Inversion FVG", "Volume Gap", "Inducement", "Power of 3 (AMD)",
            "Institutional Order Flow", "Premium/Discount", "Quarterly Theory", "Daily Bias"
        ]
        with self._lock, self._conn:
            for s in strategies:
                self._conn.execute("""
                    INSERT OR IGNORE INTO filter_weights (strategy, weight, total_trades, wins, losses, total_r)
                    VALUES (?, 1.0, 0, 0, 0, 0.0)
                """, (s,))

    # ==================================================================
    # SIGNALS
    # ==================================================================
    def insert_signal(self, sig: Dict[str, Any]) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute("""
                INSERT OR IGNORE INTO signals
                (signal_uid, symbol, strategy, direction, style, timeframe,
                 entry, stop_loss, take_profit, confidence, risk_reward, score,
                 reason, status, created_at, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sig["signal_uid"], sig["symbol"], sig["strategy"],
                sig["direction"], sig["style"], sig["timeframe"],
                float(sig["entry"]), float(sig["stop_loss"]),
                float(sig["take_profit"]), float(sig["confidence"]),
                float(sig["risk_reward"]), float(sig["score"]),
                sig.get("reason", ""), sig.get("status", "pending"),
                sig.get("created_at", _iso()),
                json.dumps(sig.get("meta", {})),
            ))
            return cur.lastrowid or 0

    def get_pending_signals(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM signals WHERE status = 'pending' "
                "ORDER BY score DESC, created_at DESC LIMIT 100"
            )
            return [dict(r) for r in cur.fetchall()]

    def get_signal(self, signal_uid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM signals WHERE signal_uid = ?", (signal_uid,)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def update_signal_status(
        self, signal_uid: str, status: str, approved_by: str = "user"
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute("""
                UPDATE signals
                SET status = ?, approved_at = ?, approved_by = ?
                WHERE signal_uid = ?
            """, (status, _iso(), approved_by, signal_uid))

    def expire_old_signals(self, max_age_minutes: int = 60) -> int:
        cutoff = (utcnow() - timedelta(minutes=max_age_minutes)).isoformat()
        with self._lock, self._conn:
            cur = self._conn.execute("""
                UPDATE signals SET status = 'expired'
                WHERE status = 'pending' AND created_at < ?
            """, (cutoff,))
            return cur.rowcount or 0

    # ==================================================================
    # OPEN POSITIONS
    # ==================================================================
    def save_open_position(self, pos: Dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO open_positions
                (ticket_id, signal_uid, symbol, side, entry_price,
                 current_sl, current_tp, initial_sl, initial_tp,
                 lot_size, initial_lot_size, risk_usd, open_time, strategy,
                 breakeven_done, tp1_done, trailing_active,
                 highest_price, lowest_price, partial_pnl_usd, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET
                    current_sl=excluded.current_sl,
                    current_tp=excluded.current_tp,
                    lot_size=excluded.lot_size,
                    breakeven_done=excluded.breakeven_done,
                    tp1_done=excluded.tp1_done,
                    trailing_active=excluded.trailing_active,
                    highest_price=excluded.highest_price,
                    lowest_price=excluded.lowest_price,
                    partial_pnl_usd=excluded.partial_pnl_usd,
                    meta=excluded.meta
            """, (
                pos["ticket_id"], pos.get("signal_uid"), pos["symbol"],
                pos["side"], float(pos["entry_price"]),
                float(pos["current_sl"]), float(pos["current_tp"]),
                float(pos["initial_sl"]), float(pos["initial_tp"]),
                float(pos["lot_size"]),
                float(pos.get("initial_lot_size") or pos["lot_size"]),
                float(pos["risk_usd"]), pos["open_time"], pos["strategy"],
                int(pos.get("breakeven_done", 0)),
                int(pos.get("tp1_done", 0)),
                int(pos.get("trailing_active", 0)),
                float(pos.get("highest_price", 0.0)),
                float(pos.get("lowest_price", 0.0)),
                float(pos.get("partial_pnl_usd", 0.0)),
                json.dumps(pos.get("meta", {})),
            ))

    def get_open_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM open_positions")
            return [dict(r) for r in cur.fetchall()]

    def delete_open_position(self, ticket_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM open_positions WHERE ticket_id = ?", (ticket_id,)
            )

    def save_partial_tp(self, ticket_id: str, lot_size: float,
                        price: float, pnl_usd: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO partial_tp_fills
                (ticket_id, lot_size, price, pnl_usd, filled_at)
                VALUES (?, ?, ?, ?, ?)
            """, (ticket_id, lot_size, price, pnl_usd, _iso()))

    def get_partial_fills(self, ticket_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM partial_tp_fills WHERE ticket_id = ? "
                "ORDER BY filled_at",
                (ticket_id,)
            )
            return [dict(r) for r in cur.fetchall()]

    # ==================================================================
    # CLOSED TRADES
    # ==================================================================
    def close_trade(self, trade: Dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO closed_trades
                (ticket_id, signal_uid, symbol, side, entry_price, exit_price,
                 initial_sl, initial_tp, lot_size, open_time, close_time,
                 realized_r, realized_pnl_usd, exit_reason, strategy, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade["ticket_id"], trade.get("signal_uid"), trade["symbol"],
                trade["side"], float(trade["entry_price"]),
                float(trade["exit_price"]),
                float(trade["initial_sl"]), float(trade["initial_tp"]),
                float(trade["lot_size"]), trade["open_time"], _iso(),
                float(trade["realized_r"]),
                float(trade["realized_pnl_usd"]),
                trade["exit_reason"], trade["strategy"],
                json.dumps(trade.get("meta", {})),
            ))

    def get_closed_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM closed_trades ORDER BY close_time DESC LIMIT ?",
                (limit,)
            )
            return [dict(r) for r in cur.fetchall()]

    def get_closed_by_strategy(self, strategy: str,
                               limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM closed_trades WHERE strategy = ? "
                "ORDER BY close_time DESC LIMIT ?",
                (strategy, limit)
            )
            return [dict(r) for r in cur.fetchall()]

    # ==================================================================
    # NEWS
    # ==================================================================
    def replace_news_cache(self, events: List[Dict[str, Any]]) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM news_cache")
            for e in events:
                self._conn.execute("""
                    INSERT INTO news_cache
                    (source, title, currency, impact, event_time,
                     forecast, previous, actual, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    e.get("source", "forexfactory"), e.get("title", ""),
                    e.get("currency", ""), e.get("impact", "medium"),
                    e.get("event_time", _iso()),
                    e.get("forecast", ""), e.get("previous", ""),
                    e.get("actual", ""), _iso(),
                ))

    def get_recent_news(self, hours: int = 24) -> List[Dict[str, Any]]:
        since = (utcnow() - timedelta(hours=hours)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM news_cache WHERE event_time >= ? "
                "ORDER BY event_time",
                (since,)
            )
            return [dict(r) for r in cur.fetchall()]

    # ==================================================================
    # MARKET SNAPSHOTS
    # ==================================================================
    def save_market_snapshot(self, snap: Dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO market_snapshots
                (snapshot_at, is_open, sessions, kill_zones,
                 blocked_reason, news_blackout, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                _iso(), int(snap.get("is_open", False)),
                json.dumps(snap.get("sessions", [])),
                json.dumps(snap.get("kill_zones", [])),
                snap.get("blocked_reason", ""),
                int(snap.get("news_blackout", 0)),
                json.dumps(snap.get("meta", {})),
            ))

    # ==================================================================
    # FILTER WEIGHTS
    # ==================================================================
    def get_filter_weights(self) -> Dict[str, float]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT strategy, weight FROM filter_weights"
            )
            return {r["strategy"]: float(r["weight"]) for r in cur.fetchall()}

    def get_filter_stats(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute("""
                SELECT strategy, weight, total_trades, wins, losses, total_r,
                       CASE WHEN total_trades > 0
                            THEN CAST(wins AS REAL) / total_trades * 100
                            ELSE 0 END AS win_rate
                FROM filter_weights
                ORDER BY weight DESC
            """)
            return [dict(r) for r in cur.fetchall()]

    def upsert_filter_weight(self, strategy: str, weight: float,
                             total: int, wins: int, losses: int,
                             total_r: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO filter_weights
                (strategy, weight, total_trades, wins, losses, total_r, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy) DO UPDATE SET
                    weight=excluded.weight,
                    total_trades=excluded.total_trades,
                    wins=excluded.wins,
                    losses=excluded.losses,
                    total_r=excluded.total_r,
                    last_updated=excluded.last_updated
            """, (strategy, weight, total, wins, losses, total_r, _iso()))

    # ==================================================================
    # FEEDBACK LOG
    # ==================================================================
    def log_feedback(self, fb: Dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO feedback_log
                (ticket_id, strategy, symbol, side,
                 realized_r, exit_reason, filters, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                fb["ticket_id"], fb["strategy"], fb["symbol"], fb["side"],
                float(fb["realized_r"]), fb["exit_reason"],
                json.dumps(fb.get("filters", {})), _iso(),
            ))

    # ==================================================================
    # FUNDAMENTAL BIAS CACHE
    # ==================================================================
    def save_fundamental_bias(self, symbol: str, bias: float,
                              confidence: float, components: Dict,
                              reason: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO fundamental_bias_cache
                (symbol, bias, confidence, components, reason, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    bias=excluded.bias,
                    confidence=excluded.confidence,
                    components=excluded.components,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
            """, (symbol, bias, confidence,
                  json.dumps(components), reason, _iso()))

    def get_fundamental_bias(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM fundamental_bias_cache WHERE symbol = ?",
                (symbol,)
            )
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)
            d["components"] = json.loads(d.get("components") or "{}")
            return d

    def get_all_fundamental_bias(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM fundamental_bias_cache"
            )
            out: Dict[str, Dict[str, Any]] = {}
            for r in cur.fetchall():
                d = dict(r)
                d["components"] = json.loads(d.get("components") or "{}")
                out[d["symbol"]] = d
            return out

    # ==================================================================
    # TIMEMACHINE CACHE
    # ==================================================================
    def save_timemachine(self, signal_uid: str,
                         result: Dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO timemachine_cache
                (signal_uid, runs, win_rate, expectancy_r, profit_factor,
                 max_dd_p95, risk_of_ruin, verdict, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_uid) DO UPDATE SET
                    runs=excluded.runs,
                    win_rate=excluded.win_rate,
                    expectancy_r=excluded.expectancy_r,
                    profit_factor=excluded.profit_factor,
                    max_dd_p95=excluded.max_dd_p95,
                    risk_of_ruin=excluded.risk_of_ruin,
                    verdict=excluded.verdict,
                    computed_at=excluded.computed_at
            """, (
                signal_uid, int(result["runs"]),
                float(result["win_rate"]),
                float(result["expectancy_r"]),
                float(result["profit_factor"]),
                float(result["max_dd_p95"]),
                float(result["risk_of_ruin"]),
                result["verdict"], _iso(),
            ))

    def get_timemachine(self, signal_uid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM timemachine_cache WHERE signal_uid = ?",
                (signal_uid,)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    # ==================================================================
    # STATS
    # ==================================================================
    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            cur = self._conn.execute("""
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(realized_pnl_usd), 0) AS pnl,
                       COALESCE(SUM(CASE WHEN realized_r > 0 THEN 1 ELSE 0 END), 0) AS wins,
                       COALESCE(SUM(CASE WHEN realized_r <= 0 THEN 1 ELSE 0 END), 0) AS losses
                FROM closed_trades
            """)
            row = cur.fetchone()
            total = int(row["n"] or 0)
            wins = int(row["wins"] or 0)
            return {
                "closed_count": total,
                "closed_pnl": round(float(row["pnl"] or 0.0), 2),
                "wins": wins,
                "losses": int(row["losses"] or 0),
                "win_rate": round((wins / total * 100) if total else 0.0, 1),
            }

    # ==================================================================
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
