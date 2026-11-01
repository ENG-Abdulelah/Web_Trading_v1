# analysis/scorer.py
"""
analysis/scorer.py

Multi-dimensional alignment score combining:
    * technical  – signal confidence + reward/risk from ict_engine
    * economic   – macro bias from fundamentals
    * geopolitical – risk regime vs symbol behaviour

Total is 0..10. A trade is only approved when total >= MIN_SCORE (5.5/10).
`validate_or_raise` enforces the gate hard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

try:
    from .fundamentals import FundamentalBias, RiskAssessment, RiskRegime
    from .ict_engine import Direction, StrategySignal
except ImportError:  # pragma: no cover - direct-script fallback
    from fundamentals import FundamentalBias, RiskAssessment, RiskRegime  # type: ignore
    from ict_engine import Direction, StrategySignal  # type: ignore

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """Raised by TradeScorer.validate_or_raise when a trade fails the gate."""


@dataclass
class ScoredTrade:
    symbol: str
    direction: Direction
    technical: float               # 0..10
    macro: float                   # 0..10
    geopolitical: float            # 0..10
    alignment: float               # 0..1 fraction of dimensions agreeing
    total: float                   # 0..10
    approved: bool
    reason: str
    components: Dict[str, float] = field(default_factory=dict)


class TradeScorer:
    """
    Combines technical, macro, and geopolitical dimensions into a
    single alignment score on a 0-10 scale.

    Strict validation:
        approved = (total >= MIN_SCORE)
    with `MIN_SCORE` defaulting to 5.5. An optional alignment requirement
    (default off) can further restrict approvals.
    """

    MIN_SCORE: float = 5.5

    def __init__(
        self,
        technical_weight: float = 0.5,
        macro_weight: float = 0.3,
        geopolitical_weight: float = 0.2,
        min_score: float = 5.5,
        require_alignment: int = 0,   # 0 = off; set to 2 to require majority
    ):
        total_w = technical_weight + macro_weight + geopolitical_weight
        if total_w <= 0:
            raise ValueError("Weights must sum to a positive value")
        self.w_tech = technical_weight / total_w
        self.w_macro = macro_weight / total_w
        self.w_geo = geopolitical_weight / total_w
        self.min_score = min_score
        self.require_alignment = require_alignment

    # -- scoring ----------------------------------------------------------
    def score(
        self,
        signal: StrategySignal,
        bias: FundamentalBias,
        risk: RiskAssessment,
    ) -> ScoredTrade:
        dir_sign = 1 if signal.direction == Direction.LONG else (
            -1 if signal.direction == Direction.SHORT else 0
        )

        # ---- technical ----
        conf = max(0.0, min(1.0, signal.confidence))
        rr_score = max(0.0, min(1.0, signal.rr / 3.0))
        technical = 10.0 * (0.7 * conf + 0.3 * rr_score)

        # ---- macro ----
        # 5 = neutral, 10 = fully aligned with a confident bias, 0 = strongly opposed.
        macro_align = bias.bias * dir_sign          # -1..+1
        macro = 5.0 + 5.0 * macro_align * max(0.0, min(1.0, bias.confidence))
        macro = max(0.0, min(10.0, macro))

        # ---- geopolitical ----
        geo = 5.0
        if dir_sign != 0 and risk.regime == RiskRegime.RISK_OFF:
            intensity = risk.intensity
            sh_pos = self._safe_haven_position(signal.symbol)  # +1/-1/0
            if sh_pos != 0:
                # Safe haven: risk-off benefits long for +1, short for -1.
                geo = 5.0 + 5.0 * intensity * (sh_pos * dir_sign)
            elif self._is_energy(signal.symbol) and risk.components.get("energy", 0.0) > 0:
                # Energy supply shock: risk-off with energy stress → bullish oil.
                geo = 5.0 + 5.0 * intensity * dir_sign
            else:
                # Risk-on / neutral pairs suffer in risk-off.
                geo = 5.0 - 5.0 * intensity * dir_sign
        geo = max(0.0, min(10.0, geo))

        # ---- alignment ----
        tech_aligned = technical >= 5.0
        macro_aligned = macro_align > 0.1
        geo_aligned = geo >= 5.0
        aligned_count = int(tech_aligned) + int(macro_aligned) + int(geo_aligned)
        alignment = aligned_count / 3.0

        # ---- total + gate ----
        total = self.w_tech * technical + self.w_macro * macro + self.w_geo * geo
        approved = total >= self.min_score and (
            self.require_alignment == 0 or aligned_count >= self.require_alignment
        )

        reasons = []
        if total < self.min_score:
            reasons.append(f"total {total:.2f} < min {self.min_score:.2f}")
        if self.require_alignment and aligned_count < self.require_alignment:
            reasons.append(f"only {aligned_count}/3 dimensions aligned")
        if not reasons:
            reasons.append(f"approved at {total:.2f}")

        return ScoredTrade(
            symbol=signal.symbol,
            direction=signal.direction,
            technical=round(technical, 3),
            macro=round(macro, 3),
            geopolitical=round(geo, 3),
            alignment=alignment,
            total=round(total, 3),
            approved=approved,
            reason="; ".join(reasons),
            components={
                "tech_conf": conf,
                "rr": round(signal.rr, 3),
                "bias": round(bias.bias, 3),
                "bias_conf": round(bias.confidence, 3),
                "risk_intensity": round(risk.intensity, 3),
                "aligned_dimensions": aligned_count,
            },
        )

    def validate_or_raise(self, scored: ScoredTrade) -> ScoredTrade:
        """Hard gate — raises if the trade is not approved."""
        if not scored.approved:
            raise ValidationError(
                f"Trade on {scored.symbol} rejected: {scored.reason} "
                f"(score {scored.total:.2f}/{self.min_score:.2f})"
            )
        return scored

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _safe_haven_position(symbol: str) -> int:
        """
        +1 if a LONG on this symbol benefits from risk-off.
        -1 if a SHORT benefits from risk-off (e.g. USD/JPY, USD/CHF).
         0 if not a safe-haven expression.
        """
        sym = symbol.upper()
        if sym.startswith(("XAU", "XAG")):
            return 1
        if sym.endswith(("JPY", "CHF")):
            return -1
        if "/" in sym:
            base = sym.split("/")[0]
            if base in ("JPY", "CHF"):
                return 1
        return 0

    @staticmethod
    def _is_energy(symbol: str) -> bool:
        sym = symbol.upper()
        return any(k in sym for k in ("WTI", "XTI", "OIL", "BRENT"))
