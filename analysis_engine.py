# analysis_engine.py
"""
ICT/SMC Analysis Engine — 20 strategies on real OHLCV data.
NO random or synthetic data used. Uses yfinance for real candles,
ForexFactory JSON + ForexLive RSS for real news.
Live prices: TwelveData (15s cache).

Developed & Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time as _time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf

logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.addHandler(logging.NullHandler())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# =============================================================================
# SYMBOL MAPPING (yfinance)
# =============================================================================
YF_MAP = {
    "XAU/USD": "GC=F",
    "XAG/USD": "SI=F",
    "WTI/USD": "CL=F",
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
}

# Pip / value specifications
SYMBOL_SPECS = {
    "XAU/USD": {"pip_size": 0.10, "pip_value_per_lot": 1.0, "min_lot": 0.01, "lot_step": 0.01, "digits": 2},
    "XAG/USD": {"pip_size": 0.001, "pip_value_per_lot": 5.0, "min_lot": 0.01, "lot_step": 0.01, "digits": 3},
    "WTI/USD": {"pip_size": 0.01, "pip_value_per_lot": 1.0, "min_lot": 0.01, "lot_step": 0.01, "digits": 2},
    "EUR/USD": {"pip_size": 0.0001, "pip_value_per_lot": 10.0, "min_lot": 0.01, "lot_step": 0.01, "digits": 5},
    "GBP/USD": {"pip_size": 0.0001, "pip_value_per_lot": 10.0, "min_lot": 0.01, "lot_step": 0.01, "digits": 5},
    "USD/JPY": {"pip_size": 0.01, "pip_value_per_lot": 6.7, "min_lot": 0.01, "lot_step": 0.01, "digits": 3},
}

TIMEFRAME_MAP = {
    "1M":  ("1m",  "2d"),    # 👈 جديد
    "5M":  ("5m",  "5d"),
    "15M": ("15m", "5d"),
    "1H":  ("60m", "1mo"),
    "4H":  ("1h",  "3mo"),   # yfinance max intraday for 1h is ~2y
    "1D":  ("1d",  "2y"),
    "1W":  ("1wk", "5y"),
}

STRATEGY_NAMES = [
    "Order Blocks", "Fair Value Gap", "Liquidity Sweep", "Market Structure Shift",
    "Break of Structure", "Kill Zones", "Judas Swing", "Balanced Price Range",
    "Breaker Block", "Mitigation Block", "Silver Bullet", "Optimal Trade Entry",
    "Inversion FVG", "Volume Gap", "Inducement", "Power of 3 (AMD)",
    "Institutional Order Flow", "Premium/Discount", "Quarterly Theory", "Daily Bias",
]


# =============================================================================
# DATA FETCHERS
# =============================================================================
class CandleFetcher:
    """Fetches real OHLCV data from yfinance with retry + cache."""

    def __init__(self, cache_ttl_sec: int = 900):    # 👈 زيادة التخزين المؤقت إلى 15 دقيقة
        self._cache: Dict[str, tuple] = {}
        self._ttl = cache_ttl_sec
        self._fail_cache: Dict[str, float] = {}

    def get(self, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        key = f"{symbol}:{timeframe}"
        now = _time.time()

        if key in self._fail_cache:
            if now - self._fail_cache[key] < 900:
                return None

        if key in self._cache:
            ts, df = self._cache[key]
            if now - ts < self._ttl:
                return df

        yf_symbol = YF_MAP.get(symbol)
        if not yf_symbol:
            return None
        interval, period = TIMEFRAME_MAP.get(timeframe, ("1h", "1mo"))

        for attempt in range(2):
            try:
                # 👈 إضافة auto_adjust=False لإزالة التحذير
                df = yf.download(yf_symbol, period=period, interval=interval, progress=False, auto_adjust=False)
                if df is not None and not df.empty:
                    # 👈 السطر السحري: إذا كانت الأعمدة متعددة المستويات، اجعلها مستوى واحد
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)

                    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
                    df = df[["open", "high", "low", "close", "volume"]].copy()
                    df.index = pd.to_datetime(df.index, utc=True)
                    df = df[~df.index.duplicated(keep="last")].sort_index()

                    self._cache[key] = (now, df)
                    self._fail_cache.pop(key, None)
                    return df
            except Exception as e:
                logger.warning("yfinance attempt %d %s@%s: %s", attempt + 1, symbol, timeframe, e)
                _time.sleep(2.0)

        self._fail_cache[key] = now
        return None


class NewsFetcher:
    """
    Fetches real economic calendar events.
    Priority: ForexFactory → ForexLive RSS (fallback)
    """
    FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    RSS_URL = "https://www.forexlive.com/feed/news"

    def __init__(self, cache_ttl_sec: int = 1800):  # 30 دقيقة cache
        self._cache: List[Dict[str, Any]] = []
        self._last_fetch: float = 0.0
        self._last_fail: float = 0.0
        self._ttl = cache_ttl_sec
        self._fail_cooldown = 300  # 👈 5 دقائق فقط (بدل الساعة)
        self._lock = threading.Lock()

    def fetch(self) -> List[Dict[str, Any]]:
        with self._lock:
            now = _time.time()

            # إذا فشل مؤخراً (خلال 5 دقائق)، جرّب RSS بدلاً منه
            if now - self._last_fail < self._fail_cooldown:
                return self._fetch_rss()

            if now - self._last_fetch < self._ttl and self._cache:
                return list(self._cache)

            # 1) محاولة ForexFactory
            try:
                r = requests.get(self.FF_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                raw = r.json()
                out = self._parse_ff(raw)
                if out:
                    self._cache = out
                    self._last_fetch = now
                    return out
            except Exception as e:
                logger.warning("ForexFactory failed (429/network): %s — switching to RSS", e)
                self._last_fail = now

            # 2) Fallback: RSS
            return self._fetch_rss()

    def _parse_ff(self, raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Parse ForexFactory JSON."""
        out: List[Dict[str, Any]] = []
        for item in raw or []:
            try:
                dt = pd.to_datetime(item.get("date"), utc=True).to_pydatetime()
            except Exception:
                continue
            impact = str(item.get("impact", "Medium")).lower()
            if impact not in ("high", "medium", "low"):
                impact = "medium"
            out.append({
                "source": "forexfactory",
                "title": item.get("title", "—"),
                "currency": item.get("country", ""),
                "impact": impact,
                "event_time": dt.isoformat(),
                "forecast": item.get("forecast", ""),
                "previous": item.get("previous", ""),
                "actual": item.get("actual", ""),
            })
        return out

    def _fetch_rss(self) -> List[Dict[str, Any]]:
        """Fallback: ForexLive RSS — works always, no API key."""
        cached = self._cache if self._cache else []
        try:
            import feedparser
            feed = feedparser.parse(self.RSS_URL)
            out: List[Dict[str, Any]] = []
            for entry in feed.entries[:15]:
                try:
                    dt = pd.to_datetime(entry.published, utc=True).to_pydatetime()
                except Exception:
                    dt = utcnow()

                title_lower = entry.title.lower()
                is_geo = any(w in title_lower for w in
                             ["war", "sanction", "conflict", "military", "attack", "iran", "israel", "russia"])
                is_high = any(w in title_lower for w in
                              ["fed", "cpi", "nfp", "rate", "inflation", "employment", "gdp"])

                impact = "high" if (is_geo or is_high) else "medium"

                out.append({
                    "source": "forexlive",
                    "title": entry.title,
                    "currency": "USD",
                    "impact": impact,
                    "event_time": dt.isoformat(),
                    "forecast": "",
                    "previous": "",
                    "actual": "",
                    "url": entry.link,
                })
            return out if out else cached
        except Exception as e:
            logger.debug("RSS fallback failed: %s", e)
            return cached

    def high_impact_within(self, minutes_before: int = 5,
                           minutes_after: int = 10,
                           now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """Return nearest high-impact event in blackout window."""
        now = now or utcnow()
        events = self.fetch()
        for e in events:
            if e["impact"] != "high":
                continue
            try:
                ev_dt = datetime.fromisoformat(e["event_time"])
                if ev_dt.tzinfo is None:
                    ev_dt = ev_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            start = ev_dt - timedelta(minutes=minutes_before)
            end = ev_dt + timedelta(minutes=minutes_after)
            if start <= now <= end:
                return e
        return None


# =============================================================================
# HELPERS
# =============================================================================
def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([
        (h - l).abs(), (h - prev_c).abs(), (l - prev_c).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def _swings(df: pd.DataFrame, n: int = 2):
    highs, lows = [], []
    h = df["high"].values
    l = df["low"].values
    for i in range(n, len(df) - n):
        if h[i] == h[i - n:i + n + 1].max():
            highs.append(i)
        if l[i] == l[i - n:i + n + 1].min():
            lows.append(i)
    return highs, lows


def _signal_uid(symbol: str, strategy: str, direction: str,
                tf: str, entry: float) -> str:
    raw = f"{symbol}|{strategy}|{direction}|{tf}|{entry:.5f}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# =============================================================================
# ICT DETECTORS
# =============================================================================
class ICTDetectors:
    """20 ICT strategy detectors on real OHLCV data."""

    def __init__(self, df: pd.DataFrame, atr_series: pd.Series,
                 symbol: str, timeframe: str, style: str,
                 filters: Optional[Dict[str, float]] = None):
        # 👈 تأمين: تسطيح MultiIndex إن وُجد قبل أي معالجة
        if isinstance(df.columns, pd.MultiIndex):
            df = df.copy()
            df.columns = df.columns.get_level_values(0)
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})

        self.df = df
        self.atr = atr_series
        self.symbol = symbol
        self.timeframe = timeframe
        self.style = style
        self.filters = filters or {}

    # ------------------------------------------------------------------
    @staticmethod
    def _prepare(df: pd.DataFrame) -> pd.DataFrame:
        required = {"open", "high", "low", "close"}
        missing = required - set(df.columns)

        # 👈 إضافة تأمينية: إذا كانت الأعمدة متعددة المستويات قم بتسطيحها
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
            missing = required - set(df.columns)

        if missing:
            raise ValueError(f"Missing OHLC columns: {missing}")

        df = df.copy()

        if "timestamp" not in df.columns:
            if isinstance(df.index, pd.DatetimeIndex):
                df["timestamp"] = df.index
            else:
                raise ValueError("DataFrame must have a 'timestamp' column or a DatetimeIndex")

        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True)

        if "volume" not in df.columns:
            df["volume"] = 0.0

        for col in ("open", "high", "low", "close", "volume"):
            # 👈 استخدام .astype(float) لضمان تحويل الأرقام بدقة بدون تحذيرات
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)

        df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
        return df

    # ------------------------------------------------------------------
    def _setup(self, strategy: str, direction: str, entry: float,
               sl: float, tp: float, bar_idx: int, confidence: float,
               reason: str = "", detail: Optional[dict] = None) -> Dict[str, Any]:
        w = float(self.filters.get(strategy, 1.0))
        rr = abs(tp - entry) / max(abs(entry - sl), 1e-9)
        confidence_weighted = round(min(confidence * w, 1.0), 3)
        return {
            "signal_uid": _signal_uid(self.symbol, strategy, direction,
                                      self.timeframe, entry),
            "symbol": self.symbol,
            "strategy": strategy,
            "direction": direction,
            "style": self.style,
            "timeframe": self.timeframe,
            "entry": round(float(entry), 5),
            "stop_loss": round(float(sl), 5),
            "take_profit": round(float(tp), 5),
            "confidence": confidence_weighted,
            "risk_reward": round(rr, 2),
            "reason": reason or strategy,
            "bar_index": int(bar_idx),
            "meta": detail or {},
        }

    def _atr_at(self, i: int) -> float:
        v = self.atr.iloc[i]
        return float(v) if not pd.isna(v) else 0.0

    # 1. Order Blocks ----------------------------------------------------
    def order_blocks(self, lookback: int = 60) -> List[Dict[str, Any]]:
        out = []
        df = self.df
        n = len(df)
        start = max(2, n - lookback)
        for i in range(start, n):
            body = abs(df["close"].iloc[i] - df["open"].iloc[i])
            atr = self._atr_at(i)
            if atr <= 0 or body < 1.2 * atr:
                continue
            direction = "BUY" if df["close"].iloc[i] > df["open"].iloc[i] else "SELL"
            ob = None
            for j in range(i - 1, max(0, i - 8), -1):
                if direction == "BUY" and df["close"].iloc[j] < df["open"].iloc[j]:
                    ob = j
                    break
                if direction == "SELL" and df["close"].iloc[j] > df["open"].iloc[j]:
                    ob = j
                    break
            if ob is None:
                continue
            entry = float(df["close"].iloc[ob])
            if direction == "BUY":
                sl = float(df["low"].iloc[ob]) - 0.1 * atr
                tp = entry + 2.0 * (entry - sl)
            else:
                sl = float(df["high"].iloc[ob]) + 0.1 * atr
                tp = entry - 2.0 * (sl - entry)
            out.append(self._setup("Order Blocks", direction, entry, sl, tp, ob,
                                   confidence=0.72, reason="OB displacement entry",
                                   detail={"ob_bar": ob, "disp_bar": i}))
        return out

    # 2. Fair Value Gap --------------------------------------------------
    def fair_value_gap(self, lookback: int = 60) -> List[Dict[str, Any]]:
        out = []
        df = self.df
        n = len(df)
        start = max(2, n - lookback)
        for i in range(start, n):
            atr = self._atr_at(i)
            if atr <= 0:
                continue
            # Bullish FVG
            if df["low"].iloc[i] > df["high"].iloc[i - 2]:
                gap_top = float(df["low"].iloc[i])
                gap_bot = float(df["high"].iloc[i - 2])
                if gap_top - gap_bot < 0.1 * atr:
                    continue
                entry = (gap_top + gap_bot) / 2
                sl = gap_bot - 0.1 * atr
                tp = entry + 2 * (entry - sl)
                out.append(self._setup("Fair Value Gap", "BUY", entry, sl, tp, i,
                                       confidence=0.66, reason="Bullish FVG retest",
                                       detail={"gap_top": gap_top, "gap_bot": gap_bot}))
            # Bearish FVG
            if df["high"].iloc[i] < df["low"].iloc[i - 2]:
                gap_top = float(df["low"].iloc[i - 2])
                gap_bot = float(df["high"].iloc[i])
                if gap_top - gap_bot < 0.1 * atr:
                    continue
                entry = (gap_top + gap_bot) / 2
                sl = gap_top + 0.1 * atr
                tp = entry - 2 * (sl - entry)
                out.append(self._setup("Fair Value Gap", "SELL", entry, sl, tp, i,
                                       confidence=0.66, reason="Bearish FVG retest",
                                       detail={"gap_top": gap_top, "gap_bot": gap_bot}))
        return out

    # 3. Liquidity Sweep -------------------------------------------------
    def liquidity_sweep(self, lookback: int = 50) -> List[Dict[str, Any]]:
        out = []
        df = self.df
        n = len(df)
        highs, lows = _swings(df, 2)
        start = max(3, n - lookback)
        for i in range(start, n):
            atr = self._atr_at(i)
            if atr <= 0:
                continue
            for sh in reversed(highs):
                if sh >= i or (i - sh) > 40:
                    continue
                if (df["high"].iloc[i] > df["high"].iloc[sh]
                        and df["close"].iloc[i] < df["high"].iloc[sh]):
                    entry = float(df["close"].iloc[i])
                    sl = float(df["high"].iloc[i]) + 0.1 * atr
                    tp = entry - 2 * (sl - entry)
                    out.append(self._setup("Liquidity Sweep", "SELL", entry, sl, tp, i,
                                           confidence=0.75, reason="BSL swept",
                                           detail={"swept": float(df["high"].iloc[sh])}))
                    break
            for sl_i in reversed(lows):
                if sl_i >= i or (i - sl_i) > 40:
                    continue
                if (df["low"].iloc[i] < df["low"].iloc[sl_i]
                        and df["close"].iloc[i] > df["low"].iloc[sl_i]):
                    entry = float(df["close"].iloc[i])
                    sl = float(df["low"].iloc[i]) - 0.1 * atr
                    tp = entry + 2 * (entry - sl)
                    out.append(self._setup("Liquidity Sweep", "BUY", entry, sl, tp, i,
                                           confidence=0.75, reason="SSL swept",
                                           detail={"swept": float(df["low"].iloc[sl_i])}))
                    break
        return out

    # 4. Break of Structure ----------------------------------------------
    def break_of_structure(self, lookback: int = 50) -> List[Dict[str, Any]]:
        out = []
        df = self.df
        n = len(df)
        highs, lows = _swings(df, 2)
        start = max(3, n - lookback)
        for i in range(start, n):
            atr = self._atr_at(i)
            if atr <= 0:
                continue
            for sh in reversed(highs):
                if sh >= i:
                    continue
                if (df["close"].iloc[i] > df["high"].iloc[sh]
                        and df["close"].iloc[i - 1] <= df["high"].iloc[sh]):
                    entry = float(df["close"].iloc[i])
                    sl = float(df["low"].iloc[i - 1]) - 0.1 * atr
                    tp = entry + 2 * (entry - sl)
                    out.append(self._setup("Break of Structure", "BUY",
                                           entry, sl, tp, i, confidence=0.68,
                                           reason="BOS bullish"))
                    break
            for sl_i in reversed(lows):
                if sl_i >= i:
                    continue
                if (df["close"].iloc[i] < df["low"].iloc[sl_i]
                        and df["close"].iloc[i - 1] >= df["low"].iloc[sl_i]):
                    entry = float(df["close"].iloc[i])
                    sl = float(df["high"].iloc[i - 1]) + 0.1 * atr
                    tp = entry - 2 * (sl - entry)
                    out.append(self._setup("Break of Structure", "SELL",
                                           entry, sl, tp, i, confidence=0.68,
                                           reason="BOS bearish"))
                    break
        return out

    # 5. Market Structure Shift ------------------------------------------
    def market_structure_shift(self, lookback: int = 50) -> List[Dict[str, Any]]:
        bos = self.break_of_structure(lookback)
        out = []
        df = self.df
        for b in bos:
            bi = b["bar_index"]
            if bi < 30:
                continue
            w = df.iloc[max(0, bi - 30): bi]
            if len(w) < 5:
                continue
            trend_up = float(w["close"].iloc[-1]) > float(w["close"].iloc[0])
            reversal = (b["direction"] == "SELL" and trend_up) or (
                b["direction"] == "BUY" and not trend_up)
            if not reversal:
                continue
            out.append(self._setup("Market Structure Shift", b["direction"],
                                   b["entry"], b["stop_loss"], b["take_profit"], bi,
                                   confidence=0.78, reason="MSS confirmed"))
        return out

    # 6. Kill Zones ------------------------------------------------------
    def kill_zones(self, lookback: int = 20) -> List[Dict[str, Any]]:
        from session_guard import SessionGuard, KILL_ZONES as KZ
        out = []
        sg = SessionGuard()
        df = self.df
        n = len(df)
        start = max(1, n - lookback)
        for i in range(start, n):
            ts = df.index[i]
            if not isinstance(ts, pd.Timestamp):
                continue
            dt = ts.to_pydatetime()
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            active = sg._active_kill_zones(dt)
            if not active:
                continue
            direction = "BUY" if df["close"].iloc[i] > df["open"].iloc[i] else "SELL"
            entry = float(df["close"].iloc[i])
            atr = self._atr_at(i)
            if direction == "BUY":
                sl = float(df["low"].iloc[i]) - 0.2 * atr
                tp = entry + 2 * (entry - sl)
            else:
                sl = float(df["high"].iloc[i]) + 0.2 * atr
                tp = entry - 2 * (sl - entry)
            out.append(self._setup("Kill Zones", direction, entry, sl, tp, i,
                                   confidence=0.62, reason=f"Inside {active[0]['name']}"))
        return out

    # 7. Judas Swing -----------------------------------------------------
    def judas_swing(self, lookback: int = 30) -> List[Dict[str, Any]]:
        out = []
        df = self.df
        n = len(df)
        start = max(3, n - lookback)
        for i in range(start, n):
            ts = df.index[i]
            if not isinstance(ts, pd.Timestamp):
                continue
            dt = ts.to_pydatetime()
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            hh = dt.hour
            if not ((7 <= hh < 9) or (12 <= hh < 14)):
                continue
            atr = self._atr_at(i)
            prev_highs = float(df["high"].iloc[i - 3:i].max())
            prev_lows = float(df["low"].iloc[i - 3:i].min())
            if df["high"].iloc[i - 1] > prev_highs and df["close"].iloc[i] < df["open"].iloc[i]:
                entry = float(df["close"].iloc[i])
                sl = float(df["high"].iloc[i - 1]) + 0.1 * atr
                tp = entry - 2 * (sl - entry)
                out.append(self._setup("Judas Swing", "SELL", entry, sl, tp, i,
                                       confidence=0.72, reason="Judas high sweep"))
            if df["low"].iloc[i - 1] < prev_lows and df["close"].iloc[i] > df["open"].iloc[i]:
                entry = float(df["close"].iloc[i])
                sl = float(df["low"].iloc[i - 1]) - 0.1 * atr
                tp = entry + 2 * (entry - sl)
                out.append(self._setup("Judas Swing", "BUY", entry, sl, tp, i,
                                       confidence=0.72, reason="Judas low sweep"))
        return out

    # 8. Balanced Price Range --------------------------------------------
    def balanced_price_range(self, lookback: int = 50) -> List[Dict[str, Any]]:
        fvgs = self.fair_value_gap(lookback)
        bull = [f for f in fvgs if f["direction"] == "BUY"]
        bear = [f for f in fvgs if f["direction"] == "SELL"]
        out = []
        for b in bull:
            for s in bear:
                b_top, b_bot = b["meta"]["gap_top"], b["meta"]["gap_bot"]
                s_top, s_bot = s["meta"]["gap_top"], s["meta"]["gap_bot"]
                top = min(b_top, s_top)
                bot = max(b_bot, s_bot)
                if top <= bot:
                    continue
                entry = (top + bot) / 2
                atr = self._atr_at(len(self.df) - 1)
                if b_top < s_top:
                    sl = bot - 0.2 * atr
                    tp = entry + 2 * (entry - sl)
                    direction = "BUY"
                else:
                    sl = top + 0.2 * atr
                    tp = entry - 2 * (sl - entry)
                    direction = "SELL"
                bi = max(b["bar_index"], s["bar_index"])
                out.append(self._setup("Balanced Price Range", direction,
                                       entry, sl, tp, bi, confidence=0.74,
                                       reason="BPR overlap"))
        return out

    # 9. Breaker Block ---------------------------------------------------
    def breaker_block(self, lookback: int = 60) -> List[Dict[str, Any]]:
        obs = self.order_blocks(lookback)
        out = []
        df = self.df
        if len(df) < 2:
            return out
        atr = self._atr_at(len(df) - 1)
        last = float(df["close"].iloc[-1])
        for ob in obs:
            ob_bar = ob["meta"]["ob_bar"]
            after = df.iloc[ob_bar + 1:]
            if len(after) == 0:
                continue
            if ob["direction"] == "BUY":
                broken = (after["close"] < ob["stop_loss"]).any()
                if broken:
                    entry = last
                    sl = ob["stop_loss"] + 0.2 * atr
                    tp = entry - 2 * (sl - entry)
                    out.append(self._setup("Breaker Block", "SELL",
                                           entry, sl, tp, len(df) - 1,
                                           confidence=0.7, reason="Bullish OB broken"))
            else:
                broken = (after["close"] > ob["stop_loss"]).any()
                if broken:
                    entry = last
                    sl = ob["stop_loss"] - 0.2 * atr
                    tp = entry + 2 * (entry - sl)
                    out.append(self._setup("Breaker Block", "BUY",
                                           entry, sl, tp, len(df) - 1,
                                           confidence=0.7, reason="Bearish OB broken"))
        return out

    # 10. Mitigation Block -----------------------------------------------
    def mitigation_block(self, lookback: int = 60) -> List[Dict[str, Any]]:
        obs = self.order_blocks(lookback)
        out = []
        df = self.df
        for ob in obs:
            ob_bar = ob["meta"]["ob_bar"]
            after = df.iloc[ob_bar + 1:]
            if len(after) == 0:
                continue
            if ob["direction"] == "BUY":
                touched = (after["low"] <= ob["entry"]).any()
                violated = (after["close"] < ob["stop_loss"]).any()
                if touched and not violated:
                    out.append(self._setup("Mitigation Block", "BUY",
                                           ob["entry"], ob["stop_loss"],
                                           ob["take_profit"], ob_bar,
                                           confidence=0.66, reason="Mitigated OB"))
            else:
                touched = (after["high"] >= ob["entry"]).any()
                violated = (after["close"] > ob["stop_loss"]).any()
                if touched and not violated:
                    out.append(self._setup("Mitigation Block", "SELL",
                                           ob["entry"], ob["stop_loss"],
                                           ob["take_profit"], ob_bar,
                                           confidence=0.66, reason="Mitigated OB"))
        return out

    # 11. Silver Bullet --------------------------------------------------
    def silver_bullet(self, lookback: int = 30) -> List[Dict[str, Any]]:
        out = []
        df = self.df
        n = len(df)
        start = max(3, n - lookback)
        for i in range(start, n):
            ts = df.index[i]
            if not isinstance(ts, pd.Timestamp):
                continue
            dt = ts.to_pydatetime()
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            # Silver Bullet window: 14:00–15:00 UTC
            if not (14 <= dt.hour < 15):
                continue
            w = df.iloc[max(0, i - 2): i + 1]
            if len(w) < 3:
                continue
            atr = self._atr_at(i)
            if atr <= 0:
                continue
            if w["low"].iloc[2] > w["high"].iloc[0]:
                gt, gb = float(w["low"].iloc[2]), float(w["high"].iloc[0])
                entry = (gt + gb) / 2
                sl = gb - 0.1 * atr
                tp = entry + 2 * (entry - sl)
                out.append(self._setup("Silver Bullet", "BUY", entry, sl, tp, i,
                                       confidence=0.8, reason="SB bullish FVG"))
            if w["high"].iloc[2] < w["low"].iloc[0]:
                gt, gb = float(w["low"].iloc[0]), float(w["high"].iloc[2])
                entry = (gt + gb) / 2
                sl = gt + 0.1 * atr
                tp = entry - 2 * (sl - entry)
                out.append(self._setup("Silver Bullet", "SELL", entry, sl, tp, i,
                                       confidence=0.8, reason="SB bearish FVG"))
        return out

    # 12. Optimal Trade Entry --------------------------------------------
    def optimal_trade_entry(self, lookback: int = 60) -> List[Dict[str, Any]]:
        df = self.df
        n = len(df)
        if n < 20:
            return []
        window = df.iloc[-min(lookback, n):]
        hi = window["high"].idxmax()
        lo = window["low"].idxmin()
        sh = float(window["high"].max())
        sl = float(window["low"].min())
        leg = sh - sl
        if leg <= 0:
            return []
        last = float(df["close"].iloc[-1])
        atr = self._atr_at(n - 1)
        hi_pos = df.index.get_loc(hi) if hi in df.index else 0
        lo_pos = df.index.get_loc(lo) if lo in df.index else 0
        out = []
        if hi_pos > lo_pos:
            top = sh - 0.62 * leg
            bot = sh - 0.79 * leg
            if bot <= last <= top:
                entry = last
                stop = sl - 0.1 * atr
                tp = sh
                out.append(self._setup("Optimal Trade Entry", "BUY",
                                       entry, stop, tp, n - 1,
                                       confidence=0.76, reason="OTE 62-79% retrace"))
        else:
            bot = sl + 0.62 * leg
            top = sl + 0.79 * leg
            if bot <= last <= top:
                entry = last
                stop = sh + 0.1 * atr
                tp = sl
                out.append(self._setup("Optimal Trade Entry", "SELL",
                                       entry, stop, tp, n - 1,
                                       confidence=0.76, reason="OTE 62-79% retrace"))
        return out

    # 13. Inversion FVG --------------------------------------------------
    def inversion_fvg(self, lookback: int = 60) -> List[Dict[str, Any]]:
        fvgs = self.fair_value_gap(lookback)
        out = []
        df = self.df
        n = len(df)
        if n < 2:
            return out
        atr = self._atr_at(n - 1)
        last = float(df["close"].iloc[-1])
        for f in fvgs:
            gt, gb = f["meta"]["gap_top"], f["meta"]["gap_bot"]
            after = df.iloc[f["bar_index"] + 1:]
            if len(after) == 0:
                continue
            if f["direction"] == "BUY":
                if not (after["close"] < gb).any():
                    continue
                if gb <= last <= gt:
                    entry = last
                    sl = gt + 0.2 * atr
                    tp = entry - 2 * (sl - entry)
                    out.append(self._setup("Inversion FVG", "SELL", entry, sl, tp, n - 1,
                                           confidence=0.7, reason="IFVG bearish"))
            else:
                if not (after["close"] > gt).any():
                    continue
                if gb <= last <= gt:
                    entry = last
                    sl = gb - 0.2 * atr
                    tp = entry + 2 * (entry - sl)
                    out.append(self._setup("Inversion FVG", "BUY", entry, sl, tp, n - 1,
                                           confidence=0.7, reason="IFVG bullish"))
        return out

    # 14. Volume Gap -----------------------------------------------------
    def volume_gap(self, lookback: int = 60) -> List[Dict[str, Any]]:
        df = self.df
        if df["volume"].sum() == 0:
            return []
        fvgs = self.fair_value_gap(lookback)
        vol = df["volume"]
        avg = vol.rolling(20, min_periods=1).mean()
        out = []
        for f in fvgs:
            mid = f["bar_index"] - 1
            if mid < 0 or mid >= len(df):
                continue
            base = float(avg.iloc[mid]) or 1.0
            ratio = float(vol.iloc[mid]) / base
            if ratio < 0.75:
                out.append(self._setup("Volume Gap", f["direction"],
                                       f["entry"], f["stop_loss"], f["take_profit"],
                                       f["bar_index"], confidence=0.68,
                                       reason="Low-volume FVG"))
        return out

    # 15. Inducement -----------------------------------------------------
    def inducement(self, lookback: int = 40) -> List[Dict[str, Any]]:
        out = []
        df = self.df
        n = len(df)
        highs, lows = _swings(df, 1)
        start = max(3, n - lookback)
        for i in range(start, n):
            atr = self._atr_at(i)
            if atr <= 0:
                continue
            for sh in reversed(highs):
                if sh >= i or (i - sh) > 15:
                    continue
                if df["high"].iloc[i] > df["high"].iloc[sh] and \
                        df["close"].iloc[i] < df["high"].iloc[sh]:
                    entry = float(df["close"].iloc[i])
                    sl = float(df["high"].iloc[i]) + 0.1 * atr
                    tp = entry - 2 * (sl - entry)
                    out.append(self._setup("Inducement", "SELL",
                                           entry, sl, tp, i, confidence=0.66,
                                           reason="IDM sweep high"))
                    break
            for sl_i in reversed(lows):
                if sl_i >= i or (i - sl_i) > 15:
                    continue
                if df["low"].iloc[i] < df["low"].iloc[sl_i] and \
                        df["close"].iloc[i] > df["low"].iloc[sl_i]:
                    entry = float(df["close"].iloc[i])
                    sl = float(df["low"].iloc[i]) - 0.1 * atr
                    tp = entry + 2 * (entry - sl)
                    out.append(self._setup("Inducement", "BUY",
                                           entry, sl, tp, i, confidence=0.66,
                                           reason="IDM sweep low"))
                    break
        return out

    # 16. Power of 3 (AMD) -----------------------------------------------
    def power_of_3(self, lookback: int = 50) -> List[Dict[str, Any]]:
        df = self.df
        n = len(df)
        out = []
        if n < 25:
            return out
        for i in range(max(20, n - lookback), n):
            acc = df.iloc[i - 20: i - 5]
            manip = df.iloc[i - 5: i]
            if len(acc) < 10:
                continue
            atr = self._atr_at(i)
            if atr <= 0:
                continue
            rng = float(acc["high"].max() - acc["low"].min())
            if rng > 3.5 * atr:
                continue
            hi = float(acc["high"].max())
            lo = float(acc["low"].min())
            swept_hi = bool((manip["high"] > hi).any())
            swept_lo = bool((manip["low"] < lo).any())
            if swept_hi and not swept_lo:
                entry = float(df["close"].iloc[i])
                sl = float(manip["high"].max()) + 0.1 * atr
                tp = entry - 2 * (sl - entry)
                out.append(self._setup("Power of 3 (AMD)", "SELL",
                                       entry, sl, tp, i, confidence=0.75,
                                       reason="AMD distribution"))
            elif swept_lo and not swept_hi:
                entry = float(df["close"].iloc[i])
                sl = float(manip["low"].min()) - 0.1 * atr
                tp = entry + 2 * (entry - sl)
                out.append(self._setup("Power of 3 (AMD)", "BUY",
                                       entry, sl, tp, i, confidence=0.75,
                                       reason="AMD distribution"))
        return out

    # 17. Institutional Order Flow ---------------------------------------
    def institutional_order_flow(self, lookback: int = 30) -> List[Dict[str, Any]]:
        out = []
        df = self.df
        n = len(df)
        start = max(3, n - lookback)
        for i in range(start, n):
            bodies = [float(df["close"].iloc[j] - df["open"].iloc[j])
                      for j in range(i - 2, i + 1)]
            atr = self._atr_at(i)
            if atr <= 0:
                continue
            if all(b > 0 for b in bodies) and bodies[-1] > bodies[0]:
                entry = float(df["close"].iloc[i])
                sl = float(df["low"].iloc[i - 2]) - 0.1 * atr
                tp = entry + 2 * (entry - sl)
                out.append(self._setup("Institutional Order Flow", "BUY",
                                       entry, sl, tp, i, confidence=0.72,
                                       reason="IOF bullish"))
            elif all(b < 0 for b in bodies) and bodies[-1] < bodies[0]:
                entry = float(df["close"].iloc[i])
                sl = float(df["high"].iloc[i - 2]) + 0.1 * atr
                tp = entry - 2 * (sl - entry)
                out.append(self._setup("Institutional Order Flow", "SELL",
                                       entry, sl, tp, i, confidence=0.72,
                                       reason="IOF bearish"))
        return out

    # 18. Premium/Discount -----------------------------------------------
    def premium_discount(self, lookback: int = 60) -> List[Dict[str, Any]]:
        df = self.df
        n = len(df)

        if n < lookback:
            return []

        w = df.iloc[-lookback:]
        hi = float(w["high"].max())
        lo = float(w["low"].min())
        mid = (hi + lo) / 2

        last = float(df["close"].iloc[-1])
        atr = self._atr_at(n - 1)

        if atr <= 1e-5:
            return []

        if last < mid:
            entry = last
            sl = lo - (0.5 * atr)
            tp = mid + (mid - lo) * 0.5

            return [self._setup(
                "Premium/Discount", "BUY",
                entry, sl, tp, n - 1,
                confidence=0.62,
                reason="Discount zone"
            )]

        entry = last
        sl = hi + (0.5 * atr)
        tp = mid - (hi - mid) * 0.5

        return [self._setup(
            "Premium/Discount", "SELL",
            entry, sl, tp, n - 1,
            confidence=0.62,
            reason="Premium zone"
        )]

    # 19. Quarterly Theory -----------------------------------------------
    def quarterly_theory(self, lookback: int = 20) -> List[Dict[str, Any]]:
        out = []
        df = self.df
        n = len(df)
        start = max(1, n - lookback)
        for i in range(start, n):
            ts = df.index[i]
            if not isinstance(ts, pd.Timestamp):
                continue
            dt = ts.to_pydatetime()
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            mod = dt.hour * 60 + dt.minute
            if not (13 * 60 <= mod < 16 * 60):
                continue
            q = (mod - 13 * 60) // 45 + 1
            direction = "BUY" if df["close"].iloc[i] > df["open"].iloc[i] else "SELL"
            entry = float(df["close"].iloc[i])
            atr = self._atr_at(i)
            if direction == "BUY":
                sl = float(df["low"].iloc[i]) - 0.2 * atr
                tp = entry + 2 * (entry - sl)
            else:
                sl = float(df["high"].iloc[i]) + 0.2 * atr
                tp = entry - 2 * (sl - entry)
            out.append(self._setup("Quarterly Theory", direction,
                                   entry, sl, tp, i, confidence=0.56 + 0.05 * q,
                                   reason=f"NY AM Q{q}"))
        return out

    # 20. Daily Bias -----------------------------------------------------
    def daily_bias(self) -> List[Dict[str, Any]]:
        df = self.df
        n = len(df)
        if n < 20:
            return []
        w = df["close"].iloc[-min(20, n):]
        mean = float(w.mean())
        last = float(df["close"].iloc[-1])
        atr = self._atr_at(n - 1)
        if last > mean:
            sl = last - 2 * atr
            tp = last + 4 * atr
            return [self._setup("Daily Bias", "BUY", last, sl, tp, n - 1,
                                confidence=0.56, reason="Above 20-mean")]
        sl = last + 2 * atr
        tp = last - 4 * atr
        return [self._setup("Daily Bias", "SELL", last, sl, tp, n - 1,
                            confidence=0.56, reason="Below 20-mean")]

    # ------------------------------------------------------------------
    def run_all(self) -> List[Dict[str, Any]]:
        if len(self.df) < 30:
            return []
        all_setups: List[Dict[str, Any]] = []
        for fn in (self.order_blocks, self.fair_value_gap, self.liquidity_sweep,
                   self.break_of_structure, self.market_structure_shift,
                   self.kill_zones, self.judas_swing, self.balanced_price_range,
                   self.breaker_block, self.mitigation_block, self.silver_bullet,
                   self.optimal_trade_entry, self.inversion_fvg, self.volume_gap,
                   self.inducement, self.power_of_3, self.institutional_order_flow,
                   self.premium_discount, self.quarterly_theory, self.daily_bias):
            try:
                all_setups.extend(fn())
            except Exception as e:
                logger.warning("Detector %s failed: %s", fn.__name__, e)
        return all_setups


# =============================================================================
# PUBLIC ENTRY POINT
# =============================================================================
def analyze_symbol(symbol: str, timeframe: str, style: str,
                   filters: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """
    Run all 20 detectors on real candles.
    Returns {ok, error?, symbol, timeframe, price, atr, setups}.
    """
    fetcher = CandleFetcher()
    df = fetcher.get(symbol, timeframe)
    if df is None or len(df) < 30:
        return {"ok": False, "error": f"No data for {symbol}@{timeframe}",
                "symbol": symbol, "timeframe": timeframe, "setups": []}

    atr_series = _atr(df, 14)
    last_price = float(df["close"].iloc[-1])
    last_atr = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0

    det = ICTDetectors(df, atr_series, symbol, timeframe, style, filters)
    setups = det.run_all()

    # Filter by style TF and valid SL/TP direction
    valid: List[Dict[str, Any]] = []
    for s in setups:
        if s["direction"] == "BUY" and s["stop_loss"] >= s["entry"]:
            continue
        if s["direction"] == "SELL" and s["stop_loss"] <= s["entry"]:
            continue
        if s["risk_reward"] < 1.2:
            continue
        if s["confidence"] < 0.55:
            continue
        valid.append(s)

    return {
        "ok": True,
        "symbol": symbol,
        "timeframe": timeframe,
        "price": round(last_price, 5),
        "atr": round(last_atr, 5),
        "setups": valid,
    }


# =============================================================================
# LIVE PRICES (TwelveData — 15s cache)
# =============================================================================
_price_cache: Dict[str, Dict[str, Any]] = {}
_price_cache_time: float = 0.0


def get_live_prices(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    global _price_cache, _price_cache_time
    now = _time.time()

    # تحديث كل 15 ثانية فقط لتجنب حظر TwelveData
    if now - _price_cache_time < 15 and _price_cache:
        return _price_cache

    td_key = os.getenv("TWELVE_DATA_API_KEY")
    if not td_key:
        logger.warning("TWELVE_DATA_API_KEY is missing!")
        return _price_cache

    # رموز TwelveData الفورية (تطابق MT5)
    td_syms = {
        "XAU/USD": "XAU/USD", "XAG/USD": "XAG/USD", "WTI/USD": "WTI/USD",
        "EUR/USD": "EUR/USD", "GBP/USD": "GBP/USD", "USD/JPY": "USD/JPY",
        "DXY": "DXY"
    }

    sym_str = ",".join([td_syms[s] for s in symbols if s in td_syms])
    url = f"https://api.twelvedata.com/quote?symbol={sym_str}&apikey={td_key}"

    try:
        r = requests.get(url, timeout=10).json()
        # التحقق من عدم وجود خطأ في الاستجابة
        if isinstance(r, dict) and "code" not in r:
            # إذا كان الرمز واحداً، ترجع TwelveData استجابة مسطحة، وإذا كانت متعددة ترجع قاموس
            results = r if len(symbols) > 1 else {td_syms[symbols[0]]: r}

            for sym, td_sym in td_syms.items():
                if td_sym in results:
                    data = results[td_sym]
                    cur_price = float(data.get("close", 0))
                    if cur_price > 0:
                        _price_cache[sym] = {
                            "price": round(cur_price, 5),
                            "change_pct": round(float(data.get("percent_change", 0)), 2)
                        }
            _price_cache_time = now
            return _price_cache
        else:
            logger.warning(f"TwelveData API limit reached or error: {r}")
            return _price_cache

    except Exception as e:
        logger.warning(f"TwelveData fetch failed: {e}")
        return _price_cache


# =============================================================================
# FALLBACK NEWS FETCHER (with CORS proxies)
# =============================================================================
def fetch_news_with_fallback() -> List[Dict[str, Any]]:
    """Fetch ForexFactory news bypassing Render IP block using proxies."""
    target_url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    urls = [
        target_url,  # Try direct first
        f"https://api.codetabs.com/v1/proxy?quest={target_url}",
        f"https://corsproxy.io/?{target_url}",
        f"https://api.allorigins.win/raw?url={target_url}"
    ]
    
    for url in urls:
        try:
            r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and len(data) > 0:
                    logger.info(f"News fetched successfully from: {url[:50]}...")
                    return _normalize_news(data)
        except Exception as e:
            logger.debug("News from %s failed: %s", url[:40], e)
            
    logger.warning("All news sources failed or were blocked.")
    return []


def _normalize_news(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for item in raw:
        try:
            dt = pd.to_datetime(item.get("date"), utc=True).to_pydatetime()
        except Exception:
            continue
        impact = str(item.get("impact", "Medium")).lower()
        if impact not in ("high", "medium", "low"):
            impact = "medium"
        out.append({
            "source": "forexfactory",
            "title": item.get("title", "—"),
            "currency": item.get("country", ""),
            "impact": impact,
            "event_time": dt.isoformat(),
            "forecast": item.get("forecast", ""),
            "previous": item.get("previous", ""),
            "actual": item.get("actual", ""),
        })
    return out
