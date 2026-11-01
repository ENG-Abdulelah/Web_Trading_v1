# execution/entry_exit.py
"""
مدير الدخول والخروج (Entry/Exit Manager) - المسؤول عن:
1. تخطيط SL/TP عند الدخول.
2. إدارة الصفقات المفتوحة حياً (نقل الوقف للتعادل، الوقف المتحرك، جني أرباح جزئي TP1).
3. التكامل مع قاعدة بيانات SQLite لحفظ الحالة واستعادتها.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple

from database.persistence import PersistenceManager, OpenPositionRecord

logger = logging.getLogger(__name__)


class ExitReason(Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    MANUAL = "manual"
    SHUTDOWN = "shutdown"


@dataclass
class LivePosition:
    """تمثيل الصفقة المفتوحة في الذاكرة."""
    ticket_id: str
    symbol: str
    side: str  # "buy" or "sell"
    entry_price: float
    initial_sl: float
    initial_tp: float
    current_sl: float
    current_tp: float
    lot_size: float
    open_time: datetime
    strategy_name: str
    risk_reward: float
    partial_tp1_done: bool = False
    breakeven_done: bool = False
    trailing_active: bool = False
    highest_price: float = 0.0  # للشراء
    lowest_price: float = float("inf")  # للبيع
    metadata: dict = field(default_factory=dict)


class EntryExitManager:
    """
    يدير دورة حياة الصفقات من الدخول حتى الإغلاق.
    يتم استدعاء `update_open_positions` في كل شمعة جديدة من الحلقة الرئيسية.
    """

    def __init__(
        self,
        persistence: PersistenceManager,
        breakeven_at_r: float = 1.0,
        trailing_start_r: float = 1.5,
        trailing_distance_pips: float = 10.0,
        partial_tp1_ratio: float = 0.5,
        pip_value_per_lot: Dict[str, float] = None,
    ):
        self.persistence = persistence
        self.breakeven_at_r = breakeven_at_r
        self.trailing_start_r = trailing_start_r
        self.trailing_distance_pips = trailing_distance_pips
        self.partial_tp1_ratio = partial_tp1_ratio
        self.pip_value_per_lot = pip_value_per_lot or {}

        # تحميل الصفقات المفتوحة من قاعدة البيانات عند بدء التشغيل
        self.open_positions: Dict[str, LivePosition] = {}
        self._load_open_positions()

    # ------------------------------------------------------------------
    # تحميل الحالة من التخزين الدائم
    # ------------------------------------------------------------------
    def _load_open_positions(self) -> None:
        """استعادة الصفقات المفتوحة من SQLite لضمان عدم فقدانها عند إعادة التشغيل."""
        records = self.persistence.get_all_open_positions()
        for rec in records:
            pos = LivePosition(
                ticket_id=rec.ticket_id,
                symbol=rec.symbol,
                side=rec.side,
                entry_price=rec.entry_price,
                initial_sl=rec.initial_sl,
                initial_tp=rec.initial_tp,
                current_sl=rec.current_sl,
                current_tp=rec.current_tp,
                lot_size=rec.lot_size,
                open_time=rec.open_time,
                strategy_name=rec.strategy_name,
                risk_reward=rec.risk_reward,
                partial_tp1_done=rec.partial_tp1_done,
                breakeven_done=rec.breakeven_done,
                trailing_active=rec.trailing_active,
                highest_price=rec.highest_price,
                lowest_price=rec.lowest_price,
                metadata=rec.metadata,
            )
            self.open_positions[pos.ticket_id] = pos
            logger.info(f"تم استعادة الصفقة المفتوحة: {pos.ticket_id} ({pos.symbol})")

    # ------------------------------------------------------------------
    # تسجيل صفقة جديدة (يُستدعى بعد التنفيذ مباشرة)
    # ------------------------------------------------------------------
    def register_new_position(
        self,
        ticket_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        sl: float,
        tp: float,
        lot_size: float,
        strategy_name: str,
        risk_reward: float,
    ) -> LivePosition:
        """إنشاء كائن LivePosition وحفظه فوراً في قاعدة البيانات."""
        pos = LivePosition(
            ticket_id=ticket_id,
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            initial_sl=sl,
            initial_tp=tp,
            current_sl=sl,
            current_tp=tp,
            lot_size=lot_size,
            open_time=datetime.utcnow(),
            strategy_name=strategy_name,
            risk_reward=risk_reward,
            highest_price=entry_price if side == "buy" else 0.0,
            lowest_price=entry_price if side == "sell" else float("inf"),
        )
        self.open_positions[ticket_id] = pos
        self.persistence.save_open_position(pos)
        logger.info(f"تم تسجيل صفقة جديدة: {ticket_id} | {side} {symbol} @ {entry_price}")
        return pos

    # ------------------------------------------------------------------
    # التحديث الديناميكي لكل صفقة مفتوحة (يُستدعى في كل شمعة)
    # ------------------------------------------------------------------
    def update_open_positions(
        self,
        current_prices: Dict[str, float],
        atr_values: Optional[Dict[str, float]] = None,
    ) -> List[Tuple[str, ExitReason, float]]:
        """
        المرور على جميع الصفقات المفتوحة وتطبيق قواعد الإدارة الحية.
        يُرجع قائمة بالصفقات التي يجب إغلاقها: (ticket_id, exit_reason, exit_price).
        """
        closed_actions: List[Tuple[str, ExitReason, float]] = []
        atr_values = atr_values or {}

        for ticket_id, pos in list(self.open_positions.items()):
            if pos.symbol not in current_prices:
                continue
            price = current_prices[pos.symbol]
            atr = atr_values.get(pos.symbol, 0.0)

            # 1. تحديث أعلى/أدنى سعر منذ الدخول
            if pos.side == "buy":
                pos.highest_price = max(pos.highest_price, price)
            else:
                pos.lowest_price = min(pos.lowest_price, price)

            # 2. حساب R الحالي (الربح بالوحدات النسبية)
            risk_per_unit = abs(pos.entry_price - pos.initial_sl)
            if risk_per_unit == 0:
                continue  # تجنب القسمة على صفر

            if pos.side == "buy":
                current_r = (price - pos.entry_price) / risk_per_unit
            else:
                current_r = (pos.entry_price - price) / risk_per_unit

            # 3. فحص إغلاق كامل (SL/TP) أولاً
            if self._check_full_exit(pos, price):
                reason, exit_price = self._check_full_exit(pos, price)
                closed_actions.append((ticket_id, reason, exit_price))
                continue

            # 4. جني أرباح جزئي TP1 (إذا لم يتم بعد)
            if not pos.partial_tp1_done and current_r >= 1.0:
                self._execute_partial_tp1(pos, price)
                # لا نغلق الصفقة بالكامل هنا، نكتفي بتحديث الحالة

            # 5. نقل الوقف إلى التعادل (Breakeven) عند +1R
            if not pos.breakeven_done and current_r >= self.breakeven_at_r:
                self._move_to_breakeven(pos)
                pos.breakeven_done = True

            # 6. تفعيل الوقف المتحرك (Trailing Stop) بعد +1.5R
            if current_r >= self.trailing_start_r and not pos.trailing_active:
                pos.trailing_active = True
                logger.info(f"تم تفعيل الوقف المتحرك للصفقة {ticket_id} عند {current_r:.2f}R")

            if pos.trailing_active:
                self._update_trailing_stop(pos, price, atr)

            # 7. حفظ أي تغييرات في قاعدة البيانات (لتحديث SL/TP وحالة TP1)
            self.persistence.save_open_position(pos)

        # معالجة الإغلاقات (إزالة من الذاكرة وقاعدة البيانات)
        for ticket_id, reason, exit_price in closed_actions:
            self._close_position(ticket_id, reason, exit_price)

        return closed_actions

    # ------------------------------------------------------------------
    # دوال مساعدة داخلية
    # ------------------------------------------------------------------
    def _check_full_exit(self, pos: LivePosition, price: float) -> Optional[Tuple[ExitReason, float]]:
        """فحص ما إذا كان السعر قد ضرب SL أو TP الحاليين."""
        if pos.side == "buy":
            if price <= pos.current_sl:
                return ExitReason.STOP_LOSS, pos.current_sl
            if price >= pos.current_tp:
                return ExitReason.TAKE_PROFIT, pos.current_tp
        else:  # sell
            if price >= pos.current_sl:
                return ExitReason.STOP_LOSS, pos.current_sl
            if price <= pos.current_tp:
                return ExitReason.TAKE_PROFIT, pos.current_tp
        return None

    def _execute_partial_tp1(self, pos: LivePosition, price: float) -> None:
        """إغلاق جزء من الصفقة (TP1) وتسجيل الربح الجزئي."""
        partial_lot = pos.lot_size * self.partial_tp1_ratio
        pos.lot_size -= partial_lot  # تعديل الحجم المتبقي
        pos.partial_tp1_done = True
        self.persistence.save_partial_tp(pos.ticket_id, partial_lot, price)
        logger.info(
            f"TP1 جزئي للصفقة {pos.ticket_id}: إغلاق {partial_lot} لوت عند {price}. "
            f"الحجم المتبقي: {pos.lot_size}"
        )

    def _move_to_breakeven(self, pos: LivePosition) -> None:
        """نقل وقف الخسارة إلى سعر الدخول (التعادل)."""
        pos.current_sl = pos.entry_price
        logger.info(f"نقل الوقف إلى التعادل للصفقة {pos.ticket_id} عند {pos.entry_price}")

    def _update_trailing_stop(self, pos: LivePosition, price: float, atr: float) -> None:
        """تحديث الوقف المتحرك بناءً على السعر الحالي والمسافة المحددة."""
        # استخدام ATR إذا توفر، وإلا استخدام مسافة نقاط ثابتة
        if atr > 0:
            distance = atr * 1.5  # مضاعف ATR
        else:
            pip = self.pip_value_per_lot.get(pos.symbol, 0.0001)
            distance = self.trailing_distance_pips * pip

        if pos.side == "buy":
            new_sl = pos.highest_price - distance
            if new_sl > pos.current_sl:
                pos.current_sl = new_sl
                logger.debug(f"تحديث الوقف المتحرك (شراء) {pos.ticket_id}: {new_sl}")
        else:  # sell
            new_sl = pos.lowest_price + distance
            if new_sl < pos.current_sl:
                pos.current_sl = new_sl
                logger.debug(f"تحديث الوقف المتحرك (بيع) {pos.ticket_id}: {new_sl}")

    def _close_position(self, ticket_id: str, reason: ExitReason, exit_price: float) -> None:
        """إغلاق الصفقة بالكامل، إزالتها من الذاكرة، وتسجيلها في السجل المغلق."""
        pos = self.open_positions.pop(ticket_id, None)
        if pos is None:
            return

        # حساب R المحقق
        risk_per_unit = abs(pos.entry_price - pos.initial_sl)
        if pos.side == "buy":
            realized_r = (exit_price - pos.entry_price) / risk_per_unit
        else:
            realized_r = (pos.entry_price - exit_price) / risk_per_unit

        self.persistence.close_position(
            ticket_id=ticket_id,
            exit_price=exit_price,
            realized_r=realized_r,
            reason=reason.value,
            close_time=datetime.utcnow(),
        )
        logger.info(
            f"تم إغلاق الصفقة {ticket_id} ({pos.symbol}) | السبب: {reason.value} | "
            f"سعر الخروج: {exit_price} | R محقق: {realized_r:.2f}"
        )

    # ------------------------------------------------------------------
    # إغلاق طارئ لجميع الصفقات (عند إيقاف التشغيل)
    # ------------------------------------------------------------------
    def close_all_positions(self, current_prices: Dict[str, float]) -> None:
        """إغلاق جميع الصفقات المفتوحة عند إيقاف تشغيل النظام."""
        for ticket_id, pos in list(self.open_positions.items()):
            price = current_prices.get(pos.symbol, pos.entry_price)
            self._close_position(ticket_id, ExitReason.SHUTDOWN, price)
