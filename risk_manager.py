# risk_manager.py
"""
Risk Manager — strict lot sizing and safety gates.
NO trade is executed without passing every gate.

Developed & Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from analysis_engine import SYMBOL_SPECS

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)



  @dataclass
class RiskConfig:
    risk_per_trade_pct: float = 1.5
    max_risk_per_trade_pct: float = 2.0
    min_rr: float = 1.5
    max_rr: float = 6.0              # 👈 جديد: رفض أي R:R أكبر من 1:6
    min_sl_pips: float = 5.0
    max_sl_pips: float = 500.0 
    max_open_positions: int = 3
    max_positions_per_symbol: int = 1
    max_portfolio_risk_pct: float = 6.0
    max_daily_loss_pct: float = 5.0
    max_consecutive_losses: int = 3
    min_account_balance: float = 10.0


@dataclass
class TradeDecision:
    approved: bool
    reason: str
    lot_size: float = 0.0
    risk_usd: float = 0.0
    risk_pct: float = 0.0
    reward_usd: float = 0.0
    rr: float = 0.0
    checks: Dict[str, Any] = field(default_factory=dict)


def _floor_step(v: float, step: float) -> float:
    if step <= 0:
        return v
    return math.floor(v / step + 1e-12) * step


class RiskManager:
    """Central risk gate — every recommendation must pass through here."""

    def __init__(self, config: Optional[RiskConfig] = None):
        self.cfg = config or RiskConfig()

    # ------------------------------------------------------------------
    def _spec(self, symbol: str) -> Optional[Dict[str, float]]:
        return SYMBOL_SPECS.get(symbol)

    def calc_lot(self, symbol: str, entry: float, sl: float,
                 balance: float, risk_pct: Optional[float] = None
                 ) -> Tuple[bool, float, float, float, str]:
        spec = self._spec(symbol)
        if not spec:
            return False, 0.0, 0.0, 0.0, f"unknown_symbol:{symbol}"

        if balance <= 0 or balance < self.cfg.min_account_balance:
            return False, 0.0, 0.0, 0.0, f"balance_too_low ({balance:.2f})"
        if entry <= 0 or sl <= 0:
            return False, 0.0, 0.0, 0.0, "invalid_prices"
        if abs(entry - sl) <= 1e-12:
            return False, 0.0, 0.0, 0.0, "zero_sl"

        rp = risk_pct if risk_pct is not None else self.cfg.risk_per_trade_pct
        rp = min(rp, self.cfg.max_risk_per_trade_pct)

        risk_amount = balance * (rp / 100.0)
        sl_pips = abs(entry - sl) / spec["pip_size"]

        if sl_pips < self.cfg.min_sl_pips:
            return False, 0.0, 0.0, 0.0, f"sl_too_close ({sl_pips:.1f}p)"
        if sl_pips > self.cfg.max_sl_pips:
            return False, 0.0, 0.0, 0.0, f"sl_too_wide ({sl_pips:.1f}p)"

        risk_per_lot = sl_pips * spec["pip_value_per_lot"]
        if risk_per_lot <= 0:
            return False, 0.0, 0.0, 0.0, "invalid_risk_per_lot"

        raw_lot = risk_amount / risk_per_lot
        lot = _floor_step(raw_lot, spec["lot_step"])

        if lot < spec["min_lot"]:
            # Attempt min-lot fallback within buffer
            eff = spec["min_lot"] * risk_per_lot
            eff_pct = (eff / balance) * 100.0
            if eff_pct <= self.cfg.max_risk_per_trade_pct * 1.5:
                return True, spec["min_lot"], eff, eff_pct, "min_lot_fallback"
            return False, 0.0, 0.0, 0.0, f"lot_below_min ({raw_lot:.6f})"

        eff_usd = lot * risk_per_lot
        eff_pct = (eff_usd / balance) * 100.0
        if eff_pct > self.cfg.max_risk_per_trade_pct + 1e-9:
            return False, 0.0, 0.0, 0.0, f"risk_above_cap ({eff_pct:.2f}%)"

        return True, lot, eff_usd, eff_pct, "ok"

    # ------------------------------------------------------------------
    def validate_trade(
        self,
        symbol: str,
        direction: str,
        entry: float,
        sl: float,
        tp: float,
        balance: float,
        open_positions: List[Dict[str, Any]],
        closed_trades_today: List[Dict[str, Any]],
        consecutive_losses: int,
    ) -> TradeDecision:
        checks: Dict[str, Any] = {}

        # 1) Direction sanity
        if direction not in ("BUY", "SELL"):
            return TradeDecision(False, "invalid_direction", checks=checks)

        # 2) SL/TP side
        if direction == "BUY":
            if sl >= entry:
                return TradeDecision(False, "sl_wrong_side", checks=checks)
            if tp <= entry:
                return TradeDecision(False, "tp_wrong_side", checks=checks)
        else:
            if sl <= entry:
                return TradeDecision(False, "sl_wrong_side", checks=checks)
            if tp >= entry:
                return TradeDecision(False, "tp_wrong_side", checks=checks)

        # 3) R:R
       # rr = abs(tp - entry) / max(abs(entry - sl), 1e-9)
       # checks["rr"] = round(rr, 2)
        #if rr < self.cfg.min_rr:
        #    return TradeDecision(False, f"rr_below_min ({rr:.2f})", checks=checks)

        # 3) R:R
        rr = abs(tp - entry) / max(abs(entry - sl), 1e-9)
        checks["rr"] = round(rr, 2)
       # 👇 جديد: منع R:R الخيالي
       if rr > self.cfg.max_rr:
       return TradeDecision(False, f"rr_too_high ({rr:.2f} > {self.cfg.max_rr})", checks=checks)
        

        
        # 4) Concurrency
        if len(open_positions) >= self.cfg.max_open_positions:
            return TradeDecision(False, f"max_open ({len(open_positions)})", checks=checks)
        same = [p for p in open_positions if p.get("symbol") == symbol]
        if len(same) >= self.cfg.max_positions_per_symbol:
            return TradeDecision(False, f"max_per_symbol ({symbol})", checks=checks)
        opposing = [p for p in same if p.get("side") != direction.lower()]
        if opposing:
            return TradeDecision(False, f"hedging_blocked ({symbol})", checks=checks)

        # 5) Lot sizing
        ok, lot, risk_usd, risk_pct, reason = self.calc_lot(symbol, entry, sl, balance)
        checks["lot"] = {"lot_size": lot, "risk_usd": round(risk_usd, 4),
                         "risk_pct": round(risk_pct, 4), "reason": reason}
        if not ok:
            return TradeDecision(False, reason, checks=checks)

        # 6) Portfolio risk
        current_port = self._portfolio_risk_pct(open_positions, balance)
        after = current_port + risk_pct
        checks["portfolio_risk_pct"] = round(current_port, 3)
        checks["portfolio_risk_after"] = round(after, 3)
        if after > self.cfg.max_portfolio_risk_pct:
            return TradeDecision(False, f"portfolio_risk_exceeded ({after:.2f}%)",
                                 checks=checks)

        # 7) Daily loss limit
        daily_pnl = sum(float(t.get("realized_pnl_usd", 0.0)) for t in closed_trades_today)
        daily_pct = (daily_pnl / balance) * 100.0 if balance > 0 else 0.0
        checks["daily_pnl_usd"] = round(daily_pnl, 2)
        checks["daily_pnl_pct"] = round(daily_pct, 2)
        if daily_pct <= -self.cfg.max_daily_loss_pct:
            return TradeDecision(False, f"daily_loss_limit ({daily_pct:.2f}%)", checks=checks)

        # 8) Consecutive losses
        checks["consecutive_losses"] = consecutive_losses
        if consecutive_losses >= self.cfg.max_consecutive_losses:
            return TradeDecision(False, f"cooldown ({consecutive_losses} losses)", checks=checks)

        reward_usd = risk_usd * rr
        return TradeDecision(
            approved=True,
            reason="approved",
            lot_size=lot,
            risk_usd=round(risk_usd, 4),
            risk_pct=round(risk_pct, 4),
            reward_usd=round(reward_usd, 4),
            rr=round(rr, 2),
            checks=checks,
        )

    # ------------------------------------------------------------------
    def _portfolio_risk_pct(self, open_positions: List[Dict[str, Any]],
                            balance: float) -> float:
        if balance <= 0 or not open_positions:
            return 0.0
        total = 0.0
        for p in open_positions:
            spec = self._spec(p.get("symbol", ""))
            if not spec:
                continue
            entry = float(p.get("entry_price", 0.0))
            sl = float(p.get("current_sl", 0.0))
            lot = float(p.get("lot_size", 0.0))
            if entry <= 0 or sl <= 0 or lot <= 0:
                continue
            pips = abs(entry - sl) / spec["pip_size"]
            total += pips * spec["pip_value_per_lot"] * lot
        return (total / balance) * 100.0
