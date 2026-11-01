"""
ICT / Smart Money Concepts Engine — 20 detectors.

Implements:
   1.  Order Block
   2.  Fair Value Gap
   3.  Liquidity Sweep
   4.  Market Structure Shift
   5.  Break of Structure
   6.  Kill Zones
   7.  Judas Swing
   8.  Balanced Price Range (BPR)
   9.  Breaker Block
  10.  Mitigation Block
  11.  Silver Bullet
  12.  Optimal Trade Entry (OTE)
  13.  Inversion FVG
  14.  Volume Gap
  15.  Inducement
  16.  Power of 3 (AMD)
  17.  Institutional Order Flow (IOF)
  18.  Premium/Discount Zones
  19.  Quarterly Theory
  20.  Daily Bias

Public API:
   • ICTEngine(df, symbol, timeframe, style).run_all() -> list[Setup]
   • analyze(df, symbol, timeframe, style) -> list[Setup]
   • analyze_and_track(df, symbol, timeframe, style) -> dict
     (also records usage in analysis.scorer)

Developed & Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dt_time, timezone
from typing import Optional

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Optional config (kill zones, time windows) — with safe fallbacks
# ---------------------------------------------------------------------------
try:
    import config  # type: ignore
except ImportError:
    config = None

KILL_ZONES = getattr(config, "KILL_ZONES", {}) if config else {}
if not KILL_ZONES:
    KILL_ZONES = {
        "asian_kz":  {"name": "Asian KZ",       "start": "00:00", "end": "03:00", "priority": "medium"},
        "london_kz": {"name": "London KZ",      "start": "07:00", "end": "10:00", "priority": "high"},
        "ny_am_kz":  {"name": "NY AM KZ",       "start": "12:00", "end": "15:00", "priority": "high"},
    }


# ---------------------------------------------------------------------------
# Canonical list of the 20 strategies (used to seed the scorer)
# ---------------------------------------------------------------------------
STRATEGY_NAMES = [
    "Order Block",
    "Fair Value Gap",
    "Liquidity Sweep",
    "Market Structure Shift",
    "Break of Structure",
    "Kill Zones",
    "Judas Swing",
    "Balanced Price Range",
    "Breaker Block",
    "Mitigation Block",
    "Silver Bullet",
    "Optimal Trade Entry",
    "Inversion FVG",
    "Volume Gap",
    "Inducement",
    "Power of 3 (AMD)",
    "Institutional Order Flow",
    "Premium/Discount Zones",
    "Quarterly Theory",
    "Daily Bias",
]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class Setup:
    id: str
    strategy: str
    direction: str          # "BUY" | "SELL"
    entry: float
    sl: float
    tp: float
    confidence: float
    timeframe: str
    style: str
    symbol: str
    detected_at: datetime
    bar_index: int
    detail: dict = field(default_factory=dict)

    def risk_reward(self) -> float:
        risk = abs(self.entry - self.sl)
        reward = abs(self.tp - self.entry)
        if risk == 0:
            return 0.0
        return round(reward / risk, 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["detected_at"] = self.detected_at.isoformat()
        d["risk_reward"] = self.risk_reward()
        return d


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class ICTEngine:
    """All 20 ICT detectors operating on a single OHLCV DataFrame."""

    def __init__(
        self,
        df: pd.DataFrame,
        symbol: str = "EUR/USD",
        timeframe: str = "1H",
        style: str = "Swing",
    ):
        self.df = self._prepare(df)
        self.symbol = symbol
        self.timeframe = timeframe
        self.style = style
        self.atr = self._atr(14)

    # ------------------------------------------------------------------ prep
    @staticmethod
    def _prepare(df: pd.DataFrame) -> pd.DataFrame:
        required = {"open", "high", "low", "close"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"Missing OHLC columns: {missing}")

        df = df.copy()

        if "timestamp" not in df.columns:
            if isinstance(df.index, pd.DatetimeIndex):
                df["timestamp"] = df.index
            else:
                raise ValueError(
                    "DataFrame must have a 'timestamp' column or a DatetimeIndex"
                )

        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True)

        if "volume" not in df.columns:
            df["volume"] = 0.0

        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
        return df

    def _atr(self, period: int = 14) -> pd.Series:
        high, low, close = self.df["high"], self.df["low"], self.df["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [
                (high - low).abs(),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return tr.rolling(period, min_periods=1).mean()

    # ---------------------------------------------------------------- helpers
    def _to_dt(self, obj) -> datetime:
        if isinstance(obj, datetime):
            return obj if obj.tzinfo else obj.replace(tzinfo=timezone.utc)
        if hasattr(obj, "to_pydatetime"):
            d = obj.to_pydatetime()
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        try:
            d = pd.Timestamp(obj).to_pydatetime()
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)

    def _mk_setup(
        self,
        strategy: str,
        direction: str,
        entry: float,
        sl: float,
        tp: float,
        bar_idx: int,
        confidence: float = 0.6,
        detail: Optional[dict] = None,
    ) -> Setup:
        return Setup(
            id=_uuid(),
            strategy=strategy,
            direction=direction,
            entry=float(entry),
            sl=float(sl),
            tp=float(tp),
            confidence=float(confidence),
            timeframe=self.timeframe,
            style=self.style,
            symbol=self.symbol,
            detected_at=self._to_dt(self.df["timestamp"].iloc[bar_idx]),
            bar_index=int(bar_idx),
            detail=detail or {},
        )

    def _swings(self, n: int = 2) -> tuple[list[int], list[int]]:
        """Return bar indices of swing highs and swing lows (fractal, n each side)."""
        highs_arr = self.df["high"].values
        lows_arr = self.df["low"].values
        highs: list[int] = []
        lows: list[int] = []
        size = len(self.df)
        for i in range(n, size - n):
            window_h = highs_arr[i - n : i + n + 1]
            window_l = lows_arr[i - n : i + n + 1]
            if highs_arr[i] == window_h.max():
                highs.append(i)
            if lows_arr[i] == window_l.min():
                lows.append(i)
        return highs, lows

    def _is_displacement(self, i: int, multiplier: float = 1.5) -> bool:
        body = abs(self.df["close"].iloc[i] - self.df["open"].iloc[i])
        atr = self.atr.iloc[i] if not np.isnan(self.atr.iloc[i]) else 0.0
        if atr <= 0:
            return False
        return body >= multiplier * atr

    def _atr_at(self, i: int) -> float:
        v = self.atr.iloc[i]
        return float(v) if not np.isnan(v) else 0.0

    # =====================================================================
    # 1. ORDER BLOCK
    # =====================================================================
    def detect_order_blocks(self, lookback: int = 200) -> list[Setup]:
        setups: list[Setup] = []
        df = self.df
        n = len(df)
        start = max(2, n - lookback)

        for i in range(start, n):
            if not self._is_displacement(i):
                continue
            direction = "BUY" if df["close"].iloc[i] > df["open"].iloc[i] else "SELL"

            ob_idx = None
            for j in range(i - 1, max(0, i - 10), -1):
                if direction == "BUY" and df["close"].iloc[j] < df["open"].iloc[j]:
                    ob_idx = j
                    break
                if direction == "SELL" and df["close"].iloc[j] > df["open"].iloc[j]:
                    ob_idx = j
                    break
            if ob_idx is None:
                continue

            atr = self._atr_at(ob_idx)
            if direction == "BUY":
                entry = float(df["close"].iloc[ob_idx])
                sl = float(df["low"].iloc[ob_idx]) - 0.1 * atr
                tp = float(df["high"].iloc[i])
                if tp <= entry:
                    tp = entry + 2 * (entry - sl)
            else:
                entry = float(df["close"].iloc[ob_idx])
                sl = float(df["high"].iloc[ob_idx]) + 0.1 * atr
                tp = float(df["low"].iloc[i])
                if tp >= entry:
                    tp = entry - 2 * (sl - entry)

            setups.append(
                self._mk_setup(
                    "Order Block",
                    direction,
                    entry,
                    sl,
                    tp,
                    ob_idx,
                    confidence=0.7,
                    detail={
                        "ob_bar": int(ob_idx),
                        "displacement_bar": int(i),
                        "ob_high": float(df["high"].iloc[ob_idx]),
                        "ob_low": float(df["low"].iloc[ob_idx]),
                    },
                )
            )
        return setups

    # =====================================================================
    # 2. FAIR VALUE GAP
    # =====================================================================
    def detect_fvg(self, lookback: int = 200) -> list[Setup]:
        setups: list[Setup] = []
        df = self.df
        n = len(df)
        start = max(2, n - lookback)

        for i in range(start, n):
            atr = self._atr_at(i)
            # Bullish FVG: low[i] > high[i-2]
            if df["low"].iloc[i] > df["high"].iloc[i - 2]:
                gap_top = float(df["low"].iloc[i])
                gap_bot = float(df["high"].iloc[i - 2])
                size = gap_top - gap_bot
                if size < 0.1 * max(atr, 1e-9):
                    continue
                entry = (gap_top + gap_bot) / 2
                sl = gap_bot - 0.1 * atr
                tp = entry + 2 * (entry - sl)
                setups.append(
                    self._mk_setup(
                        "Fair Value Gap",
                        "BUY",
                        entry,
                        sl,
                        tp,
                        i,
                        confidence=0.65,
                        detail={
                            "gap_top": gap_top,
                            "gap_bot": gap_bot,
                            "middle_bar": int(i - 1),
                        },
                    )
                )
            # Bearish FVG: high[i] < low[i-2]
            if df["high"].iloc[i] < df["low"].iloc[i - 2]:
                gap_top = float(df["low"].iloc[i - 2])
                gap_bot = float(df["high"].iloc[i])
                size = gap_top - gap_bot
                if size < 0.1 * max(atr, 1e-9):
                    continue
                entry = (gap_top + gap_bot) / 2
                sl = gap_top + 0.1 * atr
                tp = entry - 2 * (sl - entry)
                setups.append(
                    self._mk_setup(
                        "Fair Value Gap",
                        "SELL",
                        entry,
                        sl,
                        tp,
                        i,
                        confidence=0.65,
                        detail={
                            "gap_top": gap_top,
                            "gap_bot": gap_bot,
                            "middle_bar": int(i - 1),
                        },
                    )
                )
        return setups

    # =====================================================================
    # 3. LIQUIDITY SWEEP
    # =====================================================================
    def detect_liquidity_sweeps(
        self, lookback: int = 200, swing_n: int = 2
    ) -> list[Setup]:
        setups: list[Setup] = []
        df = self.df
        n = len(df)
        highs, lows = self._swings(swing_n)
        start = max(swing_n + 1, n - lookback)

        for i in range(start, n):
            atr = self._atr_at(i)

            for sh in reversed(highs):
                if sh >= i or (i - sh) > 60:
                    continue
                if (
                    df["high"].iloc[i] > df["high"].iloc[sh]
                    and df["close"].iloc[i] < df["high"].iloc[sh]
                ):
                    entry = float(df["close"].iloc[i])
                    sl = float(df["high"].iloc[i]) + 0.1 * atr
                    tp = entry - 2 * (sl - entry)
                    setups.append(
                        self._mk_setup(
                            "Liquidity Sweep",
                            "SELL",
                            entry,
                            sl,
                            tp,
                            i,
                            confidence=0.7,
                            detail={
                                "swept_level": float(df["high"].iloc[sh]),
                                "swept_bar": int(sh),
                            },
                        )
                    )
                    break

            for sl_i in reversed(lows):
                if sl_i >= i or (i - sl_i) > 60:
                    continue
                if (
                    df["low"].iloc[i] < df["low"].iloc[sl_i]
                    and df["close"].iloc[i] > df["low"].iloc[sl_i]
                ):
                    entry = float(df["close"].iloc[i])
                    sl = float(df["low"].iloc[i]) - 0.1 * atr
                    tp = entry + 2 * (entry - sl)
                    setups.append(
                        self._mk_setup(
                            "Liquidity Sweep",
                            "BUY",
                            entry,
                            sl,
                            tp,
                            i,
                            confidence=0.7,
                            detail={
                                "swept_level": float(df["low"].iloc[sl_i]),
                                "swept_bar": int(sl_i),
                            },
                        )
                    )
                    break
        return setups

    # =====================================================================
    # 4. BREAK OF STRUCTURE
    # =====================================================================
    def detect_bos(self, lookback: int = 200, swing_n: int = 2) -> list[Setup]:
        setups: list[Setup] = []
        df = self.df
        n = len(df)
        highs, lows = self._swings(swing_n)
        start = max(swing_n + 1, n - lookback)

        for i in range(start, n):
            atr = self._atr_at(i)

            for sh in reversed(highs):
                if sh >= i:
                    continue
                if (
                    df["close"].iloc[i] > df["high"].iloc[sh]
                    and df["close"].iloc[i - 1] <= df["high"].iloc[sh]
                ):
                    entry = float(df["close"].iloc[i])
                    sl = float(df["low"].iloc[i - 1]) - 0.1 * atr
                    tp = entry + 2 * (entry - sl)
                    setups.append(
                        self._mk_setup(
                            "Break of Structure",
                            "BUY",
                            entry,
                            sl,
                            tp,
                            i,
                            confidence=0.7,
                            detail={
                                "broken_level": float(df["high"].iloc[sh]),
                                "broken_bar": int(sh),
                            },
                        )
                    )
                    break

            for sl_i in reversed(lows):
                if sl_i >= i:
                    continue
                if (
                    df["close"].iloc[i] < df["low"].iloc[sl_i]
                    and df["close"].iloc[i - 1] >= df["low"].iloc[sl_i]
                ):
                    entry = float(df["close"].iloc[i])
                    sl = float(df["high"].iloc[i - 1]) + 0.1 * atr
                    tp = entry - 2 * (sl - entry)
                    setups.append(
                        self._mk_setup(
                            "Break of Structure",
                            "SELL",
                            entry,
                            sl,
                            tp,
                            i,
                            confidence=0.7,
                            detail={
                                "broken_level": float(df["low"].iloc[sl_i]),
                                "broken_bar": int(sl_i),
                            },
                        )
                    )
                    break
        return setups

    # =====================================================================
    # 5. MARKET STRUCTURE SHIFT
    # =====================================================================
    def detect_mss(self, lookback: int = 200, swing_n: int = 2) -> list[Setup]:
        bos_setups = self.detect_bos(lookback, swing_n)
        out: list[Setup] = []
        df = self.df

        for bos in bos_setups:
            bi = bos.bar_index
            if bi < 30:
                continue
            window = df.iloc[max(0, bi - 50) : bi]
            if len(window) < 5:
                continue
            trend_up = float(window["close"].iloc[-1]) > float(window["close"].iloc[0])
            reversal = (bos.direction == "SELL" and trend_up) or (
                bos.direction == "BUY" and not trend_up
            )
            if not reversal:
                continue
            out.append(
                Setup(
                    id=_uuid(),
                    strategy="Market Structure Shift",
                    direction=bos.direction,
                    entry=bos.entry,
                    sl=bos.sl,
                    tp=bos.tp,
                    confidence=0.75,
                    timeframe=self.timeframe,
                    style=self.style,
                    symbol=self.symbol,
                    detected_at=bos.detected_at,
                    bar_index=bi,
                    detail={"bos_reference": bos.id, **bos.detail},
                )
            )
        return out

    # =====================================================================
    # 6. KILL ZONES
    # =====================================================================
    def detect_kill_zones(self, lookback: int = 60) -> list[Setup]:
        setups: list[Setup] = []
        df = self.df
        n = len(df)
        start = max(1, n - lookback)

        for i in range(start, n):
            ts = self._to_dt(df["timestamp"].iloc[i])
            t = ts.time()
            for key, kz in KILL_ZONES.items():
                s = dt_time(*map(int, kz["start"].split(":")))
                e = dt_time(*map(int, kz["end"].split(":")))
                in_zone = (s <= t < e) if s <= e else (t >= s or t < e)
                if not in_zone:
                    continue

                direction = (
                    "BUY" if df["close"].iloc[i] > df["open"].iloc[i] else "SELL"
                )
                entry = float(df["close"].iloc[i])
                atr = self._atr_at(i)
                if direction == "BUY":
                    sl = float(df["low"].iloc[i]) - 0.2 * atr
                    tp = entry + 2 * (entry - sl)
                else:
                    sl = float(df["high"].iloc[i]) + 0.2 * atr
                    tp = entry - 2 * (sl - entry)

                setups.append(
                    self._mk_setup(
                        "Kill Zones",
                        direction,
                        entry,
                        sl,
                        tp,
                        i,
                        confidence=0.6,
                        detail={
                            "kill_zone": key,
                            "priority": kz.get("priority", "medium"),
                        },
                    )
                )
        return setups

    # =====================================================================
    # 7. JUDAS SWING
    # =====================================================================
    def detect_judas_swing(self, lookback: int = 120) -> list[Setup]:
        setups: list[Setup] = []
        df = self.df
        n = len(df)
        start = max(3, n - lookback)

        for i in range(start, n):
            ts = self._to_dt(df["timestamp"].iloc[i])
            t = ts.time()
            in_open = (dt_time(7, 0) <= t < dt_time(8, 0)) or (
                dt_time(12, 0) <= t < dt_time(13, 0)
            )
            if not in_open:
                continue

            atr = self._atr_at(i)
            prev_highs = float(df["high"].iloc[i - 3 : i].max())
            prev_lows = float(df["low"].iloc[i - 3 : i].min())

            # Sweep high then close down
            if (
                df["high"].iloc[i - 1] > prev_highs
                and df["close"].iloc[i] < df["open"].iloc[i]
            ):
                entry = float(df["close"].iloc[i])
                sl = float(df["high"].iloc[i - 1]) + 0.1 * atr
                tp = entry - 2 * (sl - entry)
                setups.append(
                    self._mk_setup(
                        "Judas Swing",
                        "SELL",
                        entry,
                        sl,
                        tp,
                        i,
                        confidence=0.7,
                        detail={"swept_bar": int(i - 1)},
                    )
                )

            # Sweep low then close up
            if (
                df["low"].iloc[i - 1] < prev_lows
                and df["close"].iloc[i] > df["open"].iloc[i]
            ):
                entry = float(df["close"].iloc[i])
                sl = float(df["low"].iloc[i - 1]) - 0.1 * atr
                tp = entry + 2 * (entry - sl)
                setups.append(
                    self._mk_setup(
                        "Judas Swing",
                        "BUY",
                        entry,
                        sl,
                        tp,
                        i,
                        confidence=0.7,
                        detail={"swept_bar": int(i - 1)},
                    )
                )
        return setups

    # =====================================================================
    # 8. BALANCED PRICE RANGE
    # =====================================================================
    def detect_bpr(self, lookback: int = 200) -> list[Setup]:
        fvgs = self.detect_fvg(lookback)
        bull = [s for s in fvgs if s.direction == "BUY"]
        bear = [s for s in fvgs if s.direction == "SELL"]
        setups: list[Setup] = []
        n = len(self.df)

        for b in bull:
            b_top = b.detail["gap_top"]
            b_bot = b.detail["gap_bot"]
            for s in bear:
                s_top = s.detail["gap_top"]
                s_bot = s.detail["gap_bot"]
                overlap_top = min(b_top, s_top)
                overlap_bot = max(b_bot, s_bot)
                if overlap_top <= overlap_bot:
                    continue

                entry = (overlap_top + overlap_bot) / 2
                atr = self._atr_at(n - 1)
                direction = "BUY" if b_top <= s_top else "SELL"
                if direction == "BUY":
                    sl = overlap_bot - 0.2 * atr
                    tp = entry + 2 * (entry - sl)
                else:
                    sl = overlap_top + 0.2 * atr
                    tp = entry - 2 * (sl - entry)

                bar_idx = max(b.bar_index, s.bar_index)
                setups.append(
                    self._mk_setup(
                        "Balanced Price Range",
                        direction,
                        entry,
                        sl,
                        tp,
                        bar_idx,
                        confidence=0.75,
                        detail={
                            "overlap_top": overlap_top,
                            "overlap_bot": overlap_bot,
                            "bull_fvg": b.id,
                            "bear_fvg": s.id,
                        },
                    )
                )
        return setups

    # =====================================================================
    # 9. BREAKER BLOCK
    # =====================================================================
    def detect_breaker_blocks(self, lookback: int = 200) -> list[Setup]:
        obs = self.detect_order_blocks(lookback)
        df = self.df
        n = len(df)
        setups: list[Setup] = []
        if n < 2:
            return setups

        atr = self._atr_at(n - 1)
        last_close = float(df["close"].iloc[-1])

        for ob in obs:
            ob_bar = ob.detail["ob_bar"]
            after = df.iloc[ob_bar + 1 :]
            if len(after) == 0:
                continue

            if ob.direction == "BUY":
                broken = (after["close"] < ob.detail["ob_low"]).any()
                if not broken:
                    continue
                if abs(last_close - ob.detail["ob_high"]) < 0.5 * max(atr, 1e-9):
                    entry = last_close
                    sl = ob.detail["ob_high"] + 0.2 * atr
                    tp = entry - 2 * (sl - entry)
                    setups.append(
                        self._mk_setup(
                            "Breaker Block",
                            "SELL",
                            entry,
                            sl,
                            tp,
                            n - 1,
                            confidence=0.7,
                            detail={"ob_bar": int(ob_bar)},
                        )
                    )
            else:
                broken = (after["close"] > ob.detail["ob_high"]).any()
                if not broken:
                    continue
                if abs(last_close - ob.detail["ob_low"]) < 0.5 * max(atr, 1e-9):
                    entry = last_close
                    sl = ob.detail["ob_low"] - 0.2 * atr
                    tp = entry + 2 * (entry - sl)
                    setups.append(
                        self._mk_setup(
                            "Breaker Block",
                            "BUY",
                            entry,
                            sl,
                            tp,
                            n - 1,
                            confidence=0.7,
                            detail={"ob_bar": int(ob_bar)},
                        )
                    )
        return setups

    # =====================================================================
    # 10. MITIGATION BLOCK
    # =====================================================================
    def detect_mitigation_blocks(self, lookback: int = 200) -> list[Setup]:
        obs = self.detect_order_blocks(lookback)
        df = self.df
        setups: list[Setup] = []

        for ob in obs:
            ob_bar = ob.detail["ob_bar"]
            after = df.iloc[ob_bar + 1 :]
            if len(after) == 0:
                continue

            if ob.direction == "BUY":
                touched = (after["low"] <= ob.detail["ob_high"]).any()
                violated = (after["close"] < ob.detail["ob_low"]).any()
                if touched and not violated:
                    setups.append(
                        self._mk_setup(
                            "Mitigation Block",
                            "BUY",
                            ob.entry,
                            ob.sl,
                            ob.tp,
                            ob_bar,
                            confidence=0.65,
                            detail={"ob_bar": int(ob_bar)},
                        )
                    )
            else:
                touched = (after["high"] >= ob.detail["ob_low"]).any()
                violated = (after["close"] > ob.detail["ob_high"]).any()
                if touched and not violated:
                    setups.append(
                        self._mk_setup(
                            "Mitigation Block",
                            "SELL",
                            ob.entry,
                            ob.sl,
                            ob.tp,
                            ob_bar,
                            confidence=0.65,
                            detail={"ob_bar": int(ob_bar)},
                        )
                    )
        return setups

    # =====================================================================
    # 11. SILVER BULLET
    # =====================================================================
    def detect_silver_bullet(self, lookback: int = 100) -> list[Setup]:
        """
        Silver Bullet window (ICT): 10:00–11:00 NY = 14:00–15:00 UTC (EDT)
        or 15:00–16:00 UTC (EST). We use 14:00–15:00 UTC as the default.
        Requires an active FVG inside the window.
        """
        setups: list[Setup] = []
        df = self.df
        n = len(df)
        start = max(2, n - lookback)

        for i in range(start, n):
            ts = self._to_dt(df["timestamp"].iloc[i])
            t = ts.time()
            if not (dt_time(14, 0) <= t < dt_time(15, 0)):
                continue
            # Local FVG within last 3 bars
            window = df.iloc[max(0, i - 2) : i + 1]
            if len(window) < 3:
                continue
            # Bullish FVG
            if window["low"].iloc[2] > window["high"].iloc[0]:
                gap_top = float(window["low"].iloc[2])
                gap_bot = float(window["high"].iloc[0])
                entry = (gap_top + gap_bot) / 2
                atr = self._atr_at(i)
                sl = gap_bot - 0.1 * atr
                tp = entry + 2 * (entry - sl)
                setups.append(
                    self._mk_setup(
                        "Silver Bullet",
                        "BUY",
                        entry,
                        sl,
                        tp,
                        i,
                        confidence=0.8,
                        detail={"window": "14:00-15:00 UTC", "fvg_top": gap_top,
                                "fvg_bot": gap_bot},
                    )
                )
            # Bearish FVG
            if window["high"].iloc[2] < window["low"].iloc[0]:
                gap_top = float(window["low"].iloc[0])
                gap_bot = float(window["high"].iloc[2])
                entry = (gap_top + gap_bot) / 2
                atr = self._atr_at(i)
                sl = gap_top + 0.1 * atr
                tp = entry - 2 * (sl - entry)
                setups.append(
                    self._mk_setup(
                        "Silver Bullet",
                        "SELL",
                        entry,
                        sl,
                        tp,
                        i,
                        confidence=0.8,
                        detail={"window": "14:00-15:00 UTC", "fvg_top": gap_top,
                                "fvg_bot": gap_bot},
                    )
                )
        return setups

    # =====================================================================
    # 12. OPTIMAL TRADE ENTRY (OTE)
    # =====================================================================
    def detect_ote(self, lookback: int = 100) -> list[Setup]:
        df = self.df
        n = len(df)
        if n < 20:
            return []

        window = df.iloc[-min(lookback, n):]
        high_idx = window["high"].idxmax()
        low_idx = window["low"].idxmin()
        swing_low = float(df.loc[low_idx, "low"])
        swing_high = float(df.loc[high_idx, "high"])
        leg = swing_high - swing_low
        if leg <= 0:
            return []

        last_close = float(df["close"].iloc[-1])
        atr = self._atr_at(n - 1)
        setups: list[Setup] = []

        if high_idx > low_idx:
            # Bullish leg — retracement into 62–79%
            ote_top = swing_high - 0.62 * leg
            ote_bot = swing_high - 0.79 * leg
            if ote_bot <= last_close <= ote_top:
                entry = last_close
                sl = swing_low - 0.1 * atr
                tp = swing_high
                setups.append(
                    self._mk_setup(
                        "Optimal Trade Entry",
                        "BUY",
                        entry,
                        sl,
                        tp,
                        n - 1,
                        confidence=0.75,
                        detail={
                            "swing_low": swing_low,
                            "swing_high": swing_high,
                            "ote_top": ote_top,
                            "ote_bot": ote_bot,
                        },
                    )
                )
        else:
            ote_bot = swing_low + 0.62 * leg
            ote_top = swing_low + 0.79 * leg
            if ote_bot <= last_close <= ote_top:
                entry = last_close
                sl = swing_high + 0.1 * atr
                tp = swing_low
                setups.append(
                    self._mk_setup(
                        "Optimal Trade Entry",
                        "SELL",
                        entry,
                        sl,
                        tp,
                        n - 1,
                        confidence=0.75,
                        detail={
                            "swing_low": swing_low,
                            "swing_high": swing_high,
                            "ote_top": ote_top,
                            "ote_bot": ote_bot,
                        },
                    )
                )
        return setups

    # =====================================================================
    # 13. INVERSION FVG
    # =====================================================================
    def detect_inversion_fvg(self, lookback: int = 200) -> list[Setup]:
        fvgs = self.detect_fvg(lookback)
        df = self.df
        n = len(df)
        if n < 2:
            return []
        setups: list[Setup] = []
        atr = self._atr_at(n - 1)
        last_close = float(df["close"].iloc[-1])

        for fvg in fvgs:
            gap_top = fvg.detail["gap_top"]
            gap_bot = fvg.detail["gap_bot"]
            after = df.iloc[fvg.bar_index + 1 :]
            if len(after) == 0:
                continue

            if fvg.direction == "BUY":
                violated = (after["close"] < gap_bot).any()
                if not violated:
                    continue
                if gap_bot <= last_close <= gap_top:
                    entry = last_close
                    sl = gap_top + 0.2 * atr
                    tp = entry - 2 * (sl - entry)
                    setups.append(
                        self._mk_setup(
                            "Inversion FVG",
                            "SELL",
                            entry,
                            sl,
                            tp,
                            n - 1,
                            confidence=0.7,
                            detail={"original_fvg": fvg.id},
                        )
                    )
            else:
                violated = (after["close"] > gap_top).any()
                if not violated:
                    continue
                if gap_bot <= last_close <= gap_top:
                    entry = last_close
                    sl = gap_bot - 0.2 * atr
                    tp = entry + 2 * (entry - sl)
                    setups.append(
                        self._mk_setup(
                            "Inversion FVG",
                            "BUY",
                            entry,
                            sl,
                            tp,
                            n - 1,
                            confidence=0.7,
                            detail={"original_fvg": fvg.id},
                        )
                    )
        return setups

    # =====================================================================
    # 14. VOLUME GAP
    # =====================================================================
    def detect_volume_gaps(self, lookback: int = 200) -> list[Setup]:
        if self.df["volume"].sum() == 0:
            return []
        fvgs = self.detect_fvg(lookback)
        vol = self.df["volume"]
        avg_vol = vol.rolling(20, min_periods=1).mean()
        setups: list[Setup] = []

        for fvg in fvgs:
            mid = fvg.detail.get("middle_bar")
            if mid is None or mid >= len(self.df):
                continue
            baseline = float(avg_vol.iloc[mid]) or 1e-9
            ratio = float(vol.iloc[mid]) / baseline
            if ratio < 0.7:
                setups.append(
                    Setup(
                        id=_uuid(),
                        strategy="Volume Gap",
                        direction=fvg.direction,
                        entry=fvg.entry,
                        sl=fvg.sl,
                        tp=fvg.tp,
                        confidence=0.7,
                        timeframe=self.timeframe,
                        style=self.style,
                        symbol=self.symbol,
                        detected_at=fvg.detected_at,
                        bar_index=fvg.bar_index,
                        detail={"fvg_id": fvg.id, "volume_ratio": ratio},
                    )
                )
        return setups

    # =====================================================================
    # 15. INDUCEMENT
    # =====================================================================
    def detect_inducement(self, lookback: int = 120, swing_n: int = 1) -> list[Setup]:
        setups: list[Setup] = []
        df = self.df
        n = len(df)
        highs, lows = self._swings(swing_n)
        start = max(swing_n + 1, n - lookback)

        for i in range(start, n):
            atr = self._atr_at(i)

            for sh in reversed(highs):
                if sh >= i or (i - sh) > 15:
                    continue
                if (
                    df["high"].iloc[i] > df["high"].iloc[sh]
                    and df["close"].iloc[i] < df["high"].iloc[sh]
                ):
                    entry = float(df["close"].iloc[i])
                    sl = float(df["high"].iloc[i]) + 0.1 * atr
                    tp = entry - 2 * (sl - entry)
                    setups.append(
                        self._mk_setup(
                            "Inducement",
                            "SELL",
                            entry,
                            sl,
                            tp,
                            i,
                            confidence=0.65,
                            detail={"inducement_bar": int(sh)},
                        )
                    )
                    break

            for sl_i in reversed(lows):
                if sl_i >= i or (i - sl_i) > 15:
                    continue
                if (
                    df["low"].iloc[i] < df["low"].iloc[sl_i]
                    and df["close"].iloc[i] > df["low"].iloc[sl_i]
                ):
                    entry = float(df["close"].iloc[i])
                    sl = float(df["low"].iloc[i]) - 0.1 * atr
                    tp = entry + 2 * (entry - sl)
                    setups.append(
                        self._mk_setup(
                            "Inducement",
                            "BUY",
                            entry,
                            sl,
                            tp,
                            i,
                            confidence=0.65,
                            detail={"inducement_bar": int(sl_i)},
                        )
                    )
                    break
        return setups

    # =====================================================================
    # 16. POWER OF 3 (AMD)
    # =====================================================================
    def detect_power_of_3(
        self, lookback: int = 150, range_bars: int = 15, manip_bars: int = 5
    ) -> list[Setup]:
        setups: list[Setup] = []
        df = self.df
        n = len(df)
        if n < range_bars + manip_bars + 3:
            return setups

        for i in range(max(range_bars + manip_bars, n - lookback), n):
            acc = df.iloc[i - manip_bars - range_bars : i - manip_bars]
            if len(acc) < range_bars:
                continue
            atr = self._atr_at(i)
            if atr <= 0:
                continue
            rng = float(acc["high"].max() - acc["low"].min())
            if rng > 3 * atr:
                continue

            manip = df.iloc[i - manip_bars : i]
            range_high = float(acc["high"].max())
            range_low = float(acc["low"].min())
            swept_high = bool((manip["high"] > range_high).any())
            swept_low = bool((manip["low"] < range_low).any())

            if swept_high and not swept_low:
                entry = float(df["close"].iloc[i])
                sl = float(manip["high"].max()) + 0.1 * atr
                tp = entry - 2 * (sl - entry)
                setups.append(
                    self._mk_setup(
                        "Power of 3 (AMD)",
                        "SELL",
                        entry,
                        sl,
                        tp,
                        i,
                        confidence=0.75,
                        detail={
                            "range_high": range_high,
                            "range_low": range_low,
                            "phase": "distribution",
                        },
                    )
                )
            elif swept_low and not swept_high:
                entry = float(df["close"].iloc[i])
                sl = float(manip["low"].min()) - 0.1 * atr
                tp = entry + 2 * (entry - sl)
                setups.append(
                    self._mk_setup(
                        "Power of 3 (AMD)",
                        "BUY",
                        entry,
                        sl,
                        tp,
                        i,
                        confidence=0.75,
                        detail={
                            "range_high": range_high,
                            "range_low": range_low,
                            "phase": "distribution",
                        },
                    )
                )
        return setups

    # =====================================================================
    # 17. INSTITUTIONAL ORDER FLOW
    # =====================================================================
    def detect_iof(self, lookback: int = 120) -> list[Setup]:
        setups: list[Setup] = []
        df = self.df
        n = len(df)
        start = max(3, n - lookback)

        for i in range(start, n):
            bodies = [
                float(df["close"].iloc[j] - df["open"].iloc[j])
                for j in range(i - 2, i + 1)
            ]
            atr = self._atr_at(i)

            if all(b > 0 for b in bodies) and bodies[-1] > bodies[0]:
                entry = float(df["close"].iloc[i])
                sl = float(df["low"].iloc[i - 2]) - 0.1 * atr
                tp = entry + 2 * (entry - sl)
                setups.append(
                    self._mk_setup(
                        "Institutional Order Flow",
                        "BUY",
                        entry,
                        sl,
                        tp,
                        i,
                        confidence=0.7,
                        detail={"bodies": bodies},
                    )
                )
            elif all(b < 0 for b in bodies) and bodies[-1] < bodies[0]:
                entry = float(df["close"].iloc[i])
                sl = float(df["high"].iloc[i - 2]) + 0.1 * atr
                tp = entry - 2 * (sl - entry)
                setups.append(
                    self._mk_setup(
                        "Institutional Order Flow",
                        "SELL",
                        entry,
                        sl,
                        tp,
                        i,
                        confidence=0.7,
                        detail={"bodies": bodies},
                    )
                )
        return setups

    # =====================================================================
    # 18. PREMIUM / DISCOUNT ZONES
    # =====================================================================
    def detect_premium_discount(self, lookback: int = 100) -> list[Setup]:
        df = self.df
        n = len(df)
        if n < lookback:
            return []

        window = df.iloc[-lookback:]
        high = float(window["high"].max())
        low = float(window["low"].min())
        mid = (high + low) / 2
        last_close = float(df["close"].iloc[-1])
        atr = self._atr_at(n - 1)

        if last_close < mid:
            entry = last_close
            sl = low - 0.1 * atr
            tp = mid + (mid - low) * 0.5
            zone = "discount"
            direction = "BUY"
        else:
            entry = last_close
            sl = high + 0.1 * atr
            tp = mid - (high - mid) * 0.5
            zone = "premium"
            direction = "SELL"

        return [
            self._mk_setup(
                "Premium/Discount Zones",
                direction,
                entry,
                sl,
                tp,
                n - 1,
                confidence=0.6,
                detail={"range_high": high, "range_low": low, "mid": mid,
                        "zone": zone},
            )
        ]

    # =====================================================================
    # 19. QUARTERLY THEORY
    # =====================================================================
    def detect_quarterly_theory(self, lookback: int = 60) -> list[Setup]:
        """
        NY AM session 13:00–16:00 UTC is split into 4 quarters (45 min each):
            Q1 = 13:00–13:45 (accumulation)
            Q2 = 13:45–14:30 (manipulation)
            Q3 = 14:30–15:15 (distribution)
            Q4 = 15:15–16:00 (reversal)
        """
        setups: list[Setup] = []
        df = self.df
        n = len(df)
        start = max(1, n - lookback)

        for i in range(start, n):
            ts = self._to_dt(df["timestamp"].iloc[i])
            minute_of_day = ts.hour * 60 + ts.minute
            session_start = 13 * 60
            session_end = 16 * 60
            if not (session_start <= minute_of_day < session_end):
                continue
            quarter = (minute_of_day - session_start) // 45 + 1

            direction = (
                "BUY" if df["close"].iloc[i] > df["open"].iloc[i] else "SELL"
            )
            entry = float(df["close"].iloc[i])
            atr = self._atr_at(i)
            if direction == "BUY":
                sl = float(df["low"].iloc[i]) - 0.2 * atr
                tp = entry + 2 * (entry - sl)
            else:
                sl = float(df["high"].iloc[i]) + 0.2 * atr
                tp = entry - 2 * (sl - entry)

            setups.append(
                self._mk_setup(
                    "Quarterly Theory",
                    direction,
                    entry,
                    sl,
                    tp,
                    i,
                    confidence=0.55 + 0.05 * quarter,
                    detail={"quarter": int(quarter), "session": "NY_AM"},
                )
            )
        return setups

    # =====================================================================
    # 20. DAILY BIAS
    # =====================================================================
    def detect_daily_bias(self) -> list[Setup]:
        df = self.df
        n = len(df)
        if n < 2:
            return []

        window = df["close"].iloc[-min(20, n):]
        mean = float(window.mean())
        last_close = float(df["close"].iloc[-1])
        atr = self._atr_at(n - 1)

        if last_close > mean:
            direction, bias = "BUY", "bullish"
            sl = last_close - 2 * atr
            tp = last_close + 4 * atr
        else:
            direction, bias = "SELL", "bearish"
            sl = last_close + 2 * atr
            tp = last_close - 4 * atr

        return [
            self._mk_setup(
                "Daily Bias",
                direction,
                last_close,
                sl,
                tp,
                n - 1,
                confidence=0.55,
                detail={"bias": bias, "mean_20": mean},
            )
        ]

    # =====================================================================
    # RUN ALL
    # =====================================================================
    def run_all(self) -> list[Setup]:
        if len(self.df) < 5:
            return []

        all_setups: list[Setup] = []
        all_setups.extend(self.detect_order_blocks())
        all_setups.extend(self.detect_fvg())
        all_setups.extend(self.detect_liquidity_sweeps())
        all_setups.extend(self.detect_bos())
        all_setups.extend(self.detect_mss())
        all_setups.extend(self.detect_kill_zones())
        all_setups.extend(self.detect_judas_swing())
        all_setups.extend(self.detect_bpr())
        all_setups.extend(self.detect_breaker_blocks())
        all_setups.extend(self.detect_mitigation_blocks())
        all_setups.extend(self.detect_silver_bullet())
        all_setups.extend(self.detect_ote())
        all_setups.extend(self.detect_inversion_fvg())
        all_setups.extend(self.detect_volume_gaps())
        all_setups.extend(self.detect_inducement())
        all_setups.extend(self.detect_power_of_3())
        all_setups.extend(self.detect_iof())
        all_setups.extend(self.detect_premium_discount())
        all_setups.extend(self.detect_quarterly_theory())
        all_setups.extend(self.detect_daily_bias())
        return all_setups


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------
def analyze(
    df: pd.DataFrame,
    symbol: str = "EUR/USD",
    timeframe: str = "1H",
    style: str = "Swing",
) -> list[Setup]:
    """Detect all setups without touching the scorer."""
    return ICTEngine(df, symbol, timeframe, style).run_all()


def analyze_and_track(
    df: pd.DataFrame,
    symbol: str = "EUR/USD",
    timeframe: str = "1H",
    style: str = "Swing",
    record_usage: bool = True,
) -> dict:
    """
    Detect all setups AND record usage in analysis.scorer.
    Returns {"setups": [dict, ...], "scorer": {...snapshot...}}.

    Note: callers should invoke this once per signal cycle. Repeated calls on
    identical data will increment usage (each Setup gets a fresh id).
    """
    setups = analyze(df, symbol, timeframe, style)
    out = {"setups": [s.to_dict() for s in setups]}

    if record_usage:
        try:
            from analysis.scorer import get_scorer

            scorer = get_scorer()
            for s in setups:
                scorer.record_usage(s)
            out["scorer"] = scorer.snapshot()
        except Exception as exc:  # pragma: no cover
            out["scorer_error"] = str(exc)

    return out


# ---------------------------------------------------------------------------
# Self-test (run:  python -m analysis.ict_engine)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    # Synthesize 400 bars of plausible OHLC data
    rng = np.random.default_rng(42)
    base_price = 1.1000
    ts = pd.date_range(
        end=datetime.now(timezone.utc), periods=400, freq="1H"
    )
    closes = base_price + np.cumsum(rng.normal(0, 0.0006, 400))
    opens = closes + rng.normal(0, 0.0003, 400)
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0, 0.0004, 400))
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0, 0.0004, 400))
    volumes = rng.integers(500, 5000, 400)

    demo_df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )

    engine = ICTEngine(demo_df, symbol="EUR/USD", timeframe="1H", style="Swing")
    setups = engine.run_all()

    print(f"Total setups detected: {len(setups)}")
    by_strategy: dict[str, int] = {}
    for s in setups:
        by_strategy[s.strategy] = by_strategy.get(s.strategy, 0) + 1

    print("\nPer-strategy counts:")
    for name in STRATEGY_NAMES:
        count = by_strategy.get(name, 0)
        bar = "█" * min(count, 40)
        print(f"  {name:<28} {count:>3}  {bar}")

    if setups:
        print("\nSample setup:")
        print(json.dumps(setups[0].to_dict(), indent=2, default=str))
