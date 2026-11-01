"""
Live data feed + TradingGuard news filter for Web_Trading_v1.

Sources integrated:
  • ForexFactory        — economic calendar (high-impact events)
  • Investing.com       — economic calendar (via Apify actor, optional)
  • FXDaily             — economic calendar (via FXStreet Calendar API, optional)
  • CPI Releases        — BLS public API / official release schedule
  • COT (Commitments of Traders) — CFTC SODA2 API via cftc-cot-soda
  • CFTC Reports        — CFTC SODA2 API (legacy + disaggregated)

TradingGuard:
  • Hard-blocks new trades from T-5 minutes before a high-impact event
    until T+10 minutes after the scheduled release (stabilization window).
  • Exposes .can_trade(symbol, style, dt) -> Permission
  • Exposes .snapshot() -> dict for the web dashboard

Developed & Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time as _time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional, Iterable

import requests

import config

# =============================================================================
# LOGGING
# =============================================================================
logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.addHandler(logging.NullHandler())


# =============================================================================
# TIME HELPERS
# =============================================================================
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(dt: Optional[datetime]) -> datetime:
    if dt is None:
        return utcnow()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Handle trailing Z
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:
        return None


# =============================================================================
# DATA CLASSES
# =============================================================================
@dataclass
class EconomicEvent:
    """Normalized economic calendar event across all providers."""
    source: str                       # "forexfactory" | "investing" | "fxdaily" | "cpi" | "cot" | "cftc"
    title: str
    currency: str = ""
    impact: str = "medium"            # "high" | "medium" | "low" | "holiday"
    event_time: Optional[datetime] = None
    forecast: str = ""
    previous: str = ""
    actual: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def is_high_impact(self) -> bool:
        return self.impact == "high"

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.event_time:
            d["event_time"] = self.event_time.isoformat()
        return d


@dataclass
class COTReport:
    """Single COT row for a market."""
    market: str
    report_date: Optional[datetime] = None
    noncomm_long: Optional[float] = None
    noncomm_short: Optional[float] = None
    noncomm_net: Optional[float] = None
    commercial_long: Optional[float] = None
    commercial_short: Optional[float] = None
    commercial_net: Optional[float] = None
    open_interest: Optional[float] = None
    dataset: str = "legacy"           # "legacy" | "disaggregated" | ...
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.report_date:
            d["report_date"] = self.report_date.isoformat()
        d.pop("raw", None)
        return d


@dataclass
class CPIRelease:
    """Single CPI release (BLS)."""
    period: str                       # e.g. "2025-09"
    release_datetime: Optional[datetime] = None
    cpi_value: Optional[float] = None
    cpi_yoy: Optional[float] = None
    cpi_mom: Optional[float] = None
    source: str = "bls"

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.release_datetime:
            d["release_datetime"] = self.release_datetime.isoformat()
        return d


# =============================================================================
# BASE FETCHER
# =============================================================================
class BaseFetcher:
    """Common HTTP helpers + short-lived in-memory cache."""

    name: str = "base"
    cache_ttl: int = 300              # seconds (5 min)

    def __init__(self, session: Optional[requests.Session] = None):
        self._session = session or requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
        })
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ cache
    def _cache_get(self, key: str):
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            ts, value = entry
            if _time.time() - ts > self.cache_ttl:
                self._cache.pop(key, None)
                return None
            return value

    def _cache_set(self, key: str, value):
        with self._lock:
            self._cache[key] = (_time.time(), value)

    # ------------------------------------------------------------------ http
    def _get_json(self, url: str, **kwargs) -> Optional[dict]:
        try:
            r = self._session.get(url, timeout=15, **kwargs)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.warning("[%s] GET %s failed: %s", self.name, url, exc)
            return None

    def _get_text(self, url: str, **kwargs) -> Optional[str]:
        try:
            r = self._session.get(url, timeout=15, **kwargs)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            logger.warning("[%s] GET %s failed: %s", self.name, url, exc)
            return None

    def fetch(self) -> list:
        raise NotImplementedError


# =============================================================================
# 1) FOREXFACTORY
# =============================================================================
class ForexFactoryFetcher(BaseFetcher):
    """
    Primary:  `forexfactory` PyPI package (parquet cache, no scraping).
    Fallback: JSON endpoint used by the FF calendar page.
    """

    name = "forexfactory"
    cache_ttl = 300

    def fetch(self, days: int = 2) -> list[EconomicEvent]:
        cache_key = f"ff:{days}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        events = self._fetch_via_package(days)
        if not events:
            events = self._fetch_via_endpoint(days)

        self._cache_set(cache_key, events)
        return events

    # ------------------------------------------------------------------ pkg
    def _fetch_via_package(self, days: int) -> list[EconomicEvent]:
        try:
            import forexfactory  # type: ignore
        except ImportError:
            return []

        try:
            df = forexfactory.read(
                currencies=["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"],
                impacts=["high", "medium"],
                start=utcnow().strftime("%Y-%m"),
            )
        except Exception as exc:
            logger.warning("[forexfactory] package read failed: %s", exc)
            return []

        out: list[EconomicEvent] = []
        now = utcnow()
        horizon = now + timedelta(days=days)

        for _, row in df.iterrows():
            ts = row.get("datetime_utc")
            if ts is None:
                continue
            ts = _to_utc(ts if isinstance(ts, datetime) else _parse_iso(str(ts)))
            if ts < now - timedelta(hours=12) or ts > horizon:
                continue

            impact = str(row.get("impact", "medium")).lower()
            if impact not in ("high", "medium"):
                continue

            out.append(EconomicEvent(
                source="forexfactory",
                title=str(row.get("event", row.get("title", "—"))),
                currency=str(row.get("currency", "")),
                impact=impact,
                event_time=ts,
                forecast=str(row.get("forecast", "") or ""),
                previous=str(row.get("previous", "") or ""),
                actual=str(row.get("actual", "") or ""),
            ))
        return out

    # ------------------------------------------------------------------ raw
    def _fetch_via_endpoint(self, days: int) -> list[EconomicEvent]:
        # FF serves the calendar JSON via this endpoint (public, no key).
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        data = self._get_json(url)
        if not data:
            return []

        out: list[EconomicEvent] = []
        now = utcnow()
        horizon = now + timedelta(days=days)

        for item in data:
            ts = _parse_iso(item.get("date", ""))
            if ts is None:
                continue
            if ts < now - timedelta(hours=12) or ts > horizon:
                continue

            impact = str(item.get("impact", "Medium")).lower()
            if impact not in ("high", "medium"):
                continue

            out.append(EconomicEvent(
                source="forexfactory",
                title=item.get("title", "—"),
                currency=item.get("country", ""),
                impact=impact,
                event_time=ts,
                forecast=item.get("forecast", ""),
                previous=item.get("previous", ""),
                actual=item.get("actual", ""),
            ))
        return out


# =============================================================================
# 2) INVESTING.COM
# =============================================================================
class InvestingComFetcher(BaseFetcher):
    """
    Investing.com has no free public API.
    Primary:  Apify actor `pintostudio/economic-calendar-data-investing-com`
              (requires APIFY_TOKEN env var).
    Fallback: empty list (TradingGuard still works with other sources).
    """

    name = "investing"
    cache_ttl = 600

    def fetch(self, days: int = 2) -> list[EconomicEvent]:
        token = os.getenv("APIFY_TOKEN")
        if not token:
            logger.info("[investing] APIFY_TOKEN not set — source skipped")
            return []

        cache_key = f"inv:{days}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            from apify_client import ApifyClient  # type: ignore
        except ImportError:
            logger.warning("[investing] apify-client not installed")
            return []

        client = ApifyClient(token)
        run_input = {
            "timeFilter": "time_only",
            "importances": "high,medium",
            "categories": "",
            "country": "",
        }
        try:
            run = client.actor(
                "pintostudio/economic-calendar-data-investing-com"
            ).call(run_input=run_input)
            items = list(client.dataset(run.default_dataset_id).iterate_items())
        except Exception as exc:
            logger.warning("[investing] Apify call failed: %s", exc)
            return []

        out: list[EconomicEvent] = []
        now = utcnow()
        horizon = now + timedelta(days=days)

        for it in items:
            ts = _parse_iso(it.get("date") or it.get("datetime") or "")
            if ts is None:
                continue
            if ts < now - timedelta(hours=12) or ts > horizon:
                continue

            impact = str(it.get("importance", "medium")).lower()
            if impact not in ("high", "medium"):
                continue

            out.append(EconomicEvent(
                source="investing",
                title=it.get("event", it.get("title", "—")),
                currency=it.get("country", ""),
                impact=impact,
                event_time=ts,
                forecast=str(it.get("forecast", "")),
                previous=str(it.get("previous", "")),
                actual=str(it.get("actual", "")),
            ))

        self._cache_set(cache_key, out)
        return out


# =============================================================================
# 3) FXDAILY (via FXStreet Calendar API)
# =============================================================================
class FXDailyFetcher(BaseFetcher):
    """
    FXDaily.Live does not expose a public API.
    We use FXStreet's Economic Calendar API as the nearest equivalent
    (requires FXSTREET_CLIENT_ID + FXSTREET_CLIENT_SECRET env vars).
    Fallback: empty list.
    """

    name = "fxdaily"
    cache_ttl = 600

    TOKEN_URL = "https://calendar-api.fxstreet.com/en/api/v1/oauth2/token"
    EVENTS_URL = "https://calendar-api.fxstreet.com/en/api/v1/events"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._token: Optional[str] = None
        self._token_exp: float = 0.0

    def _get_token(self) -> Optional[str]:
        if self._token and _time.time() < self._token_exp - 60:
            return self._token
        cid = os.getenv("FXSTREET_CLIENT_ID")
        secret = os.getenv("FXSTREET_CLIENT_SECRET")
        if not cid or not secret:
            return None
        try:
            r = self._session.post(
                self.TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": cid,
                    "client_secret": secret,
                    "scope": "calendar",
                },
                timeout=15,
            )
            r.raise_for_status()
            payload = r.json()
            self._token = payload["access_token"]
            self._token_exp = _time.time() + int(payload.get("expires_in", 3600))
            return self._token
        except Exception as exc:
            logger.warning("[fxdaily] token failed: %s", exc)
            return None

    def fetch(self, days: int = 2) -> list[EconomicEvent]:
        token = self._get_token()
        if not token:
            logger.info("[fxdaily] FXStreet credentials not set — source skipped")
            return []

        cache_key = f"fxd:{days}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        now = utcnow()
        params = {
            "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "volatility": "high,medium",
        }
        data = self._get_json(
            self.EVENTS_URL,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if not data:
            return []

        out: list[EconomicEvent] = []
        items = data if isinstance(data, list) else data.get("events", [])

        for it in items:
            ts = _parse_iso(it.get("date") or it.get("datetime") or "")
            if ts is None:
                continue
            impact = str(it.get("volatility", "medium")).lower()
            if impact not in ("high", "medium"):
                continue

            out.append(EconomicEvent(
                source="fxdaily",
                title=it.get("name", it.get("title", "—")),
                currency=it.get("currency", ""),
                impact=impact,
                event_time=ts,
                forecast=str(it.get("consensus", "")),
                previous=str(it.get("previous", "")),
                actual=str(it.get("actual", "")),
            ))

        self._cache_set(cache_key, out)
        return out


# =============================================================================
# 4) CPI RELEASES (BLS)
# =============================================================================
class CPIFetcher(BaseFetcher):
    """
    Uses the BLS Public Data API (no key needed for v1 limits).
    Falls back to the official BLS release-schedule page scrape.
    """

    name = "cpi"
    cache_ttl = 3600

    BLS_API = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
    CPI_SERIES = "CUUR0000SA0"        # All items, US city average, NSA

    def fetch(self, months_back: int = 3) -> list[CPIRelease]:
        cache_key = f"cpi:{months_back}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        releases = self._fetch_bls_api(months_back)
        if not releases:
            releases = self._fetch_schedule()

        self._cache_set(cache_key, releases)
        return releases

    # ------------------------------------------------------------------ api
    def _fetch_bls_api(self, months_back: int) -> list[CPIRelease]:
        now = utcnow()
        start_year = (now - timedelta(days=31 * months_back)).year
        payload = {
            "seriesid": [self.CPI_SERIES],
            "startyear": str(start_year),
            "endyear": str(now.year),
        }
        try:
            r = self._session.post(self.BLS_API, json=payload, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            logger.warning("[cpi] BLS API failed: %s", exc)
            return []

        out: list[CPIRelease] = []
        for series in data.get("Results", {}).get("series", []):
            for row in series.get("data", []):
                period = f"{row.get('year')}-{row.get('period', 'M00')[1:]}"
                val = row.get("value")
                if val is None:
                    continue
                try:
                    val_f = float(str(val).replace(",", ""))
                except ValueError:
                    continue

                # BLS publishes CPI at 08:30 ET on the release day; we
                # only have the month here, so estimate the release window.
                release_dt = datetime(
                    int(row.get("year", now.year)),
                    int(row.get("period", "M01")[1:]),
                    15, 12, 30, tzinfo=timezone.utc,
                )
                out.append(CPIRelease(
                    period=period,
                    release_datetime=release_dt,
                    cpi_value=val_f,
                    source="bls_api",
                ))
        return out

    # ------------------------------------------------------------------ schedule
    def _fetch_schedule(self) -> list[CPIRelease]:
        # Minimal fallback: parse the current-year BLS schedule page.
        url = "https://www.bls.gov/schedule/news_release/cpi.htm"
        html = self._get_text(url)
        if not html:
            return []

        import re
        out: list[CPIRelease] = []
        # Very defensive regex — BLS pages use a stable table layout.
        for m in re.finditer(
            r"(\w+)\s+(\d{1,2}),\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)",
            html,
        ):
            month_s, day_s, year_s, hh, mm, ampm = m.groups()
            try:
                hour = int(hh) % 12 + (12 if ampm.upper() == "PM" else 0)
                dt = datetime(
                    int(year_s), _month_num(month_s), int(day_s),
                    hour, int(mm), tzinfo=timezone.utc,
                )
            except Exception:
                continue
            out.append(CPIRelease(
                period=f"{year_s}-{_month_num(month_s):02d}",
                release_datetime=dt,
                source="bls_schedule",
            ))
        return out


def _month_num(name: str) -> int:
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    return months.get(name.lower(), 1)


# =============================================================================
# 5) COT / CFTC
# =============================================================================
class COTFetcher(BaseFetcher):
    """
    Uses `cftc-cot-soda` SDK against the official CFTC SODA2 API.
    Returns both COTReport objects and (optionally) calendar-style events.
    """

    name = "cot"
    cache_ttl = 86400                  # COT updates weekly

    DEFAULT_MARKETS = [
        "Euro FX", "British Pound", "Japanese Yen", "Swiss Franc",
        "Canadian Dollar", "Australian Dollar", "New Zealand Dollar",
        "Gold", "Silver", "Crude Oil", "S&P 500",
    ]

    def fetch(
        self,
        markets: Optional[Iterable[str]] = None,
        weeks: int = 4,
    ) -> list[COTReport]:
        try:
            from cftc_cot import COTClient  # type: ignore
        except ImportError:
            logger.warning("[cot] cftc-cot-soda not installed")
            return []

        cache_key = f"cot:{','.join(markets or self.DEFAULT_MARKETS)}:{weeks}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        client = COTClient(cache="memory")
        out: list[COTReport] = []

        for market in (markets or self.DEFAULT_MARKETS):
            try:
                df = (
                    client.legacy()
                    .market(market)
                    .last_n_weeks(weeks)
                    .execute()
                )
            except Exception as exc:
                logger.warning("[cot] fetch %s failed: %s", market, exc)
                continue
            if df is None or len(df) == 0:
                continue

            # Take the latest row per market
            row = df.iloc[-1].to_dict()
            report_date = row.get("report_date_as_yyyy_mm_dd")
            rd = None
            if report_date:
                try:
                    rd = datetime.fromisoformat(str(report_date)[:10]).replace(
                        tzinfo=timezone.utc
                    )
                except Exception:
                    rd = None

            out.append(COTReport(
                market=market,
                report_date=rd,
                noncomm_long=_f(row.get("noncomm_positions_long_all")),
                noncomm_short=_f(row.get("noncomm_positions_short_all")),
                noncomm_net=_f(row.get("noncomm_net")),
                commercial_long=_f(row.get("comm_positions_long_all")),
                commercial_short=_f(row.get("comm_positions_short_all")),
                commercial_net=_f(row.get("comm_net")),
                open_interest=_f(row.get("open_interest_all")),
                dataset="legacy",
                raw=row,
            ))

        self._cache_set(cache_key, out)
        return out


class CFTCFetcher(COTFetcher):
    """
    Same SDK, different datasets — exposes 'disaggregated' and 'tff'
    reports in addition to legacy.
    """

    name = "cftc"

    def fetch_all_datasets(self, weeks: int = 4) -> dict[str, list[COTReport]]:
        try:
            from cftc_cot import COTClient  # type: ignore
        except ImportError:
            return {"legacy": [], "disaggregated": [], "tff": []}

        client = COTClient(cache="memory")
        out: dict[str, list[COTReport]] = {
            "legacy": [], "disaggregated": [], "tff": [],
        }

        for dataset in ("legacy", "disaggregated", "tff"):
            for market in self.DEFAULT_MARKETS:
                try:
                    method = getattr(client, dataset)
                    df = method().market(market).last_n_weeks(weeks).execute()
                except Exception:
                    continue
                if df is None or len(df) == 0:
                    continue
                row = df.iloc[-1].to_dict()
                out[dataset].append(COTReport(
                    market=market,
                    report_date=_parse_iso(
                        str(row.get("report_date_as_yyyy_mm_dd", ""))[:10]
                    ),
                    noncomm_long=_f(row.get("noncomm_positions_long_all")),
                    noncomm_short=_f(row.get("noncomm_positions_short_all")),
                    noncomm_net=_f(row.get("noncomm_net")),
                    dataset=dataset,
                    raw=row,
                ))
        return out


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


# =============================================================================
# TRADING GUARD
# =============================================================================
@dataclass
class GuardPermission:
    allowed: bool
    code: str
    reason: str
    blocked_until: Optional[datetime] = None
    event: Optional[dict] = None

    def __bool__(self) -> bool:
        return self.allowed


class TradingGuard:
    """
    Enforces the news embargo:
        T-5m  →  suspend new entries
        T+10m →  re-enable (stabilization window)

    Only `impact == 'high'` events trigger the embargo.
    """

    PRE_MINUTES = 5
    POST_MINUTES = 10

    def __init__(self, fetchers: Optional[list[BaseFetcher]] = None):
        self.fetchers: list[BaseFetcher] = fetchers or [
            ForexFactoryFetcher(),
            InvestingComFetcher(),
            FXDailyFetcher(),
            CPIFetcher(),
            COTFetcher(),
            CFTCFetcher(),
        ]
        self._events_cache: list[EconomicEvent] = []
        self._events_cache_ts: float = 0.0
        self._events_ttl = 180          # refresh every 3 minutes
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- refresh
    def _refresh_events(self, force: bool = False) -> list[EconomicEvent]:
        with self._lock:
            if not force and (time.time() - self._events_cache_ts) < self._events_ttl:
                return list(self._events_cache)

            all_events: list[EconomicEvent] = []

            # Calendar fetchers
            for fetcher in self.fetchers:
                try:
                    items = fetcher.fetch()
                except Exception as exc:
                    logger.warning("[guard] fetcher %s failed: %s", fetcher.name, exc)
                    continue

                for it in items:
                    if isinstance(it, EconomicEvent):
                        all_events.append(it)
                    elif isinstance(it, CPIRelease):
                        # Convert CPI release into a high-impact event
                        all_events.append(EconomicEvent(
                            source="cpi",
                            title="CPI Release",
                            currency="USD",
                            impact="high",
                            event_time=it.release_datetime,
                            detail=it.to_dict(),
                        ))
                    elif isinstance(it, COTReport):
                        # COT is not a timed event; skip for embargo purposes
                        continue

            # Normalize timezone + drop events without a timestamp
            normalized: list[EconomicEvent] = []
            for e in all_events:
                if e.event_time is None:
                    continue
                e.event_time = _to_utc(e.event_time)
                normalized.append(e)

            normalized.sort(key=lambda e: e.event_time)
            self._events_cache = normalized
            self._events_cache_ts = time.time()
            return list(normalized)

    # ---------------------------------------------------------------- embargo
    def _find_blocking_event(
        self,
        dt: datetime,
        high_impact_only: bool = True,
    ) -> Optional[EconomicEvent]:
        events = self._refresh_events()
        for e in events:
            if high_impact_only and not e.is_high_impact:
                continue
            start = e.event_time - timedelta(minutes=self.PRE_MINUTES)
            end = e.event_time + timedelta(minutes=self.POST_MINUTES)
            if start <= dt <= end:
                return e
        return None

    # ---------------------------------------------------------------- gate
    def can_trade(
        self,
        symbol: str = "EUR/USD",
        style: str = "Swing",
        dt: Optional[datetime] = None,
        high_impact_only: bool = True,
    ) -> GuardPermission:
        dt = _to_utc(dt)

        event = self._find_blocking_event(dt, high_impact_only=high_impact_only)
        if event is None:
            return GuardPermission(
                True, "ok", "No high-impact news embargo active"
            )

        blocked_until = event.event_time + timedelta(minutes=self.POST_MINUTES)
        return GuardPermission(
            False,
            "news_embargo",
            (
                f"News embargo: {event.title} ({event.currency}) "
                f"at {event.event_time.strftime('%H:%M UTC')} — "
                f"trading suspended until {blocked_until.strftime('%H:%M UTC')}"
            ),
            blocked_until=blocked_until,
            event=event.to_dict(),
        )

    # ---------------------------------------------------------------- snapshot
    def snapshot(self, dt: Optional[datetime] = None) -> dict:
        dt = _to_utc(dt)
        events = self._refresh_events()
        now_block = self._find_blocking_event(dt, high_impact_only=True)

        upcoming = [
            e for e in events
            if e.is_high_impact and e.event_time >= dt - timedelta(minutes=30)
        ][:10]

        return {
            "now_utc": dt.isoformat(),
            "can_trade": now_block is None,
            "blocking_event": now_block.to_dict() if now_block else None,
            "pre_minutes": self.PRE_MINUTES,
            "post_minutes": self.POST_MINUTES,
            "high_impact_events": [e.to_dict() for e in upcoming],
            "total_events_loaded": len(events),
        }

    # ---------------------------------------------------------------- convenience
    def next_high_impact(self, dt: Optional[datetime] = None) -> Optional[EconomicEvent]:
        dt = _to_utc(dt)
        events = self._refresh_events()
        for e in events:
            if e.is_high_impact and e.event_time >= dt:
                return e
        return None


# =============================================================================
# SINGLETON + MODULE-LEVEL HELPERS
# =============================================================================
_guard: Optional[TradingGuard] = None


def get_guard() -> TradingGuard:
    global _guard
    if _guard is None:
        _guard = TradingGuard()
    return _guard


def can_trade(
    symbol: str = "EUR/USD",
    style: str = "Swing",
    dt: Optional[datetime] = None,
) -> GuardPermission:
    return get_guard().can_trade(symbol=symbol, style=style, dt=dt)


def snapshot(dt: Optional[datetime] = None) -> dict:
    return get_guard().snapshot(dt)


# =============================================================================
# SELF-TEST
# =============================================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    guard = get_guard()
    snap = guard.snapshot()

    print("=" * 72)
    print("  TradingGuard self-test")
    print("=" * 72)
    print(json.dumps(snap, indent=2, ensure_ascii=False, default=str))

    print("\n--- Scenario checks ---")
    scenarios = [
        ("Now", utcnow()),
        ("+2 minutes", utcnow() + timedelta(minutes=2)),
        ("+15 minutes", utcnow() + timedelta(minutes=15)),
    ]
    for label, dt in scenarios:
        perm = guard.can_trade("EUR/USD", "Swing", dt=dt)
        print(f"{label:<14} allowed={perm.allowed!s:<5} code={perm.code}")
