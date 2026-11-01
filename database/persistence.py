# database/persistence.py
"""
طبقة التخزين الدائم (Persistence Layer) باستخدام SQLite.
تحفظ: الصفقات المفتوحة، الأوامر المعلقة، السجل المغلق، وإحصائيات الاستراتيجية.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DB_PATH = Path("data/trading.db")


@dataclass
class OpenPositionRecord:
    """سجل الصفقة المفتوحة كما يُخزن في SQLite."""
    ticket_id: str
    symbol: str
    side: str
    entry_price: float
    initial_sl: float
    initial_tp: float
    current_sl: float
    current_tp: float
    lot_size: float
    open_time: datetime
    strategy_name: str
    risk_reward: float
    partial_tp1_done: bool
    breakeven_done: bool
    trailing_active: bool
    highest_price: float
    lowest_price: float
    metadata: dict


class PersistenceManager:
    """
    مدير التخزين الدائم. يقوم بإنشاء الجداول تلقائياً عند التهيئة.
    """

    def __init__(self, db_path: str = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._connect()
        self._create_tables()

    def _connect(self) -> None:
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        logger.info(f"تم الاتصال بقاعدة البيانات: {self.db_path}")

    def _create_tables(self) -> None:
        """إنشاء الجداول المطلوبة إذا لم تكن موجودة."""
        with self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS open_positions (
                    ticket_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    initial_sl REAL NOT NULL,
                    initial_tp REAL NOT NULL,
                    current_sl REAL NOT NULL,
                    current_tp REAL NOT NULL,
                    lot_size REAL NOT NULL,
                    open_time TEXT NOT NULL,
                    strategy_name TEXT NOT NULL,
                    risk_reward REAL NOT NULL,
                    partial_tp1_done INTEGER DEFAULT 0,
                    breakeven_done INTEGER DEFAULT 0,
                    trailing_active INTEGER DEFAULT 0,
                    highest_price REAL DEFAULT 0,
                    lowest_price REAL DEFAULT 0,
                    metadata TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS pending_orders (
                    order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    price REAL NOT NULL,
                    lot_size REAL NOT NULL,
                    sl REAL,
                    tp REAL,
                    strategy_name TEXT,
                    created_at TEXT NOT NULL,
                    metadata TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS closed_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL,
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
                    exit_reason TEXT NOT NULL,
                    strategy_name TEXT NOT NULL,
                    metadata TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS partial_tp_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL,
                    lot_size REAL NOT NULL,
                    price REAL NOT NULL,
                    filled_at TEXT NOT NULL,
                    FOREIGN KEY(ticket_id) REFERENCES closed_trades(ticket_id)
                );

                CREATE TABLE IF NOT EXISTS strategy_stats (
                    strategy_name TEXT PRIMARY KEY,
                    total_trades INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    total_r REAL DEFAULT 0,
                    last_updated TEXT
                );
            """)
        logger.info("تم التحقق من جداول قاعدة البيانات.")

    # ------------------------------------------------------------------
    # عمليات الصفقات المفتوحة
    # ------------------------------------------------------------------
    def save_open_position(self, pos) -> None:
        """إدراج أو تحديث صفقة مفتوحة."""
        with self._conn:
            self._conn.execute("""
                INSERT INTO open_positions (
                    ticket_id, symbol, side, entry_price, initial_sl, initial_tp,
                    current_sl, current_tp, lot_size, open_time, strategy_name,
                    risk_reward, partial_tp1_done, breakeven_done, trailing_active,
                    highest_price, lowest_price, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET
                    current_sl=excluded.current_sl,
                    current_tp=excluded.current_tp,
                    lot_size=excluded.lot_size,
                    partial_tp1_done=excluded.partial_tp1_done,
                    breakeven_done=excluded.breakeven_done,
                    trailing_active=excluded.trailing_active,
                    highest_price=excluded.highest_price,
                    lowest_price=excluded.lowest_price,
                    metadata=excluded.metadata
            """, (
                pos.ticket_id, pos.symbol, pos.side, pos.entry_price,
                pos.initial_sl, pos.initial_tp, pos.current_sl, pos.current_tp,
                pos.lot_size, pos.open_time.isoformat(), pos.strategy_name,
                pos.risk_reward, int(pos.partial_tp1_done), int(pos.breakeven_done),
                int(pos.trailing_active), pos.highest_price, pos.lowest_price,
                json.dumps(pos.metadata)
            ))

    def get_all_open_positions(self) -> List[OpenPositionRecord]:
        """استرجاع جميع الصفقات المفتوحة."""
        cursor = self._conn.execute("SELECT * FROM open_positions")
        rows = cursor.fetchall()
        return [self._row_to_open_record(row) for row in rows]

    def _row_to_open_record(self, row: sqlite3.Row) -> OpenPositionRecord:
        return OpenPositionRecord(
            ticket_id=row["ticket_id"],
            symbol=row["symbol"],
            side=row["side"],
            entry_price=row["entry_price"],
            initial_sl=row["initial_sl"],
            initial_tp=row["initial_tp"],
            current_sl=row["current_sl"],
            current_tp=row["current_tp"],
            lot_size=row["lot_size"],
            open_time=datetime.fromisoformat(row["open_time"]),
            strategy_name=row["strategy_name"],
            risk_reward=row["risk_reward"],
            partial_tp1_done=bool(row["partial_tp1_done"]),
            breakeven_done=bool(row["breakeven_done"]),
            trailing_active=bool(row["trailing_active"]),
            highest_price=row["highest_price"],
            lowest_price=row["lowest_price"],
            metadata=json.loads(row["metadata"]),
        )

    def save_partial_tp(self, ticket_id: str, lot_size: float, price: float) -> None:
        """تسجيل عملية جني أرباح جزئي."""
        with self._conn:
            self._conn.execute("""
                INSERT INTO partial_tp_fills (ticket_id, lot_size, price, filled_at)
                VALUES (?, ?, ?, ?)
            """, (ticket_id, lot_size, price, datetime.utcnow().isoformat()))

    def close_position(
        self,
        ticket_id: str,
        exit_price: float,
        realized_r: float,
        reason: str,
        close_time: datetime,
    ) -> None:
        """نقل الصفقة من open_positions إلى closed_trades."""
        cursor = self._conn.execute("SELECT * FROM open_positions WHERE ticket_id = ?", (ticket_id,))
        row = cursor.fetchone()
        if row is None:
            logger.warning(f"محاولة إغلاق صفقة غير موجودة: {ticket_id}")
            return

        with self._conn:
            self._conn.execute("""
                INSERT INTO closed_trades (
                    ticket_id, symbol, side, entry_price, exit_price,
                    initial_sl, initial_tp, lot_size, open_time, close_time,
                    realized_r, exit_reason, strategy_name, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["ticket_id"], row["symbol"], row["side"], row["entry_price"],
                exit_price, row["initial_sl"], row["initial_tp"], row["lot_size"],
                row["open_time"], close_time.isoformat(), realized_r, reason,
                row["strategy_name"], row["metadata"]
            ))
            self._conn.execute("DELETE FROM open_positions WHERE ticket_id = ?", (ticket_id,))

        # تحديث إحصائيات الاستراتيجية
        self._update_strategy_stats(row["strategy_name"], realized_r)

    def _update_strategy_stats(self, strategy_name: str, realized_r: float) -> None:
        """تحديث عدادات الاستراتيجية (عدد الصفقات، الربح/الخسارة، مجموع R)."""
        win = 1 if realized_r > 0 else 0
        loss = 1 if realized_r < 0 else 0
        with self._conn:
            self._conn.execute("""
                INSERT INTO strategy_stats (strategy_name, total_trades, wins, losses, total_r, last_updated)
                VALUES (?, 1, ?, ?, ?, ?)
                ON CONFLICT(strategy_name) DO UPDATE SET
                    total_trades = total_trades + 1,
                    wins = wins + excluded.wins,
                    losses = losses + excluded.losses,
                    total_r = total_r + excluded.total_r,
                    last_updated = excluded.last_updated
            """, (strategy_name, win, loss, realized_r, datetime.utcnow().isoformat()))

    # ------------------------------------------------------------------
    # الأوامر المعلقة
    # ------------------------------------------------------------------
    def save_pending_order(
        self,
        order_id: str,
        symbol: str,
        side: str,
        order_type: str,
        price: float,
        lot_size: float,
        sl: float = None,
        tp: float = None,
        strategy_name: str = None,
        metadata: dict = None,
    ) -> None:
        """حفظ أمر معلق (Pending Order) في قاعدة البيانات."""
        with self._conn:
            self._conn.execute("""
                INSERT OR REPLACE INTO pending_orders (
                    order_id, symbol, side, order_type, price, lot_size,
                    sl, tp, strategy_name, created_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order_id, symbol, side, order_type, price, lot_size,
                sl, tp, strategy_name, datetime.utcnow().isoformat(),
                json.dumps(metadata or {})
            ))

    def get_pending_orders(self) -> List[sqlite3.Row]:
        """استرجاع جميع الأوامر المعلقة."""
        cursor = self._conn.execute("SELECT * FROM pending_orders")
        return cursor.fetchall()

    def delete_pending_order(self, order_id: str) -> None:
        """حذف أمر معلق بعد تنفيذه أو إلغائه."""
        with self._conn:
            self._conn.execute("DELETE FROM pending_orders WHERE order_id = ?", (order_id,))

    # ------------------------------------------------------------------
    # السجل المغلق وإحصائيات الاستراتيجية
    # ------------------------------------------------------------------
    def get_closed_trades(self, limit: int = 100) -> List[sqlite3.Row]:
        """استرجاع آخر N صفقة مغلقة."""
        cursor = self._conn.execute(
            "SELECT * FROM closed_trades ORDER BY close_time DESC LIMIT ?", (limit,)
        )
        return cursor.fetchall()

    def get_strategy_stats(self) -> List[sqlite3.Row]:
        """استرجاع إحصائيات جميع الاستراتيجيات."""
        cursor = self._conn.execute("SELECT * FROM strategy_stats")
        return cursor.fetchall()

    def close(self) -> None:
        """إغلاق الاتصال بقاعدة البيانات."""
        if self._conn:
            self._conn.close()
            logger.info("تم إغلاق اتصال قاعدة البيانات.")
