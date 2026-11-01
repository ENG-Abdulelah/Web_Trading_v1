# session_guard.py
"""
Session & Market State Guard — real market state validation.

Responsibilities:
  • Detect weekend closure (Fri 21:00 UTC → Sun 21:00 UTC).
  • Detect active sessions (Sydney / Tokyo / London / NY).
  • Detect active ICT kill zones.
  • Block analysis during dead liquidity and market transitions.
  • Signal weekend flatten (close all positions before Friday close).
  • Instrument-specific Friday early closes (XAU/XAG/WTI).

Developed & Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Dict, List, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_hhmm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(hour=h, minute=m)


def _in_window(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # crosses midnight


# =============================================================================
# SESSIONS (UTC)
# =============================================================================
SESSIONS = {
    "sydney":   {"name": "Sydney",   "name_ar": "سيدني",   "start": "21:00", "end": "06:00"},
    "tokyo":    {"name": "Tokyo",    "name_ar": "طوكيو",   "start": "00:00", "end": "09:00"},
    "london":   {"name": "London",   "name_ar": "لندن",    "start": "07:00", "end": "16:00"},
    "new_york": {"name": "New York", "name_ar": "نيويورك", "start": "12:00", "end": "21:00"},
}

# =============================================================================
# KILL ZONES (UTC)
# =============================================================================
KILL_ZONES = {
    "asian_kz": {
        "name": "Asian KZ", "name_ar": "منطقة قتل آسيا",
        "start": "00:00", "end": "03:00",
        "priority": "medium", "styles": ["Swing"],
    },
    "london_kz": {
        "name": "London KZ", "name_ar": "منطقة قتل لندن",
        "start": "07:00", "end": "10:00",
        "priority": "high", "styles": ["Swing", "Scalping"],
    },
    "ny_am_kz": {
        "name": "NY AM KZ", "name_ar": "قتل نيويورك صباحاً",
        "start": "12:00", "end": "15:00",
        "priority": "high", "styles": ["Swing", "Scalping"],
    },
    "london_close_kz": {
        "name": "London Close KZ", "name_ar": "إغلاق لندن",
        "start": "15:00", "end": "17:00",
        "priority": "medium", "styles": ["Swing"],
    },
}

# =============================================================================
# DEAD LIQUIDITY (UTC)
# =============================================================================
DEAD_LIQUIDITY = {
    "asian_lunch":  {"name": "Asian Lunch",     "start": "03:00", "end": "05:00"},
    "london_lunch": {"name": "London Lull",     "start": "10:00", "end": "11:00"},
    "ny_lunch":     {"name": "NY Lunch",        "start": "16:00", "end": "17:00"},
    "rollover":     {"name": "Daily Rollover",  "start": "20:55", "end": "21:15"},
}

# =============================================================================
# MARKET TRANSITIONS (UTC)
# =============================================================================
MARKET_TRANSITIONS = {
    "london_pre":  {"name": "London Pre-Open",           "start": "06:45", "end": "07:15"},
    "ny_pre":      {"name": "NY Pre-Open",               "start": "11:45", "end": "12:15"},
    "london_close_trans": {"name": "London Close Transition", "start": "15:45", "end": "16:15"},
}

# =============================================================================
# WEEKEND RULES (UTC)
# =============================================================================
FOREX_CLOSE_FRIDAY_HOUR = 21
FOREX_OPEN_SUNDAY_HOUR = 21

# Instruments that close earlier on Fridays (UTC hour)
EARLY_CLOSE = {
    "XAU/USD": 20,
    "XAG/USD": 20,
    "WTI/USD": 20,
}

# Timeframe presets per style
SWING_TIMEFRAMES = ["1W", "1D", "4H", "1H"]
SCALPING_TIMEFRAMES = ["1W", "1D", "1H", "5M"]

# Close all positions X minutes before Friday 21:00 UTC
FLATTEN_MINUTES_BEFORE_CLOSE = 15


# =============================================================================
# DATA CLASSES
# =============================================================================
@dataclass
class MarketState:
    is_open: bool
    is_weekend: bool
    active_sessions: List[Dict] = field(default_factory=list)
    active_kill_zones: List[Dict] = field(default_factory=list)
    dead_liquidity: Optional[str] = None
    market_transition: Optional[str] = None
    blocked_reason: str = ""
    can_trade_swing: bool = False
    can_trade_scalping: bool = False
    next_session: Optional[Dict] = None
    now_utc: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# =============================================================================
# SESSION GUARD
# =============================================================================
class SessionGuard:
    """Real-market state validator. Never uses random or mock data."""

    def __init__(self):
        self._sessions = {
            k: {**v, "start_t": _parse_hhmm(v["start"]),
                "end_t": _parse_hhmm(v["end"])}
            for k, v in SESSIONS.items()
        }
        self._kill_zones = {
            k: {**v, "start_t": _parse_hhmm(v["start"]),
                "end_t": _parse_hhmm(v["end"])}
            for k, v in KILL_ZONES.items()
        }
        self._dead = {
            k: {**v, "start_t": _parse_hhmm(v["start"]),
                "end_t": _parse_hhmm(v["end"])}
            for k, v in DEAD_LIQUIDITY.items()
        }
        self._transitions = {
            k: {**v, "start_t": _parse_hhmm(v["start"]),
                "end_t": _parse_hhmm(v["end"])}
            for k, v in MARKET_TRANSITIONS.items()
        }

    # ------------------------------------------------------------------
    def _is_weekend(self, dt: datetime) -> bool:
        wd = dt.weekday()  # Mon=0 ... Sun=6
        if wd == 5:  # Saturday
            return True
        if wd == 4 and dt.hour >= FOREX_CLOSE_FRIDAY_HOUR:  # Friday close
            return True
        if wd == 6 and dt.hour < FOREX_OPEN_SUNDAY_HOUR:    # Sunday pre-open
            return True
        return False

    def _active_sessions(self, dt: datetime) -> List[Dict]:
        t = dt.time()
        out = []
        for k, s in self._sessions.items():
            if _in_window(t, s["start_t"], s["end_t"]):
                out.append({
                    "key": k, "name": s["name"], "name_ar": s["name_ar"],
                    "start": s["start"], "end": s["end"],
                })
        return out

    def _active_kill_zones(self, dt: datetime) -> List[Dict]:
        t = dt.time()
        out = []
        for k, z in self._kill_zones.items():
            if _in_window(t, z["start_t"], z["end_t"]):
                out.append({
                    "key": k, "name": z["name"], "name_ar": z["name_ar"],
                    "start": z["start"], "end": z["end"],
                    "priority": z["priority"], "styles": z["styles"],
                })
        return out

    def _dead_zone(self, dt: datetime) -> Optional[str]:
        t = dt.time()
        for z in self._dead.values():
            if _in_window(t, z["start_t"], z["end_t"]):
                return z["name"]
        return None

    def _transition(self, dt: datetime) -> Optional[str]:
        t = dt.time()
        for z in self._transitions.values():
            if _in_window(t, z["start_t"], z["end_t"]):
                return z["name"]
        return None

    def _next_session(self, dt: datetime) -> Optional[Dict]:
        now_min = dt.hour * 60 + dt.minute
        best: Optional[tuple] = None
        for k, s in self._sessions.items():
            start_min = s["start_t"].hour * 60 + s["start_t"].minute
            delta = start_min - now_min
            if delta <= 0:
                delta += 1440
            if best is None or delta < best[1]:
                best = (s, delta, k)
        if best:
            s, delta, k = best
            return {
                "key": k, "name": s["name"], "name_ar": s["name_ar"],
                "in_minutes": delta,
            }
        return None

    # ------------------------------------------------------------------
    def state(self, dt: Optional[datetime] = None) -> MarketState:
        dt = dt or utcnow()
        weekend = self._is_weekend(dt)
        sessions = self._active_sessions(dt) if not weekend else []
        kzs = self._active_kill_zones(dt) if not weekend else []
        dead = self._dead_zone(dt)
        trans = self._transition(dt)

        blocked = ""
        is_open = True

        if weekend:
            is_open = False
            blocked = "Market closed — weekend"
        elif dead:
            is_open = False
            blocked = f"Dead liquidity zone: {dead}"
        elif trans:
            is_open = False
            blocked = f"Market transition: {trans}"

        high_kz = [k for k in kzs if k["priority"] == "high"]
        can_scalp = bool(is_open and high_kz)
        can_swing = bool(is_open)

        return MarketState(
            is_open=is_open,
            is_weekend=weekend,
            active_sessions=sessions,
            active_kill_zones=kzs,
            dead_liquidity=dead,
            market_transition=trans,
            blocked_reason=blocked,
            can_trade_swing=can_swing,
            can_trade_scalping=can_scalp,
            next_session=self._next_session(dt) if weekend else None,
            now_utc=dt.isoformat(),
        )

    # ------------------------------------------------------------------
    def is_instrument_closing(self, symbol: str,
                              dt: Optional[datetime] = None) -> bool:
        """Friday early-close for specific instruments (XAU, XAG, WTI)."""
        dt = dt or utcnow()
        if dt.weekday() != 4:  # Not Friday
            return False
        close_hour = EARLY_CLOSE.get(symbol)
        return close_hour is not None and dt.hour >= close_hour

    # ------------------------------------------------------------------
    def should_flatten_positions(self,
                                 dt: Optional[datetime] = None) -> bool:
        """
        Returns True if:
          • Already weekend (must be flat), OR
          • Within FLATTEN_MINUTES_BEFORE_CLOSE of Friday 21:00 UTC.
        """
        dt = dt or utcnow()

        # Already weekend → must be flat
        if self._is_weekend(dt):
            return True

        # Friday within X minutes of close
        if dt.weekday() == 4:
            close_dt = dt.replace(
                hour=FOREX_CLOSE_FRIDAY_HOUR,
                minute=0, second=0, microsecond=0,
            )
            delta_min = (close_dt - dt).total_seconds() / 60.0
            if 0 <= delta_min <= FLATTEN_MINUTES_BEFORE_CLOSE:
                return True

        return False

    # ------------------------------------------------------------------
    def minutes_to_weekend_close(
        self, dt: Optional[datetime] = None,
    ) -> Optional[float]:
        """Minutes until Friday 21:00 UTC close (None if not Friday)."""
        dt = dt or utcnow()
        if dt.weekday() != 4:
            return None
        close_dt = dt.replace(
            hour=FOREX_CLOSE_FRIDAY_HOUR,
            minute=0, second=0, microsecond=0,
        )
        return max(0.0, (close_dt - dt).total_seconds() / 60.0)
