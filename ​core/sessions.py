"""
Session manager for Web_Trading_v1.

Responsibilities:
  • Detect active trading sessions (Sydney / Tokyo / London / NY).
  • Detect active ICT kill zones (with style filtering).
  • Hard-block trades during:
       - weekends (with instrument-specific early closes)
       - dead liquidity zones
       - market transition windows
  • Provide a single `.can_trade(style, symbol)` gate.
  • Provide a `.snapshot()` dict for the web dashboard.

Developed & Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Optional

import config


# =============================================================================
# INTERNAL HELPERS
# =============================================================================
def _parse_hhmm(s: str) -> time:
    """Parse 'HH:MM' into a naive time object."""
    h, m = map(int, s.split(":"))
    return time(hour=h, minute=m)


def _to_utc(dt: Optional[datetime]) -> datetime:
    """Coerce any datetime to aware-UTC; default to now()."""
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _in_window(t: time, start: time, end: time) -> bool:
    """
    Return True if `t` falls in [start, end).
    Supports cross-midnight windows (e.g. 21:00 → 06:00).
    """
    if start <= end:
        return start <= t < end
    return t >= start or t < end


# =============================================================================
# DATA STRUCTURES
# =============================================================================
@dataclass
class TimeWindow:
    key: str
    name: str
    name_ar: str
    start: time
    end: time
    priority: str = "medium"
    styles: list[str] = field(default_factory=list)

    def contains(self, dt: datetime) -> bool:
        """`dt` MUST already be aware-UTC (callers go through _to_utc)."""
        return _in_window(dt.time(), self.start, self.end)

    def duration_minutes(self) -> int:
        s = self.start.hour * 60 + self.start.minute
        e = self.end.hour   * 60 + self.end.minute
        if e <= s:
            e += 1440
        return e - s

    def to_dict(self) -> dict:
        return {
            "key":      self.key,
            "name":     self.name,
            "name_ar":  self.name_ar,
            "start":    self.start.strftime("%H:%M"),
            "end":      self.end.strftime("%H:%M"),
            "priority": self.priority,
            "styles":   list(self.styles),
        }


@dataclass
class TradingPermission:
    allowed: bool
    code: str
    reason: str
    details: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.allowed


# =============================================================================
# SESSION MANAGER
# =============================================================================
class SessionManager:
    """Central gatekeeper for trading windows, kill zones and closures."""

    def __init__(self, cfg=config):
        self.cfg = cfg

        self._sessions: dict[str, TimeWindow] = {
            k: TimeWindow(
                key=k,
                name=v["name"],
                name_ar=v["name_ar"],
                start=_parse_hhmm(v["start"]),
                end=_parse_hhmm(v["end"]),
            )
            for k, v in cfg.SESSIONS.items()
            if v.get("active", True)
        }

        self._kill_zones: dict[str, TimeWindow] = {
            k: TimeWindow(
                key=k,
                name=v["name"],
                name_ar=v["name_ar"],
                start=_parse_hhmm(v["start"]),
                end=_parse_hhmm(v["end"]),
                priority=v.get("priority", "medium"),
                styles=list(v.get("styles", [])),
            )
            for k, v in cfg.KILL_ZONES.items()
        }

        self._dead_zones: dict[str, TimeWindow] = {
            k: TimeWindow(
                key=k,
                name=v["name"],
                name_ar=v["name_ar"],
                start=_parse_hhmm(v["start"]),
                end=_parse_hhmm(v["end"]),
            )
            for k, v in cfg.DEAD_LIQUIDITY_ZONES.items()
        }

        self._transitions: dict[str, TimeWindow] = {
            k: TimeWindow(
                key=k,
                name=v["name"],
                name_ar=v["name_ar"],
                start=_parse_hhmm(v["start"]),
                end=_parse_hhmm(v["end"]),
            )
            for k, v in cfg.MARKET_TRANSITIONS.items()
        }

    # -------------------------------------------------------------------------
    # CLOCK
    # -------------------------------------------------------------------------
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    # -------------------------------------------------------------------------
    # WEEKEND / CLOSURES
    # -------------------------------------------------------------------------
    def is_weekend(self, dt: Optional[datetime] = None) -> bool:
        """Forex weekend = Fri ≥ close_hour → Sun < open_hour (UTC)."""
        dt = _to_utc(dt)
        wd = dt.weekday()  # Mon=0 ... Sun=6

        if wd == 5:  # Saturday
            return True
        if wd == 4 and dt.hour >= self.cfg.FOREX_CLOSE_FRIDAY_HOUR:
            return True
        if wd == 6 and dt.hour < self.cfg.FOREX_OPEN_SUNDAY_HOUR:
            return True
        return False

    def is_market_open(self, dt: Optional[datetime] = None) -> bool:
        return not self.is_weekend(dt)

    # -------------------------------------------------------------------------
    # SESSIONS
    # -------------------------------------------------------------------------
    def get_active_sessions(self, dt: Optional[datetime] = None) -> list[TimeWindow]:
        dt = _to_utc(dt)
        return [s for s in self._sessions.values() if s.contains(dt)]

    def get_current_session(self, dt: Optional[datetime] = None) -> Optional[TimeWindow]:
        active = self.get_active_sessions(dt)
        return active[0] if active else None

    # -------------------------------------------------------------------------
    # KILL ZONES
    # -------------------------------------------------------------------------
    def get_active_kill_zones(
        self,
        dt: Optional[datetime] = None,
        style: Optional[str] = None,
    ) -> list[TimeWindow]:
        dt = _to_utc(dt)
        out: list[TimeWindow] = []
        for kz in self._kill_zones.values():
            if not kz.contains(dt):
                continue
            if style and kz.styles and style not in kz.styles:
                continue
            out.append(kz)
        return out

    def get_high_priority_kill_zones(
        self, dt: Optional[datetime] = None
    ) -> list[TimeWindow]:
        return [kz for kz in self.get_active_kill_zones(dt) if kz.priority == "high"]

    # -------------------------------------------------------------------------
    # DEAD LIQUIDITY
    # -------------------------------------------------------------------------
    def get_dead_zone(self, dt: Optional[datetime] = None) -> Optional[TimeWindow]:
        dt = _to_utc(dt)
        for z in self._dead_zones.values():
            if z.contains(dt):
                return z
        return None

    def is_dead_liquidity(self, dt: Optional[datetime] = None) -> bool:
        return self.get_dead_zone(dt) is not None

    # -------------------------------------------------------------------------
    # MARKET TRANSITIONS
    # -------------------------------------------------------------------------
    def get_transition(self, dt: Optional[datetime] = None) -> Optional[TimeWindow]:
        dt = _to_utc(dt)
        for t in self._transitions.values():
            if t.contains(dt):
                return t
        return None

    def is_market_transition(self, dt: Optional[datetime] = None) -> bool:
        return self.get_transition(dt) is not None

    # -------------------------------------------------------------------------
    # MAIN GATE
    # -------------------------------------------------------------------------
    def can_trade(
        self,
        style: str = "Swing",
        symbol: str = "EUR/USD",
        dt: Optional[datetime] = None,
    ) -> TradingPermission:
        """
        Single source of truth for whether a new trade may be opened.

        Order of hard blocks:
          1. invalid style
          2. weekend
          3. instrument-specific Friday early-close
          4. dead liquidity zone
          5. market transition window
          6. no live session
          7. style-specific kill-zone requirement (Scalping only)
        """
        dt = _to_utc(dt)

        # 1) Style validation
        if style not in self.cfg.TRADING_STYLES:
            return TradingPermission(
                False, "invalid_style", f"Unknown trading style: {style}"
            )

        # 2) Weekend
        if self.is_weekend(dt):
            return TradingPermission(
                False, "weekend", "Forex market is closed for the weekend",
                {"dt": dt.isoformat()},
            )

        # 3) Friday early close for specific symbols
        if dt.weekday() == 4:  # Friday
            close_hour = self.cfg.EARLY_CLOSE_INSTRUMENTS.get(symbol)
            if close_hour is not None and dt.hour >= close_hour:
                return TradingPermission(
                    False, "early_close",
                    f"{symbol} closed early for the weekend",
                    {"close_hour_utc": close_hour},
                )

        # 4) Dead liquidity
        dz = self.get_dead_zone(dt)
        if dz is not None:
            return TradingPermission(
                False, "dead_liquidity",
                f"Dead liquidity zone active: {dz.name}",
                {"zone": dz.to_dict()},
            )

        # 5) Market transition
        tr = self.get_transition(dt)
        if tr is not None:
            return TradingPermission(
                False, "market_transition",
                f"Market transition window: {tr.name}",
                {"transition": tr.to_dict()},
            )

        # 6) Must have at least one live session
        active_sessions = self.get_active_sessions(dt)
        if not active_sessions:
            return TradingPermission(
                False, "no_session",
                "No live trading session at this time",
            )

        # 7) Style-specific gating
        if style == "Scalping":
            kzs = self.get_active_kill_zones(dt, style="Scalping")
            if not kzs:
                return TradingPermission(
                    False, "no_scalping_kill_zone",
                    "Scalping requires an active kill zone",
                    {"sessions": [s.to_dict() for s in active_sessions]},
                )
            high = [kz for kz in kzs if kz.priority == "high"]
            if not high:
                return TradingPermission(
                    False, "no_high_priority_kill_zone",
                    "Scalping requires a high-priority kill zone",
                    {"kill_zones": [k.to_dict() for k in kzs]},
                )
            return TradingPermission(
                True, "ok",
                "Scalping allowed inside a high-priority kill zone",
                {
                    "sessions":   [s.to_dict() for s in active_sessions],
                    "kill_zones": [k.to_dict() for k in high],
                },
            )

        # Swing — allowed during any live session, extra context if in KZ
        swing_kzs = self.get_active_kill_zones(dt, style="Swing")
        return TradingPermission(
            True, "ok",
            "Swing trading allowed",
            {
                "sessions":     [s.to_dict() for s in active_sessions],
                "kill_zones":   [k.to_dict() for k in swing_kzs],
                "in_kill_zone": bool(swing_kzs),
            },
        )

    # -------------------------------------------------------------------------
    # NEXT-WINDOW QUERIES
    # -------------------------------------------------------------------------
    def _minutes_until(self, dt: datetime, target: time) -> int:
        now_minutes = dt.hour * 60 + dt.minute
        tgt_minutes = target.hour * 60 + target.minute
        delta = tgt_minutes - now_minutes
        if delta <= 0:
            delta += 1440
        return delta

    def get_next_window(
        self,
        windows: dict[str, TimeWindow],
        dt: Optional[datetime] = None,
    ) -> Optional[tuple[TimeWindow, int]]:
        """Return (window, minutes_until_start) for the soonest future window."""
        dt = _to_utc(dt)
        best: Optional[tuple[TimeWindow, int]] = None
        for w in windows.values():
            delta = self._minutes_until(dt, w.start)
            if best is None or delta < best[1]:
                best = (w, delta)
        return best

    def get_next_session(self, dt: Optional[datetime] = None):
        return self.get_next_window(self._sessions, dt)

    def get_next_kill_zone(self, dt: Optional[datetime] = None):
        return self.get_next_window(self._kill_zones, dt)

    # -------------------------------------------------------------------------
    # SNAPSHOT (for the web dashboard)
    # -------------------------------------------------------------------------
    def snapshot(self, dt: Optional[datetime] = None) -> dict:
        dt = _to_utc(dt)
        active_sessions = self.get_active_sessions(dt)
        kzs             = self.get_active_kill_zones(dt)
        dz              = self.get_dead_zone(dt)
        tr              = self.get_transition(dt)
        next_sess       = self.get_next_session(dt)
        next_kz         = self.get_next_kill_zone(dt)

        swing_perm = self.can_trade("Swing", dt=dt)
        scalp_perm = self.can_trade("Scalping", dt=dt)

        return {
            "now_utc":             dt.isoformat(),
            "is_weekend":          self.is_weekend(dt),
            "is_market_open":      self.is_market_open(dt),
            "active_sessions":     [s.to_dict() for s in active_sessions],
            "active_kill_zones":   [k.to_dict() for k in kzs],
            "dead_liquidity_zone": dz.to_dict() if dz else None,
            "market_transition":   tr.to_dict() if tr else None,
            "next_session": (
                {"session": next_sess[0].to_dict(), "in_minutes": next_sess[1]}
                if next_sess else None
            ),
            "next_kill_zone": (
                {"zone": next_kz[0].to_dict(), "in_minutes": next_kz[1]}
                if next_kz else None
            ),
            "can_trade_swing":    swing_perm.allowed,
            "swing_reason":       swing_perm.reason,
            "swing_code":         swing_perm.code,
            "can_trade_scalping": scalp_perm.allowed,
            "scalping_reason":    scalp_perm.reason,
            "scalping_code":      scalp_perm.code,
        }


# =============================================================================
# MODULE-LEVEL SINGLETON + CONVENIENCE FUNCTIONS
# =============================================================================
_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    global _manager
    if _manager is None:
        _manager = SessionManager()
    return _manager


def can_trade(
    style: str = "Swing",
    symbol: str = "EUR/USD",
    dt: Optional[datetime] = None,
) -> TradingPermission:
    return get_session_manager().can_trade(style=style, symbol=symbol, dt=dt)


def snapshot(dt: Optional[datetime] = None) -> dict:
    return get_session_manager().snapshot(dt)


# =============================================================================
# SELF-TEST
# =============================================================================
if __name__ == "__main__":
    import json

    sm  = get_session_manager()
    now = sm.now()

    print("=" * 68)
    print(f"  SessionManager self-test  @  {now.isoformat()}")
    print("=" * 68)
    print(json.dumps(sm.snapshot(), indent=2, ensure_ascii=False))

    print("\n--- Scenario checks (fixed timestamps) ---")
    scenarios = [
        ("Friday 21:30 UTC (closed)",    datetime(2026, 10, 30, 21, 30, tzinfo=timezone.utc)),
        ("Saturday 12:00 UTC (closed)",  datetime(2026, 10, 31, 12, 0,  tzinfo=timezone.utc)),
        ("Sunday 20:00 UTC (closed)",    datetime(2026, 11, 1, 20, 0,   tzinfo=timezone.utc)),
        ("Sunday 21:30 UTC (opening)",   datetime(2026, 11, 1, 21, 30,  tzinfo=timezone.utc)),
        ("Mon 08:00 UTC (London KZ)",    datetime(2026, 11, 2, 8, 0,    tzinfo=timezone.utc)),
        ("Mon 10:30 UTC (London lunch)", datetime(2026, 11, 2, 10, 30,  tzinfo=timezone.utc)),
        ("Mon 13:00 UTC (NY AM KZ)",     datetime(2026, 11, 2, 13, 0,   tzinfo=timezone.utc)),
        ("Mon 21:05 UTC (rollover)",     datetime(2026, 11, 2, 21, 5,   tzinfo=timezone.utc)),
    ]

    for label, dt in scenarios:
        sw = sm.can_trade("Swing",    "EUR/USD", dt=dt)
        sc = sm.can_trade("Scalping", "EUR/USD", dt=dt)
        print(f"{label:<32}  Swing={str(sw.allowed):<5} ({sw.code:<26})  "
              f"Scalp={str(sc.allowed):<5} ({sc.code})")
