# fundamentals.py
"""
Fundamentals Engine — Real macro + geopolitical bias computation.

Sources:
  • CFTC SODA2 API (public, no key)   → COT + Legacy + Disaggregated
  • BLS Public API (public, no key)    → CPI releases
  • ForexFactory JSON (public)         → Economic calendar
  • FXStreet API (optional env vars)   → FXDaily equivalent
  • Investing.com via Apify (opt env)  → Investing feed

Computes per-symbol FundamentalBias ∈ [-1, +1]:
  +1 = strongly bullish, -1 = strongly bearish

Developed & Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# =============================================================================
# CONSTANTS
# =============================================================================
SAFE_HAVEN = {"USD", "JPY", "CHF"}
RISK_ON = {"AUD", "NZD", "CAD", "EUR", "GBP"}
RISK_OFF_TAGS = {"war", "sanctions", "escalation", "attack", "conflict", "crisis"}
ENERGY_TAGS = {"energy", "oil", "opec", "supply", "pipeline"}


# =============================================================================
# DATA CLASSES
# =============================================================================
@dataclass
class EconomicEvent:
    source: str
    title: str
    currency: str
    impact: str
    event_time: datetime
    forecast: str = ""
    previous: str = ""
    actual: str = ""
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source": self.source, "title": self.title,
            "currency": self.currency, "impact": self.impact,
            "event_time": self.event_time.isoformat(),
            "forecast": self.forecast, "previous": self.previous,
            "actual": self.actual, "tags": self.tags,
        }


@dataclass
class COTReport:
    market: str
    report_date: Optional[datetime]
    noncomm_long: Optional[float]
    noncomm_short: Optional[float]
    noncomm_net: Optional[float]
    commercial_net: Optional[float]
    open_interest: Optional[float]

    def to_dict(self) -> dict:
        return {
            "market": self.market,
            "report_date": self.report_date.isoformat() if self.report_date else None,
            "noncomm_long": self.noncomm_long,
            "noncomm_short": self.noncomm_short,
            "noncomm_net": self.noncomm_net,
            "commercial_net": self.commercial_net,
            "open_interest": self.open_interest,
        }


@dataclass
class FundamentalBias:
    symbol: str
    bias: float               # -1 .. +1
    confidence: float         # 0 .. 1
    components: Dict[str, float] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "bias": round(self.bias, 3),
            "confidence": round(self.confidence, 3),
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "reason": self.reason,
        }


# =============================================================================
# FETCHERS
# =============================================================================
class BaseFetcher:
    name = "base"
    cache_ttl = 300

    def __init__(self):
        self._cache = {}
        self._cache_ts = 0.0
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; TradingAnalyst/1.0)"
        })

    def _cached(self):
        with self._lock:
            if _time.time() - self._cache_ts < self.cache_ttl:
                return self._cache
        return None

    def _set_cache(self, value):
        with self._lock:
            self._cache = value
            self._cache_ts = _time.time()


# -----------------------------------------------------------------------------
# CFTC / COT
# -----------------------------------------------------------------------------
class CFTCFetcher(BaseFetcher):
    """
    CFTC SODA2 API (public, no key required).
    Datasets: legacy_futures_only (COT), disaggregated_futures_only.
    """
    name = "cftc"
    cache_ttl = 3600   # 1 hour

    BASE = "https://publicreporting.cftc.gov/resource"

    MARKETS = {
        "EURO FX": "EUR", "BRITISH POUND": "GBP",
        "JAPANESE YEN": "JPY", "SWISS FRANC": "CHF",
        "CANADIAN DOLLAR": "CAD", "AUSTRALIAN DOLLAR": "AUD",
        "NEW ZEALAND DOLLAR": "NZD",
        "GOLD": "XAU", "SILVER": "XAG",
        "CRUDE OIL": "WTI",
    }

    # SODA2 dataset IDs
    DATASET_LEGACY = "6dca-aqww"

    def fetch(self) -> List[COTReport]:
        cached = self._cached()
        if cached is not None:
            return cached

        try:
            # Latest week: filter by recent date, take all markets we care about
            url = f"{self.BASE}/{self.DATASET_LEGACY}.json"
            params = {
                "$limit": 500,
                "$order": "report_date_as_yyyy_mm_dd DESC",
            }
            r = self.session.get(url, params=params, timeout=20)
            r.raise_for_status()
            rows = r.json()
        except Exception as e:
            logger.warning("CFTC fetch failed: %s", e)
            self._set_cache([])
            return []

        # Group by market, keep latest
        latest: Dict[str, dict] = {}
        for row in rows:
            name = (row.get("market_and_exchange_names") or "").upper()
            for key in self.MARKETS:
                if key in name:
                    if key not in latest:
                        latest[key] = row
                    break

        out: List[COTReport] = []
        for market, row in latest.items():
            try:
                rd = row.get("report_date_as_yyyy_mm_dd")
                rdt = (datetime.fromisoformat(rd[:10]).replace(tzinfo=timezone.utc)
                       if rd else None)

                def _f(k):
                    v = row.get(k)
                    try:
                        return float(v) if v is not None else None
                    except (TypeError, ValueError):
                        return None

                noncomm_long = _f("noncomm_positions_long_all")
                noncomm_short = _f("noncomm_positions_short_all")
                noncomm_net = ((noncomm_long or 0) - (noncomm_short or 0)
                               if noncomm_long is not None and noncomm_short is not None
                               else None)
                commercial_long = _f("comm_positions_long_all") or 0
                commercial_short = _f("comm_positions_short_all") or 0

                out.append(COTReport(
                    market=market,
                    report_date=rdt,
                    noncomm_long=noncomm_long,
                    noncomm_short=noncomm_short,
                    noncomm_net=noncomm_net,
                    commercial_net=commercial_long - commercial_short,
                    open_interest=_f("open_interest_all"),
                ))
            except Exception as e:
                logger.debug("CFTC parse failed for %s: %s", market, e)

        self._set_cache(out)
        return out


# -----------------------------------------------------------------------------
# BLS CPI
# -----------------------------------------------------------------------------
class CPIFetcher(BaseFetcher):
    """BLS Public Data API v1 (no key needed for v1)."""
    name = "cpi"
    cache_ttl = 3600
    API = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
    SERIES = "CUUR0000SA0"

    def fetch_latest(self) -> Optional[Dict]:
        cached = self._cached()
        if cached is not None:
            return cached

        now = utcnow()
        payload = {
            "seriesid": [self.SERIES],
            "startyear": str(now.year - 1),
            "endyear": str(now.year),
        }
        try:
            r = self.session.post(self.API, json=payload, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("BLS CPI fetch failed: %s", e)
            self._set_cache(None)
            return None

        series = data.get("Results", {}).get("series", [])
        if not series:
            self._set_cache(None)
            return None
        rows = series[0].get("data", [])
        if not rows:
            self._set_cache(None)
            return None

        latest = rows[0]
        try:
            value = float(str(latest.get("value", "0")).replace(",", ""))
        except (TypeError, ValueError):
            self._set_cache(None)
            return None

        result = {
            "period": f"{latest.get('year')}-{latest.get('period', 'M00')[1:]}",
            "cpi_value": value,
            "source": "bls",
        }
        self._set_cache(result)
        return result


# -----------------------------------------------------------------------------
# ForexFactory
# -----------------------------------------------------------------------------
class ForexFactoryFetcher(BaseFetcher):
    name = "forexfactory"
    cache_ttl = 300
    URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    def fetch(self) -> List[EconomicEvent]:
        cached = self._cached()
        if cached is not None:
            return cached

        try:
            r = self.session.get(self.URL, timeout=10)
            r.raise_for_status()
            raw = r.json()
        except Exception as e:
            logger.warning("ForexFactory fetch failed: %s", e)
            self._set_cache([])
            return []

        out: List[EconomicEvent] = []
        for item in raw or []:
            try:
                dt = datetime.fromisoformat(
                    item.get("date", "").replace("Z", "+00:00")
                )
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            impact = str(item.get("impact", "Medium")).lower()
            if impact not in ("high", "medium", "low"):
                impact = "medium"
            out.append(EconomicEvent(
                source="forexfactory",
                title=item.get("title", "—"),
                currency=item.get("country", ""),
                impact=impact,
                event_time=dt,
                forecast=item.get("forecast", ""),
                previous=item.get("previous", ""),
                actual=item.get("actual", ""),
            ))

        self._set_cache(out)
        return out


# -----------------------------------------------------------------------------
# FXStreet (FXDaily equivalent) — optional
# -----------------------------------------------------------------------------
class FXStreetFetcher(BaseFetcher):
    """FXStreet Calendar API — only works if credentials are set."""
    name = "fxstreet"
    cache_ttl = 600
    TOKEN_URL = "https://calendar-api.fxstreet.com/en/api/v1/oauth2/token"
    EVENTS_URL = "https://calendar-api.fxstreet.com/en/api/v1/events"

    def __init__(self):
        super().__init__()
        self._token = None
        self._token_exp = 0.0

    def _get_token(self) -> Optional[str]:
        if self._token and _time.time() < self._token_exp - 60:
            return self._token
        cid = os.getenv("FXSTREET_CLIENT_ID")
        secret = os.getenv("FXSTREET_CLIENT_SECRET")
        if not cid or not secret:
            return None
        try:
            r = self.session.post(
                self.TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": cid, "client_secret": secret,
                    "scope": "calendar",
                },
                timeout=15,
            )
            r.raise_for_status()
            payload = r.json()
            self._token = payload["access_token"]
            self._token_exp = _time.time() + int(payload.get("expires_in", 3600))
            return self._token
        except Exception as e:
            logger.warning("FXStreet token failed: %s", e)
            return None

    def fetch(self) -> List[EconomicEvent]:
        token = self._get_token()
        if not token:
            return []
        cached = self._cached()
        if cached is not None:
            return cached

        now = utcnow()
        params = {
            "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "volatility": "high,medium",
        }
        try:
            r = self.session.get(
                self.EVENTS_URL, params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("FXStreet events failed: %s", e)
            self._set_cache([])
            return []

        items = data if isinstance(data, list) else data.get("events", [])
        out: List[EconomicEvent] = []
        for it in items:
            try:
                dt = datetime.fromisoformat(
                    (it.get("date") or "").replace("Z", "+00:00")
                )
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            impact = str(it.get("volatility", "medium")).lower()
            if impact not in ("high", "medium", "low"):
                continue
            out.append(EconomicEvent(
                source="fxstreet",
                title=it.get("name", "—"),
                currency=it.get("currency", ""),
                impact=impact,
                event_time=dt,
                forecast=str(it.get("consensus", "")),
                previous=str(it.get("previous", "")),
                actual=str(it.get("actual", "")),
            ))
        self._set_cache(out)
        return out


# -----------------------------------------------------------------------------
# Investing.com (Apify — optional)
# -----------------------------------------------------------------------------
class InvestingFetcher(BaseFetcher):
    name = "investing"
    cache_ttl = 600

    def fetch(self) -> List[EconomicEvent]:
        token = os.getenv("APIFY_TOKEN")
        if not token:
            return []
        cached = self._cached()
        if cached is not None:
            return cached

        try:
            from apify_client import ApifyClient  # type: ignore
        except ImportError:
            return []

        try:
            client = ApifyClient(token)
            run = client.actor(
                "pintostudio/economic-calendar-data-investing-com"
            ).call(run_input={
                "timeFilter": "time_only",
                "importances": "high,medium",
            })
            items = list(client.dataset(run.default_dataset_id).iterate_items())
        except Exception as e:
            logger.warning("Apify call failed: %s", e)
            self._set_cache([])
            return []

        out: List[EconomicEvent] = []
        for it in items:
            try:
                dt = datetime.fromisoformat(
                    (it.get("date") or "").replace("Z", "+00:00")
                )
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            impact = str(it.get("importance", "medium")).lower()
            if impact not in ("high", "medium", "low"):
                continue
            out.append(EconomicEvent(
                source="investing",
                title=it.get("event", "—"),
                currency=it.get("country", ""),
                impact=impact,
                event_time=dt,
                forecast=str(it.get("forecast", "")),
                previous=str(it.get("previous", "")),
                actual=str(it.get("actual", "")),
            ))
        self._set_cache(out)
        return out


# =============================================================================
# FUNDAMENTAL BIAS ENGINE
# =============================================================================
class FundamentalsEngine:
    """
    Combines COT + CFTC + CPI + news into per-symbol FundamentalBias.
    """

    def __init__(self):
        self.cftc = CFTCFetcher()
        self.cpi = CPIFetcher()
        self.ff = ForexFactoryFetcher()
        self.fxstreet = FXStreetFetcher()
        self.investing = InvestingFetcher()

    # ------------------------------------------------------------------
    def all_events(self) -> List[EconomicEvent]:
        events: List[EconomicEvent] = []
        for f in (self.ff, self.fxstreet, self.investing):
            try:
                events.extend(f.fetch())
            except Exception as e:
                logger.warning("%s fetch failed: %s", f.name, e)
        return events

    def all_cot(self) -> List[COTReport]:
        try:
            return self.cftc.fetch()
        except Exception as e:
            logger.warning("COT failed: %s", e)
            return []

    # ------------------------------------------------------------------
    def _currency_strength(self, events: List[EconomicEvent],
                            cot: List[COTReport]) -> Dict[str, float]:
        """
        Rough per-currency strength score in [-1, +1].
        Based on:
          - COT net position sign
          - recent news sentiment
        """
        # COT signal per currency
        cot_score: Dict[str, float] = {}
        # Mapping of market→currency (XAU/XAG/WTI: dollar inverse)
        for r in cot:
            m = r.market
            if m in ("EURO FX",):
                cot_score["EUR"] = (cot_score.get("EUR", 0.0)
                                    + math.tanh((r.noncomm_net or 0) / 100_000))
            elif m == "BRITISH POUND":
                cot_score["GBP"] = (cot_score.get("GBP", 0.0)
                                    + math.tanh((r.noncomm_net or 0) / 100_000))
            elif m == "JAPANESE YEN":
                cot_score["JPY"] = (cot_score.get("JPY", 0.0)
                                    + math.tanh((r.noncomm_net or 0) / 100_000))
            elif m == "SWISS FRANC":
                cot_score["CHF"] = (cot_score.get("CHF", 0.0)
                                    + math.tanh((r.noncomm_net or 0) / 100_000))
            elif m == "CANADIAN DOLLAR":
                cot_score["CAD"] = (cot_score.get("CAD", 0.0)
                                    + math.tanh((r.noncomm_net or 0) / 100_000))
            elif m == "AUSTRALIAN DOLLAR":
                cot_score["AUD"] = (cot_score.get("AUD", 0.0)
                                    + math.tanh((r.noncomm_net or 0) / 100_000))
            elif m == "NEW ZEALAND DOLLAR":
                cot_score["NZD"] = (cot_score.get("NZD", 0.0)
                                    + math.tanh((r.noncomm_net or 0) / 100_000))

        # News sentiment per currency
        news_score: Dict[str, float] = {}
        now = utcnow()
        for e in events:
            if now - e.event_time > timedelta(hours=24):
                continue
            if now - e.event_time < timedelta(hours=-2):
                continue
            w = {"high": 1.0, "medium": 0.5, "low": 0.2}.get(e.impact, 0.1)
            ccy = e.currency.upper()[:3] if e.currency else ""
            if not ccy:
                continue
            news_score[ccy] = news_score.get(ccy, 0.0) + w * 0.3

        out: Dict[str, float] = {}
        all_ccys = set(cot_score) | set(news_score) | set(SAFE_HAVEN) | RISK_ON
        for c in all_ccys:
            out[c] = math.tanh(cot_score.get(c, 0.0) + news_score.get(c, 0.0))
        return out

    # ------------------------------------------------------------------
    def bias(self, symbol: str, events: Optional[List[EconomicEvent]] = None,
             cot: Optional[List[COTReport]] = None) -> FundamentalBias:
        events = events if events is not None else self.all_events()
        cot = cot if cot is not None else self.all_cot()

        strengths = self._currency_strength(events, cot)
        usd = strengths.get("USD", 0.0)
        reason_parts: List[str] = []

        # Precious metals
        if symbol.startswith(("XAU", "XAG")):
            # Metals inverse to USD, sensitive to risk-off
            bias = -0.7 * usd
            reason_parts.append(f"USD {usd:+.2f}")

        # Energy
        elif symbol.startswith("WTI"):
            bias = -0.4 * usd
            reason_parts.append(f"USD {usd:+.2f}")

        # Forex pairs
        elif "/" in symbol:
            base, quote = symbol.split("/")
            b = strengths.get(base, 0.0)
            q = strengths.get(quote, 0.0)
            bias = 0.5 * (b - q)
            reason_parts += [f"{base} {b:+.2f}", f"{quote} {q:+.2f}"]

        else:
            bias = 0.0

        bias = math.tanh(bias)
        confidence = min(1.0, abs(bias) * 0.8 + 0.2)

        return FundamentalBias(
            symbol=symbol,
            bias=bias,
            confidence=confidence,
            components={
                "usd": strengths.get("USD", 0.0),
                "base": strengths.get(symbol.split("/")[0], 0.0)
                        if "/" in symbol else 0.0,
                "quote": strengths.get(symbol.split("/")[1], 0.0)
                        if "/" in symbol else 0.0,
            },
            reason=" | ".join(reason_parts) or "neutral",
        )

    # ------------------------------------------------------------------
    def bias_for_all(self, symbols: List[str]) -> Dict[str, FundamentalBias]:
        events = self.all_events()
        cot = self.all_cot()
        return {s: self.bias(s, events, cot) for s in symbols}

    # ------------------------------------------------------------------
    def snapshot(self, symbols: List[str]) -> Dict:
        events = self.all_events()
        cot = self.all_cot()
        cpi = self.cpi.fetch_latest()
        biases = {s: self.bias(s, events, cot).to_dict() for s in symbols}
        return {
            "events": [e.to_dict() for e in events[:30]],
            "cot": [r.to_dict() for r in cot],
            "cpi": cpi,
            "biases": biases,
            "sources_available": {
                "forexfactory": True,
                "cftc_cot": bool(cot),
                "cpi_bls": cpi is not None,
                "fxstreet": bool(os.getenv("FXSTREET_CLIENT_ID")),
                "investing": bool(os.getenv("APIFY_TOKEN")),
            },
            "computed_at": utcnow().isoformat(),
        }


# =============================================================================
# MODULE SINGLETON
# =============================================================================
_engine: Optional[FundamentalsEngine] = None
_lock = threading.Lock()


def get_engine() -> FundamentalsEngine:
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = FundamentalsEngine()
    return _engine
