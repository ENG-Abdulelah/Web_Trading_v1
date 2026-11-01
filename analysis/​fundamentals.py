# analysis/fundamentals.py
"""
analysis/fundamentals.py

Real-time news and macro/geopolitical bias analyzer.

Consumes macro indicators plus news/geopolitical events and produces a
per-symbol FundamentalBias in [-1, +1] (positive = long-biased) together
with a confidence and component breakdown. The output feeds analysis.scorer.

News events only need the attributes:
    .impact (str or str-enum: low/medium/high)
    .currencies (iterable of currency codes)
    .tags (iterable of strings)
    .published_at (tz-aware datetime)

This is exactly the shape of core.data_feed.NewsEvent, so instances can
be passed in unchanged.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)


class RiskRegime(str, Enum):
    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    NEUTRAL = "neutral"


# ---------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------
@dataclass
class MacroSnapshot:
    """Macro indicators per currency. Rates/inflation in %, GDP annualized %."""
    interest_rates: Dict[str, float] = field(default_factory=dict)
    inflation_yoy: Dict[str, float] = field(default_factory=dict)
    gdp_growth: Dict[str, float] = field(default_factory=dict)
    unemployment: Dict[str, float] = field(default_factory=dict)
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class GeopoliticalEvent:
    severity: float                # 0..1
    region: str
    tag: str                       # war, sanctions, energy, trade, cyber
    description: str
    published_at: datetime


@dataclass
class FundamentalBias:
    symbol: str
    bias: float                    # -1..+1
    confidence: float              # 0..1
    components: Dict[str, float] = field(default_factory=dict)
    reason: str = ""

    @property
    def direction_sign(self) -> int:
        if self.bias > 0.1:
            return 1
        if self.bias < -0.1:
            return -1
        return 0


@dataclass
class RiskAssessment:
    regime: RiskRegime
    intensity: float               # 0..1
    components: Dict[str, float] = field(default_factory=dict)
    reason: str = ""


# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------
SAFE_HAVEN = {"USD", "JPY", "CHF"}
RISK_ON_CURRENCIES = {"AUD", "NZD", "CAD", "EUR", "GBP"}
RISK_OFF_TAGS = {"war", "sanctions", "escalation", "attack", "conflict"}
ENERGY_TAGS = {"energy", "oil", "opec", "supply", "pipeline"}


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _event_impact(event) -> str:
    impact = getattr(event, "impact", "low")
    return getattr(impact, "value", impact)


def _event_tags(event) -> set:
    return {t.lower() for t in getattr(event, "tags", ())}


def _zscore(d: Dict[str, float]) -> Dict[str, float]:
    if not d:
        return {}
    vals = list(d.values())
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / max(1, len(vals) - 1)
    sd = math.sqrt(var) or 1.0
    return {k: (v - mean) / sd for k, v in d.items()}


# ---------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------
class FundamentalAnalyzer:
    """
    Converts macro + news + geopolitics into per-symbol bias.
    """

    def __init__(
        self,
        rate_weight: float = 0.35,
        growth_weight: float = 0.25,
        inflation_weight: float = 0.20,
        news_weight: float = 0.20,
        news_lookback_hours: float = 24.0,
    ):
        self.rate_w = rate_weight
        self.growth_w = growth_weight
        self.infl_w = inflation_weight
        self.news_w = news_weight
        self.news_lookback = timedelta(hours=news_lookback_hours)

    # -- currency strength ------------------------------------------------
    def currency_strength(
        self,
        macro: MacroSnapshot,
        news: Sequence = (),
        now: Optional[datetime] = None,
    ) -> Dict[str, float]:
        now = now or datetime.now(timezone.utc)
        currencies = (
            set(macro.interest_rates) | set(macro.gdp_growth) | set(macro.inflation_yoy)
        )
        if not currencies:
            return {}

        z_rates = _zscore(macro.interest_rates)
        z_growth = _zscore(macro.gdp_growth)
        z_infl = _zscore(macro.inflation_yoy)

        # News sentiment per currency
        news_score = {c: 0.0 for c in currencies}
        counted = 0
        for event in news:
            pub = getattr(event, "published_at", None)
            if pub is None or now - pub > self.news_lookback:
                continue
            counted += 1
            weight = {"high": 1.0, "medium": 0.5, "low": 0.2}.get(_event_impact(event), 0.1)
            tags = _event_tags(event)
            risk_off = bool(tags & RISK_OFF_TAGS)

            for ccy in getattr(event, "currencies", ()):
                ccy = ccy.upper()
                if ccy not in news_score:
                    continue
                if risk_off and ccy in SAFE_HAVEN:
                    news_score[ccy] += weight
                elif risk_off and ccy in RISK_ON_CURRENCIES:
                    news_score[ccy] -= weight
                else:
                    news_score[ccy] += weight * 0.5

        strengths: Dict[str, float] = {}
        denom = max(1, counted)
        for ccy in currencies:
            s = (
                self.rate_w * z_rates.get(ccy, 0.0)
                + self.growth_w * z_growth.get(ccy, 0.0)
                - self.infl_w * z_infl.get(ccy, 0.0)
                + self.news_w * (news_score.get(ccy, 0.0) / denom)
            )
            strengths[ccy] = math.tanh(s)
        return strengths

    # -- geopolitical risk -----------------------------------------------
    def geopolitical_risk(
        self,
        events: Sequence[GeopoliticalEvent],
        now: Optional[datetime] = None,
    ) -> RiskAssessment:
        now = now or datetime.now(timezone.utc)
        active = [e for e in events if now - e.published_at <= timedelta(hours=72)]
        if not active:
            return RiskAssessment(regime=RiskRegime.NEUTRAL, intensity=0.0)

        total = 0.0
        energy = 0.0
        for e in active:
            hours_old = max(0.0, (now - e.published_at).total_seconds() / 3600.0)
            decay = math.exp(-hours_old / 48.0)
            weighted = e.severity * decay
            total += weighted
            if e.tag.lower() in ENERGY_TAGS:
                energy += weighted

        intensity = 1.0 - math.exp(-total)
        regime = RiskRegime.NEUTRAL if intensity < 0.25 else RiskRegime.RISK_OFF
        return RiskAssessment(
            regime=regime,
            intensity=min(1.0, intensity),
            components={"total": total, "energy": energy},
            reason=f"{len(active)} active events, intensity={intensity:.2f}",
        )

    # -- per-symbol bias --------------------------------------------------
    def symbol_bias(
        self,
        symbol: str,
        macro: MacroSnapshot,
        news: Sequence = (),
        geo_events: Sequence[GeopoliticalEvent] = (),
        now: Optional[datetime] = None,
    ) -> FundamentalBias:
        now = now or datetime.now(timezone.utc)
        strengths = self.currency_strength(macro, news, now)
        risk = self.geopolitical_risk(geo_events, now)
        risk_off = risk.intensity if risk.regime == RiskRegime.RISK_OFF else 0.0

        sym = symbol.upper()
        components: Dict[str, float] = {}
        reason_parts: List[str] = []

        # ---- precious metals ------------------------------------------
        if sym.startswith(("XAU", "XAG")):
            usd = strengths.get("USD", 0.0)
            components["usd_inverse"] = -0.5 * usd
            components["risk"] = 0.5 * risk_off
            reason_parts += [f"USD {usd:+.2f}", f"risk-off {risk_off:.2f}"]

        # ---- energy ---------------------------------------------------
        elif any(k in sym for k in ("WTI", "XTI", "OIL", "BRENT")):
            usd = strengths.get("USD", 0.0)
            energy = risk.components.get("energy", 0.0)
            components["usd_inverse"] = -0.3 * usd
            components["energy"] = 0.7 * energy
            reason_parts += [f"USD {usd:+.2f}", f"energy {energy:.2f}"]

        # ---- forex pairs ----------------------------------------------
        elif "/" in sym:
            base, quote = sym.split("/", 1)
            b, q = strengths.get(base, 0.0), strengths.get(quote, 0.0)
            components["macro"] = 0.5 * (b - q)

            if quote in SAFE_HAVEN:
                components["risk"] = -0.5 * risk_off
            elif base in SAFE_HAVEN:
                components["risk"] = 0.5 * risk_off
            else:
                components["risk"] = 0.0

            reason_parts += [f"{base} {b:+.2f}", f"{quote} {q:+.2f}"]
            if risk_off:
                reason_parts.append(f"risk-off {risk_off:.2f}")

        else:
            components["macro"] = 0.0
            components["risk"] = 0.0

        raw = sum(components.values())
        bias = math.tanh(raw)
        confidence = min(1.0, abs(bias) * 0.7 + risk.intensity * 0.3)

        return FundamentalBias(
            symbol=symbol,
            bias=bias,
            confidence=confidence,
            components=components,
            reason=" | ".join(reason_parts) or "neutral macro backdrop",
        )

    def bias_for_universe(
        self,
        symbols: Iterable[str],
        macro: MacroSnapshot,
        news: Sequence = (),
        geo_events: Sequence[GeopoliticalEvent] = (),
        now: Optional[datetime] = None,
    ) -> Dict[str, FundamentalBias]:
        return {
            s: self.symbol_bias(s, macro, news, geo_events, now)
            for s in symbols
        }
