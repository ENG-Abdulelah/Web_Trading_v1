# feedback/feedback_loop.py
"""
Feedback Loop Engine – محرك التغذية الراجعة

الهدف:
1. تسجيل تفاصيل كل صفقة (رابحة/خاسرة) مع الفلاتر النشطة وقت الدخول.
2. تسجيل تفصيلي موسّع للصفقات الخاسرة (losing_trades_log) لأغراض التحليل.
3. إعادة حساب أوزان الفلاتر تلقائياً بناءً على الأداء التاريخي
   (win-rate + متوسط R + tanh + EMA smoothing).
4. إتاحة الأوزان للمحرك الاستراتيجي عبر get_filter_weight().

تكامل:
    feedback = FeedbackLoop(persistence, config.get("feedback", {}))
    # عند إغلاق أي صفقة:
    feedback.record_trade_outcome(...)
    # داخل الاستراتيجية عند تجميع الفلاتر:
    w = feedback.get_filter_weight("trend_filter")   # [0.3 .. 1.7]

ملاحظة: يستخدم اتصال SQLite مستقل على نفس الملف (WAL) لتفادي التعارض.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ==================================================================
#                        نماذج البيانات
# ==================================================================
@dataclass
class FeedbackConfig:
    """إعدادات محرك التغذية الراجعة."""
    # شرط التحديث
    min_samples: int = 10                    # عدد الصفقات الأدنى قبل تعديل الوزن
    update_every_n_trades: int = 5           # تحديث الأوزان كل N صفقة مغلقة
    auto_update: bool = True                 # تشغيل التحديث التلقائي

    # صيغة الوزن
    perf_weight: float = 0.6                 # وزن win-rate في المعادلة
    r_weight: float = 0.4                    # وزن متوسط R
    sensitivity: float = 0.5                 # حساسية التعديل
    r_scale: float = 1.5                     # مقياس tanh لمتوسط R
    min_weight: float = 0.30                 # أدنى وزن لفيلتر
    max_weight: float = 1.70                 # أعلى وزن لفيلتر

    # التنعيم (EMA)
    use_ema: bool = True
    ema_decay: float = 0.4                   # 0 = لا تغيير، 1 = اعتماد كامل على الجديد

    # الاحتفاظ بالبيانات
    max_history_days: int = 180              # تنظيف بعد هذه المدة

    # التخزين
    db_path: Optional[str] = None            # إن لم يُمرَّر persistence


@dataclass
class FilterWeight:
    filter_name: str
    weight: float = 1.0
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_r: float = 0.0
    avg_r: float = 0.0
    win_rate: float = 0.0
    last_updated: Optional[str] = None
    sample_ready: bool = False


@dataclass
class LosingTradeRecord:
    ticket_id: str
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    realized_r: float
    exit_reason: str
    filters_active: Dict[str, float] = field(default_factory=dict)
    signal_strength: float = 0.0
    market_context: Dict[str, Any] = field(default_factory=dict)
    recorded_at: Optional[str] = None


# ==================================================================
#                        فئة FeedbackLoop
# ==================================================================
class FeedbackLoop:
    """
    محرك التغذية الراجعة. يُنشأ مرة واحدة في main.py ويُستعمل من:
      - execution/entry_exit.py   → record_trade_outcome
      - strategy/engine.py        → get_filter_weight / get_weights
      - dashboard / API           → get_summary / get_losing_trades
    """

    def __init__(
        self,
        persistence_or_db_path: Any,
        config: Optional[FeedbackConfig] = None,
    ):
        # تحديد مسار قاعدة البيانات
        if isinstance(persistence_or_db_path, (str, Path)):
            self.db_path = Path(persistence_or_db_path)
        else:
            self.db_path = Path(getattr(persistence_or_db_path, "db_path", "data/trading.db"))

        self.config = config or FeedbackConfig()

        # اتصال خاص (WAL يسمح بتزامن مع persistence)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")

        self._ensure_tables()
        logger.info("FeedbackLoop جاهز (db=%s, auto_update=%s)", self.db_path, self.config.auto_update)

    # ------------------------------------------------------------------
    # إنشاء الجداول
    # ------------------------------------------------------------------
    def _ensure_tables(self) -> None:
        with self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS filter_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL,
                    filter_name TEXT NOT NULL,
                    filter_value REAL DEFAULT 0,
                    outcome_r REAL NOT NULL,
                    is_win INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_filter_outcomes_filter
                    ON filter_outcomes(filter_name);
                CREATE INDEX IF NOT EXISTS idx_filter_outcomes_ticket
                    ON filter_outcomes(ticket_id);

                CREATE TABLE IF NOT EXISTS filter_weights (
                    filter_name TEXT PRIMARY KEY,
                    weight REAL DEFAULT 1.0,
                    total_trades INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    total_r REAL DEFAULT 0,
                    avg_r REAL DEFAULT 0,
                    win_rate REAL DEFAULT 0,
                    last_updated TEXT
                );

                CREATE TABLE IF NOT EXISTS losing_trades_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    realized_r REAL NOT NULL,
                    exit_reason TEXT NOT NULL,
                    filters_active TEXT DEFAULT '{}',
                    signal_strength REAL DEFAULT 0,
                    market_context TEXT DEFAULT '{}',
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_losing_trades_log_symbol
                    ON losing_trades_log(symbol);
                CREATE INDEX IF NOT EXISTS idx_losing_trades_log_date
                    ON losing_trades_log(recorded_at);

                CREATE TABLE IF NOT EXISTS feedback_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
        logger.debug("تم التحقق من جداول FeedbackLoop.")

    # ------------------------------------------------------------------
    # 1) تسجيل نتيجة صفقة مغلقة
    # ------------------------------------------------------------------
    def record_trade_outcome(
        self,
        ticket_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        realized_r: float,
        exit_reason: str,
        filters_active: Optional[Dict[str, float]] = None,
        signal_strength: float = 0.0,
        market_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        يُستدعى مرة واحدة عند إغلاق أي صفقة.
        - يخزّن outcome لكل فيلتر في filter_outcomes.
        - إذا كانت خاسرة، يسجّل تفاصيل موسّعة في losing_trades_log.
        - يزيد العدّاد ويطلق التحديث التلقائي عند الحاجة.
        """
        filters_active = filters_active or {}
        market_context = market_context or {}
        now_iso = datetime.now(timezone.utc).isoformat()
        is_win = 1 if realized_r > 0 else 0

        try:
            with self._conn:
                # (أ) سجّل outcome لكل فيلتر
                for fname, fval in filters_active.items():
                    self._conn.execute("""
                        INSERT INTO filter_outcomes
                        (ticket_id, filter_name, filter_value, outcome_r, is_win, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        ticket_id,
                        str(fname),
                        float(fval) if fval is not None else 0.0,
                        float(realized_r),
                        is_win,
                        now_iso,
                    ))

                # (ب) الصفقات الخاسرة → log تفصيلي
                if realized_r < 0:
                    self._conn.execute("""
                        INSERT INTO losing_trades_log
                        (ticket_id, symbol, side, entry_price, exit_price, realized_r,
                         exit_reason, filters_active, signal_strength, market_context, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        ticket_id, symbol, side,
                        float(entry_price), float(exit_price), float(realized_r),
                        str(exit_reason),
                        json.dumps(filters_active, ensure_ascii=False),
                        float(signal_strength),
                        json.dumps(market_context, ensure_ascii=False),
                        now_iso,
                    ))
                    logger.info("[FEEDBACK LOSS] %s | %s | R=%.2f | filters=%s",
                                ticket_id, symbol, realized_r, list(filters_active.keys()))

                # (ج) زيادة العدّاد
                self._increment_trade_counter()

            # (د) تحديث تلقائي للأوزان
            if self.config.auto_update and self._should_update():
                self.update_filter_weights()
        except Exception as e:
            logger.error("فشل تسجيل نتيجة الصفقة %s: %s", ticket_id, e)

    # ------------------------------------------------------------------
    # 2) إعادة حساب أوزان الفلاتر
    # ------------------------------------------------------------------
    def update_filter_weights(self, force: bool = False) -> Dict[str, float]:
        """
        إعادة حساب أوزان كل الفلاتر بناءً على filter_outcomes.
        Returns: dict {filter_name: new_weight}
        """
        try:
            rows = self._conn.execute("""
                SELECT filter_name,
                       COUNT(*)    AS total,
                       SUM(is_win) AS wins,
                       COUNT(*) - SUM(is_win) AS losses,
                       SUM(outcome_r) AS total_r
                FROM filter_outcomes
                GROUP BY filter_name
            """).fetchall()
        except Exception as e:
            logger.error("تعذّر قراءة filter_outcomes: %s", e)
            return {}

        if not rows:
            logger.info("لا توجد نتائج مسجّلة بعد – لا تحديث للأوزان.")
            return {}

        updated: Dict[str, float] = {}
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._conn:
            for r in rows:
                fname = r["filter_name"]
                total = int(r["total"] or 0)
                wins = int(r["wins"] or 0)
                losses = int(r["losses"] or 0)
                total_r = float(r["total_r"] or 0.0)
                avg_r = (total_r / total) if total > 0 else 0.0
                win_rate = (wins / total) if total > 0 else 0.0

                # الوزن الخام من المعادلة
                raw_weight = self._compute_weight(total, win_rate, avg_r)

                # تنعيم EMA مع الوزن السابق
                if self.config.use_ema and not force:
                    prev = self._conn.execute(
                        "SELECT weight FROM filter_weights WHERE filter_name = ?",
                        (fname,),
                    ).fetchone()
                    if prev and prev["weight"] is not None:
                        prev_w = float(prev["weight"])
                        alpha = self.config.ema_decay
                        new_weight = prev_w * (1.0 - alpha) + raw_weight * alpha
                    else:
                        new_weight = raw_weight
                else:
                    new_weight = raw_weight

                new_weight = max(self.config.min_weight, min(self.config.max_weight, new_weight))

                self._conn.execute("""
                    INSERT INTO filter_weights
                        (filter_name, weight, total_trades, wins, losses,
                         total_r, avg_r, win_rate, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(filter_name) DO UPDATE SET
                        weight=excluded.weight,
                        total_trades=excluded.total_trades,
                        wins=excluded.wins,
                        losses=excluded.losses,
                        total_r=excluded.total_r,
                        avg_r=excluded.avg_r,
                        win_rate=excluded.win_rate,
                        last_updated=excluded.last_updated
                """, (
                    fname, new_weight, total, wins, losses,
                    total_r, avg_r, win_rate, now_iso,
                ))
                updated[fname] = round(new_weight, 4)

                logger.info(
                    "[WEIGHT UPDATE] %-22s | n=%3d | win=%.1f%% | avgR=%+.2f | w=%.3f",
                    fname, total, win_rate * 100, avg_r, new_weight,
                )

            # نصفّر العدّاد
            self._conn.execute(
                "INSERT INTO feedback_meta(key, value) VALUES('trades_since_update','0') "
                "ON CONFLICT(key) DO UPDATE SET value='0'"
            )

        logger.info("تم تحديث أوزان %d فيلتر.", len(updated))
        return updated

    # ------------------------------------------------------------------
    # 3) قراءة الأوزان (للاستراتيجية والداشبورد)
    # ------------------------------------------------------------------
    def get_filter_weight(self, filter_name: str, default: float = 1.0) -> float:
        """الوزن الحالي لفيلتر (للاستخدام داخل strategy/engine.py)."""
        try:
            row = self._conn.execute(
                "SELECT weight FROM filter_weights WHERE filter_name = ?",
                (filter_name,),
            ).fetchone()
            return float(row["weight"]) if row else default
        except Exception as e:
            logger.debug("get_filter_weight(%s) فشل: %s", filter_name, e)
            return default

    def get_all_weights(self) -> Dict[str, float]:
        """كل الأوزان كقاموس."""
        try:
            rows = self._conn.execute("SELECT filter_name, weight FROM filter_weights").fetchall()
            return {r["filter_name"]: float(r["weight"]) for r in rows}
        except Exception:
            return {}

    def get_detailed_weights(self) -> List[FilterWeight]:
        """تفاصيل كاملة لكل فيلتر (للداشبورد)."""
        try:
            rows = self._conn.execute("SELECT * FROM filter_weights").fetchall()
        except Exception:
            return []
        out: List[FilterWeight] = []
        for r in rows:
            out.append(FilterWeight(
                filter_name=r["filter_name"],
                weight=float(r["weight"] or 1.0),
                total_trades=int(r["total_trades"] or 0),
                wins=int(r["wins"] or 0),
                losses=int(r["losses"] or 0),
                total_r=float(r["total_r"] or 0.0),
                avg_r=float(r["avg_r"] or 0.0),
                win_rate=float(r["win_rate"] or 0.0),
                last_updated=r["last_updated"],
                sample_ready=int(r["total_trades"] or 0) >= self.config.min_samples,
            ))
        return out

    # ------------------------------------------------------------------
    # 4) تحليل الصفقات الخاسرة
    # ------------------------------------------------------------------
    def get_losing_trades(
        self,
        limit: int = 100,
        symbol: Optional[str] = None,
    ) -> List[LosingTradeRecord]:
        """سحب آخر N صفقة خاسرة (اختياري: فلترة بالرمز)."""
        try:
            if symbol:
                rows = self._conn.execute("""
                    SELECT * FROM losing_trades_log
                    WHERE symbol = ?
                    ORDER BY recorded_at DESC LIMIT ?
                """, (symbol, limit)).fetchall()
            else:
                rows = self._conn.execute("""
                    SELECT * FROM losing_trades_log
                    ORDER BY recorded_at DESC LIMIT ?
                """, (limit,)).fetchall()
        except Exception as e:
            logger.error("get_losing_trades فشل: %s", e)
            return []

        result: List[LosingTradeRecord] = []
        for r in rows:
            try:
                filters = json.loads(r["filters_active"] or "{}")
            except Exception:
                filters = {}
            try:
                ctx = json.loads(r["market_context"] or "{}")
            except Exception:
                ctx = {}
            result.append(LosingTradeRecord(
                ticket_id=r["ticket_id"],
                symbol=r["symbol"],
                side=r["side"],
                entry_price=float(r["entry_price"]),
                exit_price=float(r["exit_price"]),
                realized_r=float(r["realized_r"]),
                exit_reason=r["exit_reason"],
                filters_active=filters,
                signal_strength=float(r["signal_strength"] or 0.0),
                market_context=ctx,
                recorded_at=r["recorded_at"],
            ))
        return result

    def analyze_weak_filters(
        self, max_avg_r: float = -0.2, min_samples: Optional[int] = None
    ) -> List[FilterWeight]:
        """
        الفلاتر التي أدّت لخسائر متكررة (مرشحة للتعطيل أو تقليل وزنها).
        """
        min_samples = min_samples or self.config.min_samples
        weak: List[FilterWeight] = []
        for f in self.get_detailed_weights():
            if f.total_trades >= min_samples and f.avg_r <= max_avg_r:
                weak.append(f)
        return sorted(weak, key=lambda x: x.avg_r)

    def analyze_strong_filters(
        self, min_avg_r: float = 0.3, min_samples: Optional[int] = None
    ) -> List[FilterWeight]:
        """الفلاتر الأقوى (مرشحة لزيادة وزنها)."""
        min_samples = min_samples or self.config.min_samples
        strong: List[FilterWeight] = []
        for f in self.get_detailed_weights():
            if f.total_trades >= min_samples and f.avg_r >= min_avg_r:
                strong.append(f)
        return sorted(strong, key=lambda x: x.avg_r, reverse=True)

    # ------------------------------------------------------------------
    # 5) ملخص شامل للداشبورد / API
    # ------------------------------------------------------------------
    def get_summary(self) -> Dict[str, Any]:
        """ملخص شامل للوحة التحكم."""
        weights = self.get_detailed_weights()
        meta = self._get_meta()
        try:
            losing_count = self._conn.execute(
                "SELECT COUNT(*) AS c FROM losing_trades_log"
            ).fetchone()["c"]
        except Exception:
            losing_count = 0
        try:
            total_outcomes = self._conn.execute(
                "SELECT COUNT(DISTINCT ticket_id) AS c FROM filter_outcomes"
            ).fetchone()["c"]
        except Exception:
            total_outcomes = 0

        return {
            "total_outcomes_recorded": total_outcomes,
            "losing_trades_logged": losing_count,
            "trades_since_update": int(meta.get("trades_since_update", 0)),
            "update_every_n_trades": self.config.update_every_n_trades,
            "filters": [
                {
                    "name": f.filter_name,
                    "weight": round(f.weight, 3),
                    "win_rate": round(f.win_rate * 100, 2),
                    "avg_r": round(f.avg_r, 3),
                    "trades": f.total_trades,
                    "sample_ready": f.sample_ready,
                }
                for f in weights
            ],
            "weak_filters": [f.filter_name for f in self.analyze_weak_filters()],
            "strong_filters": [f.filter_name for f in self.analyze_strong_filters()],
        }

    # ------------------------------------------------------------------
    # 6) صيانة
    # ------------------------------------------------------------------
    def cleanup_old_records(self) -> int:
        """حذف السجلات الأقدم من max_history_days. Returns: عدد السجلات المحذوفة."""
        try:
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(days=self.config.max_history_days)).isoformat()
            with self._conn:
                cur1 = self._conn.execute(
                    "DELETE FROM filter_outcomes WHERE recorded_at < ?", (cutoff,)
                )
                cur2 = self._conn.execute(
                    "DELETE FROM losing_trades_log WHERE recorded_at < ?", (cutoff,)
                )
            total = (cur1.rowcount or 0) + (cur2.rowcount or 0)
            if total:
                logger.info("تم حذف %d سجلاً قديماً من feedback.", total)
            return total
        except Exception as e:
            logger.error("cleanup_old_records فشل: %s", e)
            return 0

    def reset_all(self, also_weights: bool = True) -> None:
        """إعادة تعيين كل بيانات الـ feedback (حذر: لا رجعة)."""
        with self._conn:
            self._conn.execute("DELETE FROM filter_outcomes")
            self._conn.execute("DELETE FROM losing_trades_log")
            self._conn.execute("DELETE FROM feedback_meta")
            if also_weights:
                self._conn.execute("DELETE FROM filter_weights")
        logger.warning("تمت إعادة تعيين بيانات FeedbackLoop بالكامل.")

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            logger.info("تم إغلاق اتصال FeedbackLoop.")

    # ------------------------------------------------------------------
    # 7) دوال داخلية
    # ------------------------------------------------------------------
    def _compute_weight(self, total: int, win_rate: float, avg_r: float) -> float:
        """المعادلة الأساسية لوزن الفيلتر."""
        if total < self.config.min_samples:
            return 1.0  # محايد قبل توفّر عينة كافية

        perf_score = (win_rate - 0.5) * 2.0                  # [-1, +1]
        r_score = math.tanh(avg_r / max(self.config.r_scale, 1e-6))  # [-1, +1]
        combined = self.config.perf_weight * perf_score + self.config.r_weight * r_score
        raw = 1.0 + self.config.sensitivity * combined       # ~[0.5, 1.5]
        return max(self.config.min_weight, min(self.config.max_weight, raw))

    def _increment_trade_counter(self) -> None:
        try:
            self._conn.execute("""
                INSERT INTO feedback_meta(key, value) VALUES('trades_since_update','1')
                ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
            """)
        except Exception as e:
            logger.debug("_increment_trade_counter: %s", e)

    def _should_update(self) -> bool:
        try:
            val = self._get_meta().get("trades_since_update", "0")
            return int(val) >= self.config.update_every_n_trades
        except Exception:
            return False

    def _get_meta(self) -> Dict[str, str]:
        try:
            rows = self._conn.execute("SELECT key, value FROM feedback_meta").fetchall()
            return {r["key"]: r["value"] for r in rows}
        except Exception:
            return {}
