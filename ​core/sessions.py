# core/sessions.py
"""
core/sessions.py

Tracks live trading sessions (Asian, London, New York) and market
open/close times for the supported assets.

Provides an automatic shutdown mechanism: when the current time falls
outside the trading window for all enabled assets, registered callbacks
are invoked so the bot can safely halt.

All times are handled internally in UTC. Display helpers are provided
for conversion to other timezones (e.g., via pytz).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Callable, Dict, Iterable, List, Optional

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Enums & data models
# ---------------------------------------------------------------------
class SessionName(str, Enum):
    ASIAN = "asian"
    LONDON = "london"
    NEW_YORK = "new_york"


@dataclass(frozen=True)
class TradingSession:
    """A recurring daily trading session defined in UTC."""

    name: SessionName
    open_utc: time
    close_utc: time
    # If True, the session spans midnight (close < open).
    crosses_midnight: bool = False

    def contains(self, when_utc: datetime) -> bool:
        t = when_utc.timetz().replace(tzinfo=None)
        if self.crosses_midnight:
            return t >= self.open_utc or t < self.close_utc
        return self.open_utc <= t < self.close_utc

    def next_open_utc(self, when_utc: datetime) -> datetime:
        today_open = when_utc.replace(
            hour=self.open_utc.hour,
            minute=self.open_utc.minute,
            second=0,
            microsecond=0,
        )
        if today_open <= when_utc:
            today_open += timedelta(days=1)
        return today_open

    def next_close_utc(self, when_utc: datetime) -> datetime:
        today_close = when_utc.replace(
            hour=self.close_utc.hour,
            minute=self.close_utc.minute,
            second=0,
            microsecond=0,
        )
        if today_close <= when_utc:
            today_close += timedelta(days=1)
        return today_close


@dataclass(frozen=True)
class MarketHours:
    """
    Per-asset market open/close.

    weekdays = (0..6) where 0=Monday, 6=Sunday.
    Times are UTC. cross_midnight handles sessions like "Sun 22:00 -> Fri 21:00".
    """

    symbol: str
    weekdays: tuple = (0, 1, 2, 3, 4)  # Mon-Fri by default
    open_utc: time = time(0, 0)
    close_utc: time = time(23, 59)
    cross_midnight: bool = False

    def is_open(self, when_utc: datetime) -> bool:
        # NYSE-style: Sunday open at 22:00 counts as part of Monday window.
        weekday = when_utc.weekday()
        t = when_utc.timetz().replace(tzinfo=None)

        if self.cross_midnight:
            # Consider either "today after open" or "yesterday evening before close".
            if weekday in self.weekdays and t >= self.open_utc:
                return True
            prev_weekday = (weekday - 1) % 7
            if prev_weekday in self.weekdays and t < self.close_utc:
                return True
            return False

        return weekday in self.weekdays and self.open_utc <= t < self.close_utc

    def next_open_utc(self, when_utc: datetime) -> datetime:
        probe = when_utc
        for _ in range(8):  # at most a week forward
            if probe.weekday() in self.weekdays:
                candidate = probe.replace(
                    hour=self.open_utc.hour,
                    minute=self.open_utc.minute,
                    second=0,
                    microsecond=0,
                )
                if candidate > when_utc:
                    return candidate
            probe = (probe + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        raise RuntimeError(f"No market open found within a week for {self.symbol}")

    def next_close_utc(self, when_utc: datetime) -> datetime:
        probe = when_utc
        for _ in range(8):
            if probe.weekday() in self.weekdays:
                candidate = probe.replace(
                    hour=self.close_utc.hour,
                    minute=self.close_utc.minute,
                    second=0,
                    microsecond=0,
                )
                if candidate > when_utc:
                    return candidate
            probe = (probe + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        raise RuntimeError(f"No market close found within a week for {self.symbol}")


# ---------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------
# Standard forex-style sessions (UTC).
DEFAULT_SESSIONS: List[TradingSession] = [
    TradingSession(SessionName.ASIAN, time(0, 0), time(9, 0)),
    TradingSession(SessionName.LONDON, time(8, 0), time(17, 0)),
    TradingSession(SessionName.NEW_YORK, time(13, 0), time(22, 0)),
]

# Per-asset market hours.
# Forex = 24/5 (Sun 22:00 UTC -> Fri 22:00 UTC).
# Commodities have a daily maintenance/break window.
DEFAULT_MARKET_HOURS: Dict[str, MarketHours] = {
    "EUR/USD": MarketHours(
        symbol="EUR/USD",
        weekdays=(0, 1, 2, 3, 4, 6),  # Sun evening open
        open_utc=time(22, 0),
        close_utc=time(22, 0),
        cross_midnight=True,
    ),
    "GBP/USD": MarketHours(
        symbol="GBP/USD",
        weekdays=(0, 1, 2, 3, 4, 6),
        open_utc=time(22, 0),
        close_utc=time(22, 0),
        cross_midnight=True,
    ),
    "USD/JPY": MarketHours(
        symbol="USD/JPY",
        weekdays=(0, 1, 2, 3, 4, 6),
        open_utc=time(22, 0),
        close_utc=time(22, 0),
        cross_midnight=True,
    ),
    # XAU/USD ~ 23/5 with a 1-hour daily break (roughly 22:00-23:00 UTC).
    "XAU/USD": MarketHours(
        symbol="XAU/USD",
        weekdays=(0, 1, 2, 3, 4),
        open_utc=time(23, 0),
        close_utc=time(22, 0),
        cross_midnight=True,
    ),
    "XAG/USD": MarketHours(
        symbol="XAG/USD",
        weekdays=(0, 1, 2, 3, 4),
        open_utc=time(23, 0),
        close_utc=time(22, 0),
        cross_midnight=True,
    ),
    # WTI crude (CME) ~ 23:00-22:00 UTC with daily break; conservative default.
    "WTI/USD": MarketHours(
        symbol="WTI/USD",
        weekdays=(0, 1, 2, 3, 4, 6),
        open_utc=time(23, 0),
        close_utc=time(22, 0),
        cross_midnight=True,
    ),
}


# ---------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------
class SessionManager:
    """
    Coordinates session/market-hour tracking and triggers automatic
    shutdown callbacks when the market is closed.
    """

    def __init__(
        self,
        sessions: Optional[Iterable[TradingSession]] = None,
        market_hours: Optional[Dict[str, MarketHours]] = None,
        symbols: Optional[Iterable[str]] = None,
        shutdown_callbacks: Optional[List[Callable[[str], None]]] = None,
    ) -> None:
        self.sessions: List[TradingSession] = list(sessions or DEFAULT_SESSIONS)
        hours_map = market_hours or DEFAULT_MARKET_HOURS
        if symbols is not None:
            self.market_hours: Dict[str, MarketHours] = {
                s: hours_map[s] for s in symbols if s in hours_map
            }
        else:
            self.market_hours = dict(hours_map)

        self._shutdown_callbacks: List[Callable[[str], None]] = list(
            shutdown_callbacks or []
        )
        self._shutdown_fired: bool = False

    # -- session helpers -------------------------------------------------
    def active_sessions(self, when_utc: Optional[datetime] = None) -> List[SessionName]:
        when_utc = self._now(when_utc)
        return [s.name for s in self.sessions if s.contains(when_utc)]

    def session_for(self, name: SessionName) -> Optional[TradingSession]:
        for s in self.sessions:
            if s.name == name:
                return s
        return None

    def convert_session_time(
        self,
        session: TradingSession,
        when_utc: datetime,
        tz: str,
    ) -> datetime:
        return when_utc.astimezone(ZoneInfo(tz))

    # -- market-hour helpers --------------------------------------------
    def is_market_open(
        self,
        symbol: str,
        when_utc: Optional[datetime] = None,
    ) -> bool:
        when_utc = self._now(when_utc)
        hours = self.market_hours.get(symbol)
        if hours is None:
            logger.warning("No market hours registered for %s", symbol)
            return False
        return hours.is_open(when_utc)

    def any_market_open(self, when_utc: Optional[datetime] = None) -> bool:
        when_utc = self._now(when_utc)
        return any(h.is_open(when_utc) for h in self.market_hours.values())

    def next_event_utc(
        self,
        symbol: str,
        when_utc: Optional[datetime] = None,
    ) -> Dict[str, datetime]:
        """Return next open/close for a symbol."""
        when_utc = self._now(when_utc)
        hours = self.market_hours[symbol]
        return {
            "next_open": hours.next_open_utc(when_utc),
            "next_close": hours.next_close_utc(when_utc),
        }

    # -- shutdown handling ---------------------------------------------
    def register_shutdown_callback(self, cb: Callable[[str], None]) -> None:
        self._shutdown_callbacks.append(cb)

    def check_market_and_shutdown(
        self,
        when_utc: Optional[datetime] = None,
    ) -> bool:
        """
        If no market is open, invoke shutdown callbacks once and return True.
        Idempotent until reset() is called.
        """
        when_utc = self._now(when_utc)
        if self.any_market_open(when_utc):
            return False

        if self._shutdown_fired:
            return True

        self._shutdown_fired = True
        reason = f"All markets closed at {when_utc.isoformat()}"
        logger.warning("Automatic shutdown triggered: %s", reason)
        for cb in self._shutdown_callbacks:
            try:
                cb(reason)
            except Exception:
                logger.exception("Shutdown callback raised")
        return True

    def reset(self) -> None:
        """Reset the shutdown latch (e.g., at the start of a new session)."""
        self._shutdown_fired = False

    # -- internals -------------------------------------------------------
    @staticmethod
    def _now(when_utc: Optional[datetime]) -> datetime:
        if when_utc is None:
            return datetime.now(timezone.utc)
        if when_utc.tzinfo is None:
            return when_utc.replace(tzinfo=timezone.utc)
        return when_utc.astimezone(timezone.utc)
