# execution/broker_connector.py
"""
execution/broker_connector.py

Order execution and transparent trade tickets.

Design:
  * `TradeTicket` – the single source of truth for an executed trade.
    Contains everything a human or audit log needs to verify the decision:
    symbol, side, strategy, score, entry, SL, TP, lots, pip math, risk,
    reward, R:R, spread, commission, slippage, reason, timestamps.
  * `BrokerConnector` – abstract interface.
  * `SimulatedBroker` – deterministic paper-trading broker using a
    price provider (bid/ask) and applying spread + slippage + commission.
  * `PlaceholderBroker` – fails fast until a real adapter is wired.

Wire real brokers by subclassing BrokerConnector and overriding
`_submit_to_broker()` and `_close_on_broker()`.
"""

from __future__ import annotations

import abc
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

try:
    from analysis.ict_engine import Direction  # type: ignore
except Exception:  # pragma: no cover
    class Direction:
        LONG = "long"; SHORT = "short"; NEUTRAL = "neutral"

from execution.entry_exit import TradePlan
from execution.risk_manager import SizingDecision, SymbolSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# TradeTicket
# ---------------------------------------------------------------------
@dataclass
class TradeTicket:
    ticket_id: str
    symbol: str
    side: str                         # "long" | "short"
    strategy_id: str
    lots: float
    requested_entry: float
    filled_entry: float
    stop_loss: float
    take_profit: float
    tp1: Optional[float]
    tp2: Optional[float]
    pip_size: float
    pip_value_per_lot: float
    risk_usd: float
    reward_usd: float
    risk_pct_of_capital: float
    rr: float
    spread_pips: float
    slippage_pips: float
    commission_usd: float
    score: Optional[float] = None
    reason: str = ""
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "open"              # open | closed | rejected
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    realized_r: Optional[float] = None
    realized_pnl_usd: Optional[float] = None
    broker_ref: Optional[str] = None
    notes: str = ""

    # -- pretty rendering --------------------------------------------------
    def render(self) -> str:
        """Human-readable, auditable ticket (ASCII box)."""
        rr_txt = f"1:{self.rr:.2f}" if self.rr > 0 else "—"
        score_txt = f"{self.score:.2f}/10 ✓" if self.score is not None else "n/a"
        slippage_txt = (
            f"{self.slippage_pips:+.2f} pips"
            if abs(self.slippage_pips) > 1e-9 else "none"
        )
        lines = [
            f"┌─ TRADE TICKET #{self.ticket_id} {'─' * 34}",
            f"│ Symbol        : {self.symbol}",
            f"│ Side          : {self.side.upper()}",
            f"│ Strategy      : {self.strategy_id or 'manual'}",
            f"│ Alignment     : {score_txt}",
            f"│ Status        : {self.status.upper()}"
            + (f" (closed @ {self.exit_price})" if self.exit_price is not None else ""),
            f"│─ Prices {'─' * 40}",
            f"│ Entry (req)   : {self.requested_entry:.5f}",
            f"│ Entry (fill)  : {self.filled_entry:.5f}  (slippage {slippage_txt})",
            f"│ Stop Loss     : {self.stop_loss:.5f}",
            f"│ Take Profit   : {self.take_profit:.5f}",
        ]
        if self.tp1 is not None:
            lines.append(f"│ TP1 (partial) : {self.tp1:.5f}")
        if self.tp2 is not None:
            lines.append(f"│ TP2 (final)   : {self.tp2:.5f}")
        lines += [
            f"│ R:R           : {rr_txt}",
            f"│─ Sizing {'─' * 40}",
            f"│ Lots          : {self.lots:.4f}",
            f"│ Pip size      : {self.pip_size}",
            f"│ Pip value     : ${self.pip_value_per_lot:.4f} / lot",
            f"│ Risk          : ${self.risk_usd:.4f}  "
            f"({self.risk_pct_of_capital:.2%} of capital)",
            f"│ Reward        : ${self.reward_usd:.4f}",
            f"│ Spread        : {self.spread_pips:.2f} pips",
            f"│ Commission    : ${self.commission_usd:.4f}",
            f"│─ Meta {'─' * 42}",
            f"│ Reason        : {self.reason or '—'}",
            f"│ Opened        : {self.opened_at.strftime('%Y-%m-%d %H:%M:%S %Z') or 'UTC'}",
        ]
        if self.closed_at is not None:
            lines.append(
                f"│ Closed        : {self.closed_at.strftime('%Y-%m-%d %H:%M:%S %Z') or 'UTC'}"
            )
        if self.realized_r is not None:
            lines.append(
                f"│ Realized      : {self.realized_r:+.2f}R  "
                f"(${self.realized_pnl_usd:+.4f})"
            )
        if self.broker_ref:
            lines.append(f"│ Broker ref    : {self.broker_ref}")
        if self.notes:
            lines.append(f"│ Notes         : {self.notes}")
        lines.append("└" + "─" * 60)
        return "\n".join(lines)


# ---------------------------------------------------------------------
# OrderRequest
# ---------------------------------------------------------------------
@dataclass
class OrderRequest:
    symbol: str
    side: str
    lots: float
    entry: float
    stop_loss: float
    take_profit: float
    strategy_id: str
    reason: str
    score: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None


# ---------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------
class BrokerConnector(abc.ABC):
    """Abstract broker interface. Subclass and wire a real provider."""

    @abc.abstractmethod
    def submit(self, request: OrderRequest, spec: SymbolSpec, decision: SizingDecision) -> TradeTicket:
        ...

    @abc.abstractmethod
    def close(self, ticket_id: str, exit_price: float, r_multiple: float) -> TradeTicket:
        ...

    @abc.abstractmethod
    def get(self, ticket_id: str) -> Optional[TradeTicket]:
        ...

    def all_tickets(self) -> List[TradeTicket]:
        return list(getattr(self, "_tickets", {}).values())


# ---------------------------------------------------------------------
# SimulatedBroker – paper trading
# ---------------------------------------------------------------------
class SimulatedBroker(BrokerConnector):
    """
    Deterministic paper broker.

    price_provider(symbol) -> (bid, ask). If not supplied, falls back to
    a single mid-price that defaults to the requested entry (zero slippage).
    """

    def __init__(
        self,
        price_provider: Optional[Callable[[str], Tuple[float, float]]] = None,
        commission_per_lot: float = 7.0,        # $7 per standard lot (typical)
        slippage_pips: float = 0.2,             # deterministic adverse slippage
        starting_balance: float = 50.0,
    ):
        self.price_provider = price_provider
        self.commission_per_lot = commission_per_lot
        self.slippage_pips = slippage_pips
        self.balance = starting_balance
        self._tickets: Dict[str, TradeTicket] = {}

    # -- helpers ----------------------------------------------------------
    def _quote(self, symbol: str, requested_entry: float) -> Tuple[float, float]:
        if self.price_provider is not None:
            return self.price_provider(symbol)
        # Fallback: assume mid == requested entry, no spread
        return requested_entry, requested_entry

    def _compute_slippage(self, side: str, spec: SymbolSpec) -> float:
        s = self.slippage_pips * spec.pip_size
        return s if side == "long" else -s

    # -- API --------------------------------------------------------------
    def submit(
        self,
        request: OrderRequest,
        spec: SymbolSpec,
        decision: SizingDecision,
    ) -> TradeTicket:
        bid, ask = self._quote(request.symbol, request.entry)
        spread_price = max(0.0, ask - bid)
        spread_pips = spread_price / spec.pip_size if spec.pip_size else 0.0

        slip = self._compute_slippage(request.side, spec)
        filled_entry = round(request.entry + slip, 10)

        commission = self.commission_per_lot * request.lots

        ticket = TradeTicket(
            ticket_id=uuid.uuid4().hex[:8],
            symbol=request.symbol,
            side=request.side,
            strategy_id=request.strategy_id,
            lots=request.lots,
            requested_entry=request.entry,
            filled_entry=filled_entry,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            tp1=request.tp1,
            tp2=request.tp2,
            pip_size=spec.pip_size,
            pip_value_per_lot=spec.pip_value_per_lot,
            risk_usd=decision.risk_usd,
            reward_usd=decision.reward_usd,
            risk_pct_of_capital=decision.risk_pct_of_capital,
            rr=(decision.reward_usd / decision.risk_usd) if decision.risk_usd else 0.0,
            spread_pips=round(spread_pips, 3),
            slippage_pips=round(abs(slip) / spec.pip_size if spec.pip_size else 0.0, 3),
            commission_usd=round(commission, 4),
            score=request.score,
            reason=request.reason,
        )
        self._tickets[ticket.ticket_id] = ticket
        logger.info("SimulatedBroker: submitted ticket %s\n%s", ticket.ticket_id, ticket.render())
        return ticket

    def close(self, ticket_id: str, exit_price: float, r_multiple: float) -> TradeTicket:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise KeyError(f"unknown ticket {ticket_id}")
        pnl = ticket.risk_usd * r_multiple - ticket.commission_usd
        ticket.status = "closed"
        ticket.closed_at = datetime.now(timezone.utc)
        ticket.exit_price = exit_price
        ticket.realized_r = r_multiple
        ticket.realized_pnl_usd = pnl
        self.balance += pnl
        logger.info("SimulatedBroker: closed %s -> %+.2fR ($%.4f)", ticket_id, r_multiple, pnl)
        return ticket

    def get(self, ticket_id: str) -> Optional[TradeTicket]:
        return self._tickets.get(ticket_id)


# ---------------------------------------------------------------------
# PlaceholderBroker – fails until wired
# ---------------------------------------------------------------------
class PlaceholderBroker(BrokerConnector):
    """Fails fast so production code paths never silently no-op."""

    def submit(self, request: OrderRequest, spec: SymbolSpec, decision: SizingDecision) -> TradeTicket:
        raise NotImplementedError(
            "Wire your broker SDK here: implement submit(), close(), get()."
        )

    def close(self, ticket_id: str, exit_price: float, r_multiple: float) -> TradeTicket:
        raise NotImplementedError("Wire your broker SDK here.")

    def get(self, ticket_id: str) -> Optional[TradeTicket]:
        return None


# ---------------------------------------------------------------------
# Convenience: build ticket from plan + decision
# ---------------------------------------------------------------------
def ticket_from_plan(
    plan: TradePlan,
    decision: SizingDecision,
    score: Optional[float] = None,
) -> OrderRequest:
    return OrderRequest(
        symbol=plan.symbol,
        side=plan.direction.value if hasattr(plan.direction, "value") else str(plan.direction),
        lots=decision.lots,
        entry=plan.entry,
        stop_loss=plan.stop_loss,
        take_profit=plan.take_profit,
        strategy_id=plan.strategy_id,
        reason=plan.reason,
        score=score,
        tp1=plan.tp1,
        tp2=plan.tp2,
    )
