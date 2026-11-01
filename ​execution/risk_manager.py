# execution/risk_manager.py
"""
execution/risk_manager.py

Position sizing and concurrent-trade gating.

Design goals:
  * Deterministic lot sizing from (capital, risk_pct, SL distance, pip value).
  * Hard enforcement of the automation mode: 1 trade ("single") or up to 3
    concurrent trades ("multi"), configurable via config.AUTOMATION_MODES.
  * Fail-loud on min-lot overruns: with $50 capital and 1.5% risk ($0.75),
    a 20-pip SL on EUR/USD at min lot (0.01) risks ~$2 — more than budget.
    By default such trades are REJECTED with a clear reason, rather than
    silently over-risking. Set `strict_min_lot=False` to allow over-risked
    trades flagged as such.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

try:
    from config import DEFAULT_CAPITAL, RISK_PER_TRADE, AUTOMATION_MODES, ASSETS  # type: ignore
except Exception:  # pragma: no cover
    DEFAULT_CAPITAL = 50.0
    RISK_PER_TRADE = 0.015
    AUTOMATION_MODES = {
        "single": {"max_concurrent_trades": 1},
        "multi": {"max_concurrent_trades": 3},
    }
    ASSETS = [
        {"symbol": "XAU/USD", "pip_size": 0.01, "contract_size": 100},
        {"symbol": "XAG/USD", "pip_size": 0.001, "contract_size": 5000},
        {"symbol": "WTI/USD", "pip_size": 0.01, "contract_size": 1000},
        {"symbol": "EUR/USD", "pip_size": 0.0001, "contract_size": 100000},
        {"symbol": "GBP/USD", "pip_size": 0.0001, "contract_size": 100000},
        {"symbol": "USD/JPY", "pip_size": 0.01, "contract_size": 100000},
    ]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Symbol specs
# ---------------------------------------------------------------------
def pip_value_per_lot_usd(
    symbol: str, pip_size: float, contract_size: float, usdjpy_rate: float = 150.0,
) -> float:
    """
    Approximate USD value of 1 pip for a standard 1.0 lot.

    For EUR/USD, GBP/USD, XAU/USD, XAG/USD, WTI/USD the quote currency is USD,
    so value = pip_size * contract_size. For USD/JPY, the JPY pip value must be
    divided by the current USD/JPY rate to express it in USD.
    """
    if symbol.upper() == "USD/JPY":
        return (pip_size * contract_size) / max(usdjpy_rate, 1e-9)
    return pip_size * contract_size


@dataclass
class SymbolSpec:
    symbol: str
    pip_size: float
    contract_size: float
    pip_value_per_lot: float
    min_lot: float = 0.01          # micro lot
    max_lot: float = 100.0
    lot_step: float = 0.01
    one_position_per_symbol: bool = True

    @classmethod
    def from_asset(
        cls, asset: dict, usdjpy_rate: float = 150.0, min_lot: float = 0.01,
        lot_step: float = 0.01,
    ) -> "SymbolSpec":
        return cls(
            symbol=asset["symbol"],
            pip_size=asset["pip_size"],
            contract_size=asset["contract_size"],
            pip_value_per_lot=pip_value_per_lot_usd(
                asset["symbol"], asset["pip_size"], asset["contract_size"], usdjpy_rate,
            ),
            min_lot=min_lot,
            lot_step=lot_step,
        )


DEFAULT_SPECS: Dict[str, SymbolSpec] = {
    a["symbol"]: SymbolSpec.from_asset(a) for a in ASSETS
}


# ---------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------
@dataclass
class OpenPosition:
    position_id: str
    symbol: str
    direction: str               # "long" | "short"
    lots: float
    entry: float
    stop_loss: float
    take_profit: float
    risk_usd: float
    reward_usd: float
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    strategy_id: str = ""

    @property
    def rr(self) -> float:
        return self.reward_usd / self.risk_usd if self.risk_usd > 0 else 0.0


@dataclass
class SizingDecision:
    approved: bool
    reason: str
    symbol: str = ""
    direction: str = ""
    lots: float = 0.0
    risk_usd: float = 0.0
    reward_usd: float = 0.0
    risk_pct_of_capital: float = 0.0
    pip_distance_sl: float = 0.0
    pip_distance_tp: float = 0.0
    pip_value_per_lot: float = 0.0
    min_lot_capped: bool = False   # True if min lot forced us over budget
    over_risk: bool = False         # True if actual risk > budget


def _floor_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    n = math.floor(x / step + 1e-12)
    return round(n * step, 8)


# ---------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------
class RiskManager:
    """
    Sizes trades and enforces concurrent-trade constraints.

    Typical flow:
        decision = risk_manager.size(symbol, direction, entry, sl, tp)
        if decision.approved:
            pos = risk_manager.reserve(decision, strategy_id="fvg")
        ...
        risk_manager.release(pos.position_id, r_multiple=+2.0)
    """

    def __init__(
        self,
        capital: float = DEFAULT_CAPITAL,
        risk_per_trade: float = RISK_PER_TRADE,
        automation_mode: str = "single",
        specs: Optional[Dict[str, SymbolSpec]] = None,
        strict_min_lot: bool = True,
        max_portfolio_risk: Optional[float] = None,
        usdjpy_rate: float = 150.0,
    ) -> None:
        if automation_mode not in AUTOMATION_MODES:
            raise ValueError(
                f"Unknown automation_mode {automation_mode!r}; "
                f"choose from {list(AUTOMATION_MODES)}"
            )
        self.capital = float(capital)
        self.risk_per_trade = float(risk_per_trade)
        self.automation_mode = automation_mode
        self.max_concurrent = AUTOMATION_MODES[automation_mode]["max_concurrent_trades"]
        self.strict_min_lot = strict_min_lot
        self.usdjpy_rate = usdjpy_rate

        if specs is None:
            specs = {k: SymbolSpec(**vars(v)) for k, v in DEFAULT_SPECS.items()}
        self.specs = specs

        # Portfolio risk cap; defaults to capital * risk_per_trade * max_concurrent.
        self.max_portfolio_risk = (
            max_portfolio_risk
            if max_portfolio_risk is not None
            else self.capital * self.risk_per_trade * self.max_concurrent
        )

        self._open: Dict[str, OpenPosition] = {}

    # -- state -------------------------------------------------------------
    @property
    def open_positions(self) -> List[OpenPosition]:
        return list(self._open.values())

    @property
    def open_risk_usd(self) -> float:
        return sum(p.risk_usd for p in self._open.values())

    def set_mode(self, mode: str) -> None:
        if mode not in AUTOMATION_MODES:
            raise ValueError(f"Unknown automation_mode {mode!r}")
        self.automation_mode = mode
        self.max_concurrent = AUTOMATION_MODES[mode]["max_concurrent_trades"]
        self.max_portfolio_risk = (
            self.capital * self.risk_per_trade * self.max_concurrent
        )

    def update_capital(self, new_capital: float) -> None:
        self.capital = max(0.0, float(new_capital))
        self.max_portfolio_risk = (
            self.capital * self.risk_per_trade * self.max_concurrent
        )

    # -- concurrency gate --------------------------------------------------
    def can_open_new_trade(self, symbol: Optional[str] = None) -> Tuple[bool, str]:
        if len(self._open) >= self.max_concurrent:
            return False, (
                f"max concurrent trades reached ({len(self._open)}/{self.max_concurrent}) "
                f"in mode {self.automation_mode!r}"
            )
        if symbol and symbol in {p.symbol for p in self._open.values()}:
            spec = self.specs.get(symbol)
            if spec is not None and spec.one_position_per_symbol:
                return False, f"a position is already open on {symbol}"
        if self.open_risk_usd >= self.max_portfolio_risk - 1e-9:
            return False, (
                f"portfolio risk cap hit "
                f"(${self.open_risk_usd:.2f} >= ${self.max_portfolio_risk:.2f})"
            )
        return True, ""

    # -- sizing ------------------------------------------------------------
    def size(
        self,
        symbol: str,
        direction: str,
        entry: float,
        stop_loss: float,
        take_profit: float,
    ) -> SizingDecision:
        spec = self.specs.get(symbol)
        if spec is None:
            return SizingDecision(False, f"no symbol spec for {symbol!r}")

        if direction not in ("long", "short"):
            return SizingDecision(False, f"invalid direction {direction!r}")

        # Direction-consistency checks
        if direction == "long" and stop_loss >= entry:
            return SizingDecision(False, "long stop_loss must be below entry")
        if direction == "long" and take_profit <= entry:
            return SizingDecision(False, "long take_profit must be above entry")
        if direction == "short" and stop_loss <= entry:
            return SizingDecision(False, "short stop_loss must be above entry")
        if direction == "short" and take_profit >= entry:
            return SizingDecision(False, "short take_profit must be below entry")

        # Concurrency gate
        ok, reason = self.can_open_new_trade(symbol)
        if not ok:
            return SizingDecision(False, reason, symbol=symbol, direction=direction)

        sl_distance = abs(entry - stop_loss)
        tp_distance = abs(take_profit - entry)
        sl_pips = sl_distance / spec.pip_size
        tp_pips = tp_distance / spec.pip_size

        risk_budget = self.capital * self.risk_per_trade
        risk_per_lot = sl_pips * spec.pip_value_per_lot

        if risk_per_lot <= 0:
            return SizingDecision(
                False, "invalid risk_per_lot (zero SL distance or pip value)",
                symbol=symbol, direction=direction,
            )

        ideal_lots = risk_budget / risk_per_lot
        lots = _floor_step(ideal_lots, spec.lot_step)

        over_risk = False
        min_lot_capped = False
        if lots < spec.min_lot:
            min_lot_capped = True
            lots = spec.min_lot
            actual_risk = lots * risk_per_lot
            over_risk = actual_risk > risk_budget + 1e-9
            if over_risk and self.strict_min_lot:
                return SizingDecision(
                    False,
                    (
                        f"min lot {spec.min_lot} would risk ${actual_risk:.2f} "
                        f"({actual_risk / self.capital:.2%} of capital), "
                        f"exceeding risk budget ${risk_budget:.2f} "
                        f"({self.risk_per_trade:.2%})"
                    ),
                    symbol=symbol, direction=direction, lots=spec.min_lot,
                    risk_usd=actual_risk,
                    reward_usd=spec.min_lot * tp_pips * spec.pip_value_per_lot,
                    risk_pct_of_capital=actual_risk / self.capital,
                    pip_distance_sl=sl_pips, pip_distance_tp=tp_pips,
                    pip_value_per_lot=spec.pip_value_per_lot,
                    min_lot_capped=True, over_risk=True,
                )

        if lots > spec.max_lot:
            lots = spec.max_lot

        actual_risk = lots * risk_per_lot
        actual_reward = lots * tp_pips * spec.pip_value_per_lot

        # Per-trade budget guard (independent of min lot)
        if actual_risk > risk_budget + 1e-9:
            if self.strict_min_lot:
                return SizingDecision(
                    False,
                    f"risk ${actual_risk:.2f} exceeds budget ${risk_budget:.2f}",
                    symbol=symbol, direction=direction, lots=lots,
                    risk_usd=actual_risk, reward_usd=actual_reward,
                    risk_pct_of_capital=actual_risk / self.capital,
                    pip_distance_sl=sl_pips, pip_distance_tp=tp_pips,
                    pip_value_per_lot=spec.pip_value_per_lot,
                    min_lot_capped=min_lot_capped, over_risk=True,
                )
            over_risk = True

        # Portfolio risk guard
        if self.open_risk_usd + actual_risk > self.max_portfolio_risk + 1e-9:
            return SizingDecision(
                False,
                (
                    f"portfolio risk would exceed cap: open ${self.open_risk_usd:.2f} + "
                    f"new ${actual_risk:.2f} > ${self.max_portfolio_risk:.2f}"
                ),
                symbol=symbol, direction=direction, lots=lots,
                risk_usd=actual_risk, reward_usd=actual_reward,
                risk_pct_of_capital=actual_risk / self.capital,
                pip_distance_sl=sl_pips, pip_distance_tp=tp_pips,
                pip_value_per_lot=spec.pip_value_per_lot,
                min_lot_capped=min_lot_capped, over_risk=over_risk,
            )

        return SizingDecision(
            True,
            "ok",
            symbol=symbol, direction=direction, lots=lots,
            risk_usd=actual_risk, reward_usd=actual_reward,
            risk_pct_of_capital=actual_risk / self.capital,
            pip_distance_sl=sl_pips, pip_distance_tp=tp_pips,
            pip_value_per_lot=spec.pip_value_per_lot,
            min_lot_capped=min_lot_capped, over_risk=over_risk,
        )

    # -- reserve/release ---------------------------------------------------
    def reserve(
        self,
        decision: SizingDecision,
        entry: float,
        stop_loss: float,
        take_profit: float,
        strategy_id: str = "",
    ) -> OpenPosition:
        if not decision.approved:
            raise ValueError(f"Cannot reserve rejected decision: {decision.reason}")
        pos = OpenPosition(
            position_id=uuid.uuid4().hex[:10],
            symbol=decision.symbol,
            direction=decision.direction,
            lots=decision.lots,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_usd=decision.risk_usd,
            reward_usd=decision.reward_usd,
            strategy_id=strategy_id,
        )
        self._open[pos.position_id] = pos
        logger.info(
            "Reserved %s %s %.2f lots, risk $%.2f, R:R 1:%.2f",
            pos.symbol, pos.direction, pos.lots, pos.risk_usd, pos.rr,
        )
        return pos

    def release(self, position_id: str, r_multiple: float) -> Optional[float]:
        """Close a position, update capital with realized P&L, return P&L USD."""
        pos = self._open.pop(position_id, None)
        if pos is None:
            logger.warning("release(): unknown position_id %s", position_id)
            return None
        pnl = pos.risk_usd * r_multiple
        self.update_capital(self.capital + pnl)
        logger.info(
            "Closed %s with %+0.2fR -> P&L $%.2f, capital $%.2f",
            pos.symbol, r_multiple, pnl, self.capital,
        )
        return pnl

    def snapshot(self) -> dict:
        return {
            "capital": round(self.capital, 4),
            "risk_per_trade": self.risk_per_trade,
            "automation_mode": self.automation_mode,
            "max_concurrent": self.max_concurrent,
            "open_count": len(self._open),
            "open_risk_usd": round(self.open_risk_usd, 4),
            "max_portfolio_risk": round(self.max_portfolio_risk, 4),
            "open_positions": [vars(p) for p in self._open.values()],
        }
