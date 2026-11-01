# execution/risk_manager.py
"""
مدير المخاطر (Risk Manager) – منصة Web Trading

المسؤوليات:
1. حساب حجم اللوت بدقة عالية (نزولاً إلى 0.001 – nano lot) لدعم الحسابات الصغيرة ($50).
2. تطبيق مخاطرة 1.5% لكل صفقة دون رفض صارم للصفقات الصغيرة (fallback ذكي).
3. فرض سقوف التزامن في المحفظة (portfolio concurrency caps).
4. تطبيق بوابات أمان نسبة المخاطرة/المكافأة (R:R gates).
5. حدود الخسارة اليومية، المخاطرة الإجمالية، وحدود SL/Min/MAX pips.
6. إيقاف تلقائي بعد سلسلة خسائر متتالية (consecutive-loss cooldown).

يُستخدم من `main.py` و`execution/entry_exit.py` قبل فتح أي صفقة.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from database.persistence import PersistenceManager

logger = logging.getLogger(__name__)


# ==================================================================
#                إعدادات الرموز الافتراضية (Symbol Specs)
# ==================================================================
DEFAULT_SYMBOL_SPECS: Dict[str, Dict[str, Any]] = {
    # Forex majors – pip = 0.0001, $10/pip لكل لوت
    "EUR/USD": dict(pip_size=0.0001, pip_value_per_lot=10.0, contract_size=100_000.0,
                    min_lot=0.001, max_lot=100.0, lot_step=0.001, digits=5),
    "GBP/USD": dict(pip_size=0.0001, pip_value_per_lot=10.0, contract_size=100_000.0,
                    min_lot=0.001, max_lot=100.0, lot_step=0.001, digits=5),
    "AUD/USD": dict(pip_size=0.0001, pip_value_per_lot=10.0, contract_size=100_000.0,
                    min_lot=0.001, max_lot=100.0, lot_step=0.001, digits=5),
    "NZD/USD": dict(pip_size=0.0001, pip_value_per_lot=10.0, contract_size=100_000.0,
                    min_lot=0.001, max_lot=100.0, lot_step=0.001, digits=5),
    "USD/CAD": dict(pip_size=0.0001, pip_value_per_lot=7.4, contract_size=100_000.0,
                    min_lot=0.001, max_lot=100.0, lot_step=0.001, digits=5),
    "USD/CHF": dict(pip_size=0.0001, pip_value_per_lot=11.2, contract_size=100_000.0,
                    min_lot=0.001, max_lot=100.0, lot_step=0.001, digits=5),
    "USD/JPY": dict(pip_size=0.01, pip_value_per_lot=6.7, contract_size=100_000.0,
                    min_lot=0.001, max_lot=100.0, lot_step=0.001, digits=3),
    "EUR/JPY": dict(pip_size=0.01, pip_value_per_lot=6.7, contract_size=100_000.0,
                    min_lot=0.001, max_lot=100.0, lot_step=0.001, digits=3),
    "GBP/JPY": dict(pip_size=0.01, pip_value_per_lot=6.7, contract_size=100_000.0,
                    min_lot=0.001, max_lot=100.0, lot_step=0.001, digits=3),

    # المعادن – XAUUSD: $1/pip لكل لوت، عقد 100 oz
    "XAU/USD": dict(pip_size=0.01, pip_value_per_lot=1.0, contract_size=100.0,
                    min_lot=0.001, max_lot=50.0, lot_step=0.001, digits=2),
    "XAG/USD": dict(pip_size=0.001, pip_value_per_lot=5.0, contract_size=5_000.0,
                    min_lot=0.001, max_lot=50.0, lot_step=0.001, digits=3),

    # Crypto
    "BTC/USD": dict(pip_size=1.0, pip_value_per_lot=1.0, contract_size=1.0,
                    min_lot=0.001, max_lot=10.0, lot_step=0.001, digits=2),
    "ETH/USD": dict(pip_size=0.01, pip_value_per_lot=1.0, contract_size=1.0,
                    min_lot=0.001, max_lot=50.0, lot_step=0.001, digits=2),
}


# ==================================================================
#                       نماذج البيانات (Dataclasses)
# ==================================================================
@dataclass
class SymbolSpec:
    """مواصفات رمز التداول."""
    name: str
    pip_size: float
    pip_value_per_lot: float   # قيمة النقطة لكل 1 لوت بالدولار
    contract_size: float
    min_lot: float
    max_lot: float
    lot_step: float
    digits: int

    @classmethod
    def from_dict(cls, name: str, d: Dict[str, Any]) -> "SymbolSpec":
        return cls(
            name=name,
            pip_size=float(d.get("pip_size", 0.0001)),
            pip_value_per_lot=float(d.get("pip_value_per_lot", 10.0)),
            contract_size=float(d.get("contract_size", 100_000.0)),
            min_lot=float(d.get("min_lot", 0.001)),
            max_lot=float(d.get("max_lot", 100.0)),
            lot_step=float(d.get("lot_step", 0.001)),
            digits=int(d.get("digits", 5)),
        )


@dataclass
class RiskConfig:
    """إعدادات المخاطر – كلها قابلة للتعديل من settings.yaml."""
    # المخاطرة لكل صفقة
    risk_per_trade_pct: float = 0.015        # 1.5% هدف
    max_risk_per_trade_pct: float = 0.02     # 2.0% سقف صارم

    # بوابات R:R
    min_rr_ratio: float = 1.5
    min_sl_pips: float = 3.0
    max_sl_pips: float = 500.0

    # سقوف التزامن في المحفظة
    max_open_positions: int = 3              # مناسب لحساب $50
    max_positions_per_symbol: int = 1
    max_same_direction_per_symbol: int = 1   # منع التحوط (hedging)

    # مخاطرة المحفظة الإجمالية
    max_portfolio_risk_pct: float = 0.06     # 6% مجموع مفتوح

    # حدود يومية
    max_daily_loss_pct: float = 0.05         # 5% حد يومي

    # سلسلة خسائر
    max_consecutive_losses: int = 3

    # fallback للوت الصغير
    allow_min_lot_fallback: bool = True
    min_lot_fallback_buffer: float = 1.5     # يسمح حتى 1.5x من max_risk

    # رصيد أدنى
    min_account_balance: float = 10.0


@dataclass
class LotSizingResult:
    """نتيجة حساب اللوت."""
    approved: bool
    lot_size: float
    risk_amount: float           # بالدولار
    effective_risk_pct: float
    reason: str = ""
    warning: Optional[str] = None
    adjusted: bool = False

    def __post_init__(self):
        self.lot_size = round(self.lot_size, 6)
        self.risk_amount = round(self.risk_amount, 6)
        self.effective_risk_pct = round(self.effective_risk_pct, 6)


@dataclass
class TradeValidationResult:
    """نتيجة التحقق الشامل قبل تنفيذ صفقة جديدة."""
    approved: bool
    reason: str
    lot_sizing: Optional[LotSizingResult] = None
    checks: Dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"approved={self.approved} reason={self.reason} "
            f"lot={self.lot_sizing.lot_size if self.lot_sizing else 0} "
            f"rr={self.checks.get('rr', 'n/a')}"
        )


# ==================================================================
#                       أدوات مساعدة
# ==================================================================
def _round_step_down(value: float, step: float) -> float:
    """تقريب DOWN إلى أقرب مضاعف للـ step (لأمان المخاطرة)."""
    if step <= 0:
        return value
    return math.floor(value / step + 1e-12) * step


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


# ==================================================================
#                        فئة RiskManager
# ==================================================================
class RiskManager:
    """
    مدير المخاطر المركزي. يُنشأ مرة واحدة في `main.py` ويُستدعى قبل كل صفقة.
    """

    def __init__(
        self,
        persistence: PersistenceManager,
        config: Optional[RiskConfig] = None,
        symbol_specs: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.persistence = persistence
        self.config = config or RiskConfig()

        # نسخة قابلة للتعديل من مواصفات الرموز
        self.symbol_specs: Dict[str, Dict[str, Any]] = dict(DEFAULT_SYMBOL_SPECS)
        if symbol_specs:
            for sym, spec in symbol_specs.items():
                key = sym.upper().replace("-", "/").replace("_", "/")
                self.symbol_specs[key] = spec

        logger.info(
            "RiskManager جاهز: risk=%.2f%%, max_risk=%.2f%%, min_rr=%.2f, "
            "max_open=%d, max_portfolio=%.2f%%, max_daily_loss=%.2f%%",
            self.config.risk_per_trade_pct * 100,
            self.config.max_risk_per_trade_pct * 100,
            self.config.min_rr_ratio,
            self.config.max_open_positions,
            self.config.max_portfolio_risk_pct * 100,
            self.config.max_daily_loss_pct * 100,
        )

    # ------------------------------------------------------------------
    # إنشاء من ملف إعدادات
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, persistence: PersistenceManager, cfg: Optional[dict]) -> "RiskManager":
        cfg = cfg or {}
        valid_fields = set(RiskConfig.__dataclass_fields__.keys())
        risk_kwargs = {k: v for k, v in cfg.items() if k in valid_fields}
        specs = cfg.get("symbol_specs") if isinstance(cfg, dict) else None
        return cls(persistence, RiskConfig(**risk_kwargs), specs)

    # ------------------------------------------------------------------
    # إدارة مواصفات الرموز
    # ------------------------------------------------------------------
    def register_symbol(self, symbol: str, spec: Dict[str, Any]) -> None:
        """إضافة/تحديث مواصفات رمز."""
        key = symbol.upper().replace("-", "/").replace("_", "/")
        self.symbol_specs[key] = spec
        logger.info("تم تسجيل مواصفات الرمز: %s", key)

    def get_spec(self, symbol: str) -> Optional[SymbolSpec]:
        """إرجاع مواصفات الرمز (مع تطبيع الاسم)."""
        if not symbol:
            return None
        key = symbol.upper().replace("-", "/").replace("_", "/")
        if key in self.symbol_specs:
            return SymbolSpec.from_dict(key, self.symbol_specs[key])
        return None

    # ------------------------------------------------------------------
    # 1) حساب حجم اللوت (Core)
    # ------------------------------------------------------------------
    def calculate_lot_size(
        self,
        symbol: str,
        entry_price: float,
        stop_loss: float,
        balance: float,
        risk_pct: Optional[float] = None,
    ) -> LotSizingResult:
        """
        حساب حجم اللوت بدقة nano-lot (0.001).

        المنطق:
          1. risk_amount = balance * risk_pct
          2. risk_per_lot = sl_pips * pip_value_per_lot
          3. raw_lot = risk_amount / risk_per_lot
          4. round DOWN إلى lot_step
          5. إذا raw_lot < min_lot:
             - fallback: استخدم min_lot إذا كان ضمن buffer الأمان
             - وإلا ارفض بلطف (بدون استثناء)
        """
        spec = self.get_spec(symbol)
        if spec is None:
            return LotSizingResult(False, 0.0, 0.0, 0.0, reason=f"unknown_symbol:{symbol}")

        entry = _safe_float(entry_price)
        sl = _safe_float(stop_loss)
        bal = _safe_float(balance)

        if bal <= 0:
            return LotSizingResult(False, 0.0, 0.0, 0.0, reason="invalid_balance")
        if bal < self.config.min_account_balance:
            return LotSizingResult(
                False, 0.0, 0.0, 0.0,
                reason=f"balance_below_min ({bal}<{self.config.min_account_balance})",
            )
        if entry <= 0 or sl <= 0:
            return LotSizingResult(False, 0.0, 0.0, 0.0, reason="invalid_prices")
        if abs(entry - sl) <= 1e-12:
            return LotSizingResult(False, 0.0, 0.0, 0.0, reason="zero_sl_distance")

        risk_pct = (
            _safe_float(risk_pct, self.config.risk_per_trade_pct)
            if risk_pct is not None
            else self.config.risk_per_trade_pct
        )
        if risk_pct <= 0:
            risk_pct = self.config.risk_per_trade_pct
        # لا تتجاوز السقف الصارم أبداً
        risk_pct = min(risk_pct, self.config.max_risk_per_trade_pct)

        risk_amount = bal * risk_pct
        sl_distance = abs(entry - sl)
        sl_pips = sl_distance / spec.pip_size

        if sl_pips < self.config.min_sl_pips:
            return LotSizingResult(
                False, 0.0, 0.0, 0.0,
                reason=f"sl_too_close ({sl_pips:.2f}p<{self.config.min_sl_pips}p)",
            )

        risk_per_lot = sl_pips * spec.pip_value_per_lot
        if risk_per_lot <= 0:
            return LotSizingResult(False, 0.0, 0.0, 0.0, reason="invalid_risk_per_lot")

        raw_lot = risk_amount / risk_per_lot

        # --- التقريب DOWN إلى step (0.001 مثلاً) ---
        stepped = _round_step_down(raw_lot, spec.lot_step)
        stepped = max(0.0, stepped)
        if stepped > spec.max_lot:
            stepped = _round_step_down(spec.max_lot, spec.lot_step)

        # --- حالة اللوت الصغير جداً ---
        if stepped < spec.min_lot:
            if not self.config.allow_min_lot_fallback:
                return LotSizingResult(
                    False, 0.0, 0.0, 0.0,
                    reason=f"lot_below_min ({raw_lot:.6f}<{spec.min_lot})",
                )

            # fallback: استخدم الحد الأدنى وتحقق من أن المخاطرة مقبولة
            fallback_lot = spec.min_lot
            eff_risk = fallback_lot * risk_per_lot
            eff_pct = eff_risk / bal

            buffer_cap = self.config.max_risk_per_trade_pct * self.config.min_lot_fallback_buffer
            if eff_pct > buffer_cap:
                return LotSizingResult(
                    False, 0.0, 0.0, 0.0,
                    reason=(
                        f"min_lot_risk_exceeds_buffer "
                        f"({eff_pct*100:.3f}%>{buffer_cap*100:.3f}%) – "
                        f"قلّل المخاطرة أو قرّب SL"
                    ),
                )

            return LotSizingResult(
                approved=True,
                lot_size=fallback_lot,
                risk_amount=eff_risk,
                effective_risk_pct=eff_pct,
                reason="min_lot_fallback",
                warning=(
                    f"استُخدم الحد الأدنى {fallback_lot} لوت "
                    f"(المخاطرة الفعلية {eff_pct*100:.3f}% بدلاً من {risk_pct*100:.3f}%)"
                ),
                adjusted=True,
            )

        # --- التحقق النهائي ---
        eff_risk = stepped * risk_per_lot
        eff_pct = eff_risk / bal

        if eff_pct > self.config.max_risk_per_trade_pct + 1e-9:
            return LotSizingResult(
                False, 0.0, 0.0, 0.0,
                reason=f"risk_above_cap ({eff_pct*100:.3f}%)",
            )

        return LotSizingResult(
            approved=True,
            lot_size=stepped,
            risk_amount=eff_risk,
            effective_risk_pct=eff_pct,
            reason="ok",
        )

    # ------------------------------------------------------------------
    # 2) التحقق الشامل قبل فتح صفقة
    # ------------------------------------------------------------------
    def validate_new_trade(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        balance: float,
        open_positions: Optional[List[Any]] = None,
        risk_pct: Optional[float] = None,
    ) -> TradeValidationResult:
        """
        بوابة أمان واحدة تجمع كل الفحوصات.
        `open_positions`: قائمة كائنات LivePosition (يمكن تمرير
                         `list(exit_manager.open_positions.values())`).
        """
        open_positions = open_positions or []
        checks: Dict[str, Any] = {}

        # --- (0) صحة المدخلات ---
        if not symbol or side not in ("buy", "sell"):
            return TradeValidationResult(False, "invalid_side_or_symbol", checks=checks)

        entry = _safe_float(entry_price)
        sl = _safe_float(stop_loss)
        tp = _safe_float(take_profit)
        bal = _safe_float(balance)

        if entry <= 0 or sl <= 0 or tp <= 0:
            return TradeValidationResult(False, "invalid_prices", checks=checks)
        if bal < self.config.min_account_balance:
            return TradeValidationResult(
                False, f"balance_below_min ({bal})", checks=checks
            )

        # --- (1) اتجاه SL/TP ---
        if side == "buy":
            if sl >= entry:
                return TradeValidationResult(False, "sl_wrong_side (buy: sl>=entry)", checks=checks)
            if tp <= entry:
                return TradeValidationResult(False, "tp_wrong_side (buy: tp<=entry)", checks=checks)
        else:
            if sl <= entry:
                return TradeValidationResult(False, "sl_wrong_side (sell: sl<=entry)", checks=checks)
            if tp >= entry:
                return TradeValidationResult(False, "tp_wrong_side (sell: tp>=entry)", checks=checks)

        # --- (2) بوابة R:R ---
        risk_distance = abs(entry - sl)
        reward_distance = abs(tp - entry)
        rr = reward_distance / risk_distance if risk_distance > 0 else 0.0
        checks["rr"] = round(rr, 4)
        if rr < self.config.min_rr_ratio:
            return TradeValidationResult(
                False, f"rr_below_min ({rr:.2f}<{self.config.min_rr_ratio})", checks=checks
            )

        # --- (3) مسافة SL بالـ pips ---
        spec = self.get_spec(symbol)
        if spec is None:
            return TradeValidationResult(False, f"unknown_symbol:{symbol}", checks=checks)
        sl_pips = risk_distance / spec.pip_size
        checks["sl_pips"] = round(sl_pips, 4)
        if sl_pips < self.config.min_sl_pips:
            return TradeValidationResult(
                False, f"sl_too_close ({sl_pips:.2f}p)", checks=checks
            )
        if sl_pips > self.config.max_sl_pips:
            return TradeValidationResult(
                False, f"sl_too_wide ({sl_pips:.2f}p>{self.config.max_sl_pips}p)", checks=checks
            )

        # --- (4) سقوف التزامن ---
        total_open = len(open_positions)
        same_symbol = [p for p in open_positions if getattr(p, "symbol", None) == symbol]
        checks["open_total"] = total_open
        checks["open_same_symbol"] = len(same_symbol)

        if total_open >= self.config.max_open_positions:
            return TradeValidationResult(
                False, f"max_open_positions ({total_open}>={self.config.max_open_positions})",
                checks=checks,
            )
        if len(same_symbol) >= self.config.max_positions_per_symbol:
            return TradeValidationResult(
                False, f"max_positions_per_symbol ({symbol})", checks=checks
            )

        # منع التحوط: لا صفقة معاكسة على نفس الرمز
        opposing = [p for p in same_symbol if getattr(p, "side", None) != side]
        if opposing and self.config.max_same_direction_per_symbol >= 1:
            return TradeValidationResult(
                False, f"hedging_not_allowed ({symbol} لديه صفقة معاكسة)", checks=checks
            )

        # --- (5) المخاطرة الإجمالية للمحفظة ---
        port_risk_pct = self._compute_portfolio_risk_pct(open_positions, bal)
        checks["portfolio_risk_pct_current"] = round(port_risk_pct, 6)

        # --- (6) حساب اللوت ---
        ls = self.calculate_lot_size(symbol, entry, sl, bal, risk_pct)
        checks["lot_sizing"] = {
            "lot_size": ls.lot_size,
            "risk_amount": ls.risk_amount,
            "effective_risk_pct": ls.effective_risk_pct,
            "reason": ls.reason,
            "warning": ls.warning,
        }
        if not ls.approved:
            return TradeValidationResult(
                False, f"lot_sizing_failed:{ls.reason}", lot_sizing=ls, checks=checks
            )

        new_port_risk_pct = port_risk_pct + ls.effective_risk_pct
        checks["portfolio_risk_pct_after"] = round(new_port_risk_pct, 6)
        if new_port_risk_pct > self.config.max_portfolio_risk_pct + 1e-9:
            return TradeValidationResult(
                False,
                f"portfolio_risk_exceeded "
                f"({new_port_risk_pct*100:.2f}%>{self.config.max_portfolio_risk_pct*100:.2f}%)",
                lot_sizing=ls, checks=checks,
            )

        # --- (7) حد الخسارة اليومية ---
        daily_pnl, daily_pnl_pct = self._get_daily_pnl(bal)
        checks["daily_pnl"] = round(daily_pnl, 4)
        checks["daily_pnl_pct"] = round(daily_pnl_pct, 6)
        if daily_pnl_pct <= -self.config.max_daily_loss_pct + 1e-9:
            return TradeValidationResult(
                False,
                f"daily_loss_limit_hit ({daily_pnl_pct*100:.2f}%)",
                lot_sizing=ls, checks=checks,
            )

        # --- (8) سلسلة خسائر متتالية ---
        cooldown_hit, streak = self._check_consecutive_losses()
        checks["consecutive_losses"] = streak
        if cooldown_hit:
            return TradeValidationResult(
                False,
                f"consecutive_losses_cooldown ({streak}>={self.config.max_consecutive_losses})",
                lot_sizing=ls, checks=checks,
            )

        # --- اجتاز كل الفحوصات ---
        return TradeValidationResult(True, "approved", lot_sizing=ls, checks=checks)

    # ------------------------------------------------------------------
    # 3) بوابات مستقلة يمكن استدعاؤها منفصلة
    # ------------------------------------------------------------------
    def can_open_position(
        self, open_positions: List[Any], symbol: Optional[str] = None
    ) -> Tuple[bool, str]:
        """فحص سقوف التزامن فقط."""
        open_positions = open_positions or []
        if len(open_positions) >= self.config.max_open_positions:
            return False, f"max_open_positions ({len(open_positions)})"
        if symbol:
            same = [p for p in open_positions if getattr(p, "symbol", None) == symbol]
            if len(same) >= self.config.max_positions_per_symbol:
                return False, f"max_positions_per_symbol ({symbol})"
        return True, "ok"

    def check_rr_gate(
        self, entry: float, sl: float, tp: float
    ) -> Tuple[bool, float]:
        """فحص نسبة R:R فقط."""
        risk = abs(_safe_float(entry) - _safe_float(sl))
        reward = abs(_safe_float(tp) - _safe_float(entry))
        if risk <= 0:
            return False, 0.0
        rr = reward / risk
        return rr >= self.config.min_rr_ratio, rr

    def check_daily_loss(self, balance: float) -> Tuple[bool, float, float]:
        """فحص حد الخسارة اليومية. يعيد (ok, pnl_usd, pnl_pct)."""
        pnl, pct = self._get_daily_pnl(_safe_float(balance))
        ok = pct > -self.config.max_daily_loss_pct + 1e-9
        return ok, pnl, pct

    def check_portfolio_risk(
        self,
        open_positions: List[Any],
        balance: float,
        additional_risk_pct: float = 0.0,
    ) -> Tuple[bool, float]:
        """فحص المخاطرة الإجمالية للمحفظة (اختياري: إضافة صفقة جديدة)."""
        current = self._compute_portfolio_risk_pct(open_positions, _safe_float(balance))
        total = current + max(0.0, additional_risk_pct)
        ok = total <= self.config.max_portfolio_risk_pct + 1e-9
        return ok, total

    def check_consecutive_losses(self) -> Tuple[bool, int]:
        """فحص سلسلة الخسائر (cooldown)."""
        return self._check_consecutive_losses()

    # ------------------------------------------------------------------
    # 4) دوال داخلية
    # ------------------------------------------------------------------
    def _compute_portfolio_risk_pct(
        self, open_positions: List[Any], balance: float
    ) -> float:
        """
        المخاطرة الحالية = مجموع مخاطرة كل صفقة مفتوحة إذا ضُرب SL الحالي.
        يستخدم current_sl (بعد Breakeven/TP1 قد تكون المخاطرة صفراً).
        """
        if balance <= 0 or not open_positions:
            return 0.0

        total_risk_usd = 0.0
        for pos in open_positions:
            spec = self.get_spec(getattr(pos, "symbol", ""))
            if spec is None:
                continue
            entry = _safe_float(getattr(pos, "entry_price", 0.0))
            # نستخدم current_sl إن وُجد، وإلا initial_sl
            sl = _safe_float(getattr(pos, "current_sl", None)
                             or getattr(pos, "initial_sl", 0.0))
            lot = _safe_float(getattr(pos, "lot_size", 0.0))
            if entry <= 0 or sl <= 0 or lot <= 0:
                continue
            # بعد Breakeven: SL == entry -> risk = 0
            distance = abs(entry - sl)
            pips = distance / spec.pip_size
            total_risk_usd += pips * spec.pip_value_per_lot * lot

        return total_risk_usd / balance if balance > 0 else 0.0

    def _get_daily_pnl(self, balance: float) -> Tuple[float, float]:
        """
        حساب P&L المحقق اليوم من closed_trades (تقديري بالدولار).
        يعيد (pnl_usd, pnl_pct).
        """
        if balance <= 0:
            return 0.0, 0.0

        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        try:
            trades = self.persistence.get_closed_trades(limit=500)
        except Exception as e:
            logger.warning("تعذّر قراءة الصفقات المغلقة لحساب P&L اليومي: %s", e)
            return 0.0, 0.0

        pnl = 0.0
        for t in trades:
            try:
                close_time_raw = t["close_time"]
                close_dt = datetime.fromisoformat(close_time_raw)
                if close_dt.tzinfo is None:
                    close_dt = close_dt.replace(tzinfo=timezone.utc)
                if close_dt < today_start:
                    continue
                pnl += self._estimate_trade_pnl(t)
            except Exception:
                # تخطي أي صفقة تالفة
                continue

        pct = pnl / balance if balance > 0 else 0.0
        return pnl, pct

    def _estimate_trade_pnl(self, row: Any) -> float:
        """تقدير P&L لصفقة مغلقة (row من sqlite3.Row)."""
        try:
            symbol = row["symbol"]
            spec = self.get_spec(symbol)
            if spec is None:
                return 0.0
            entry = _safe_float(row["entry_price"])
            initial_sl = _safe_float(row["initial_sl"])
            lot = _safe_float(row["lot_size"])
            realized_r = _safe_float(row["realized_r"])
            sl_distance = abs(entry - initial_sl)
            if sl_distance <= 0 or lot <= 0:
                return 0.0
            pips = sl_distance / spec.pip_size
            risk_per_lot = pips * spec.pip_value_per_lot
            return risk_per_lot * lot * realized_r
        except Exception:
            return 0.0

    def _check_consecutive_losses(self) -> Tuple[bool, int]:
        """يعيد (cooldown_hit, streak)."""
        try:
            trades = self.persistence.get_closed_trades(
                limit=max(self.config.max_consecutive_losses, 1)
            )
        except Exception as e:
            logger.warning("تعذّر فحص سلسلة الخسائر: %s", e)
            return False, 0

        streak = 0
        for t in trades:
            try:
                if _safe_float(t["realized_r"]) < 0:
                    streak += 1
                else:
                    break
            except Exception:
                break

        return streak >= self.config.max_consecutive_losses, streak

    # ------------------------------------------------------------------
    # 5) مساعدات عرض/إحصائيات
    # ------------------------------------------------------------------
    def get_risk_summary(
        self, open_positions: List[Any], balance: float
    ) -> Dict[str, Any]:
        """ملخص المخاطر الحالي (مفيد للوحة التحكم / اللوجات)."""
        port_pct = self._compute_portfolio_risk_pct(open_positions or [], balance)
        daily_pnl, daily_pct = self._get_daily_pnl(_safe_float(balance))
        cooldown, streak = self._check_consecutive_losses()
        return {
            "balance": round(_safe_float(balance), 2),
            "open_positions": len(open_positions or []),
            "max_open_positions": self.config.max_open_positions,
            "portfolio_risk_pct": round(port_pct, 6),
            "max_portfolio_risk_pct": self.config.max_portfolio_risk_pct,
            "portfolio_risk_headroom_pct": round(
                max(0.0, self.config.max_portfolio_risk_pct - port_pct), 6
            ),
            "daily_pnl_usd": round(daily_pnl, 4),
            "daily_pnl_pct": round(daily_pct, 6),
            "max_daily_loss_pct": self.config.max_daily_loss_pct,
            "consecutive_losses": streak,
            "max_consecutive_losses": self.config.max_consecutive_losses,
            "cooldown_active": cooldown,
        }
