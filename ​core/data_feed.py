# core/data_feed.py
"""
core/data_feed.py

Real-time market data and news feed integration.

Notes on "zero-delay":
    True zero-latency is not achievable over public networks. This module
    minimizes delay by:
      1) Using provider push streams (WebSocket) when available.
      2) Preferring tick-level updates over polling when the provider allows.
      3) Maintaining an in-memory cache to avoid blocking I/O on hot paths.

The provided concrete classes are adapters/stubs. Substitute the marked
methods with your broker/vendor SDK (OANDA, Binance, Polygon, DTN, etc.).

Includes:
    * LivePrice / Candle dataclasses.
    * DataFeed interface + StreamingDataFeed reference implementation.
    * NewsEvent + NewsStreamParser for macro/geopolitical events.
    * TradingGuard that suspends trading during high-impact news and
      enforces a 10-minute post-news stabilization wait window.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import AsyncIterator, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class LivePrice:
    symbol: str
    bid: float
    ask: float
    timestamp: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class Candle:
    symbol: str
    timeframe: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class NewsImpact(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class NewsEvent:
    id: str
    title: str
    source: str
    published_at: datetime
    impact: NewsImpact
    currencies: tuple = ()           # e.g. ("USD", "EUR")
    symbols: tuple = ()              # e.g. ("XAU/USD", "EUR/USD")
    tags: tuple = ()                 # e.g. ("geopolitical", "macro", "cpi")
    scheduled_at: Optional[datetime] = None  # for scheduled releases
    description: str = ""


# ---------------------------------------------------------------------
# DataFeed interface
# ---------------------------------------------------------------------
class DataFeed(abc.ABC):
    """Abstract market data feed."""

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def subscribe(self, symbols: Iterable[str]) -> None: ...

    @abc.abstractmethod
    async def stream_prices(self) -> AsyncIterator[LivePrice]: ...

    @abc.abstractmethod
    async def stream_candles(self, timeframe: str) -> AsyncIterator[Candle]: ...


class StreamingDataFeed(DataFeed):
    """
    Reference streaming feed.

    Replace `_connect_ws`, `_subscribe_ws`, and `_normalize_ws_message`
    with a concrete vendor implementation. The rest of the class stays
    the same.
    """

    def __init__(self, provider_name: str = "generic") -> None:
        self.provider_name = provider_name
        self._symbols: List[str] = []
        self._price_queue: "asyncio.Queue[LivePrice]" = asyncio.Queue()
        self._candle_queue: "asyncio.Queue[Candle]" = asyncio.Queue()
        self._tasks: List[asyncio.Task] = []
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._tasks.append(asyncio.create_task(self._ws_loop()))
        logger.info("StreamingDataFeed[%s] started", self.provider_name)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        logger.info("StreamingDataFeed[%s] stopped", self.provider_name)

    async def subscribe(self, symbols: Iterable[str]) -> None:
        self._symbols = list(symbols)
        await self._subscribe_ws(self._symbols)

    async def stream_prices(self) -> AsyncIterator[LivePrice]:
        while self._running:
            yield await self._price_queue.get()

    async def stream_candles(self, timeframe: str) -> AsyncIterator[Candle]:
        # In a real implementation, candle timeframe would be part of the
        # subscription. The queue is shared here for simplicity.
        while self._running:
            yield await self._candle_queue.get()

    # -- vendor hooks -----------------------------------------------------
    async def _connect_ws(self):
        """Return an open websocket connection. Vendor-specific."""
        raise NotImplementedError("Wire up your provider's websocket client")

    async def _subscribe_ws(self, symbols: List[str]) -> None:
        """Send subscription message(s) on the websocket. Vendor-specific."""
        logger.debug("Subscribing to %s (stub)", symbols)

    def _normalize_ws_message(self, raw: dict):
        """
        Return (LivePrice | Candle) list from raw vendor payload.
        Vendor-specific. This stub produces synthetic events for testing.
        """
        return []

    async def _ws_loop(self) -> None:
        """
        Main loop: connect -> read messages -> normalize -> dispatch.
        Includes exponential backoff reconnect.
        """
        backoff = 1.0
        while self._running:
            try:
                ws = await self._connect_ws()
                backoff = 1.0
                async for raw in ws:  # type: ignore[union-attr]
                    for item in self._normalize_ws_message(raw):
                        if isinstance(item, LivePrice):
                            await self._price_queue.put(item)
                        elif isinstance(item, Candle):
                            await self._candle_queue.put(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Websocket error; reconnecting in %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


# ---------------------------------------------------------------------
# News stream parser
# ---------------------------------------------------------------------
class NewsStreamParser:
    """
    Parses a provider's news / economic calendar stream into NewsEvent
    objects and exposes an async iterator.

    Wire `_connect_news_stream` and `_parse_raw_news` to your vendor.
    The guard below relies on scheduled events (e.g., NFP, CPI, FOMC).
    """

    def __init__(
        self,
        high_impact_tags: Iterable[str] = (
            "cpi", "nfp", "fomc", "gdp", "rate_decision", "war", "sanctions",
        ),
        high_impact_currencies: Iterable[str] = ("USD", "EUR", "GBP", "JPY"),
    ) -> None:
        self.high_impact_tags = {t.lower() for t in high_impact_tags}
        self.high_impact_currencies = {c.upper() for c in high_impact_currencies}
        self._queue: "asyncio.Queue[NewsEvent]" = asyncio.Queue()
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._reader_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def stream(self) -> AsyncIterator[NewsEvent]:
        while self._running:
            yield await self._queue.get()

    # -- vendor hooks -----------------------------------------------------
    async def _connect_news_stream(self):
        """Return an async iterator of raw news dicts. Vendor-specific."""
        raise NotImplementedError("Wire up your news provider")

    def _parse_raw_news(self, raw: dict) -> Optional[NewsEvent]:
        """
        Map a vendor payload to a NewsEvent. Vendor-specific.
        Return None to drop the message.
        """
        return None

    def _classify_impact(self, event: NewsEvent) -> NewsImpact:
        tags = {t.lower() for t in event.tags}
        currencies = {c.upper() for c in event.currencies}
        if tags & self.high_impact_tags or currencies & self.high_impact_currencies:
            return NewsImpact.HIGH
        return event.impact

    async def _reader_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                stream = await self._connect_news_stream()
                backoff = 1.0
                async for raw in stream:  # type: ignore[union-attr]
                    event = self._parse_raw_news(raw)
                    if event is None:
                        continue
                    # Reclassify to be safe.
                    event = NewsEvent(
                        id=event.id,
                        title=event.title,
                        source=event.source,
                        published_at=event.published_at,
                        impact=self._classify_impact(event),
                        currencies=event.currencies,
                        symbols=event.symbols,
                        tags=event.tags,
                        scheduled_at=event.scheduled_at,
                        description=event.description,
                    )
                    await self._queue.put(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("News stream error; reconnecting in %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)


# ---------------------------------------------------------------------
# Trading guard
# ---------------------------------------------------------------------
class TradingGuard:
    """
    Central safety net around news.

    Rules:
      * A HIGH-impact event (published or scheduled) whose currencies /
        symbols overlap the bot's exposure suspends trading.
      * Suspension begins `pre_event_window` before `scheduled_at` (if set)
        or at `published_at`.
      * Suspension lasts until `post_event_stabilization` (default 10 min)
        after the event's publish/start time.
      * Multiple overlapping events keep the guard suspended until all
        windows have expired.

    Query the guard from your execution path via `is_suspended()` and
    `suspension_reason()`.
    """

    def __init__(
        self,
        symbols: Iterable[str],
        currencies: Iterable[str],
        pre_event_window: timedelta = timedelta(minutes=5),
        post_event_stabilization: timedelta = timedelta(minutes=10),
        suspension_callbacks: Optional[List[Callable[[NewsEvent], None]]] = None,
        resume_callbacks: Optional[List[Callable[[], None]]] = None,
    ) -> None:
        self.symbols = {s.upper() for s in symbols}
        self.currencies = {c.upper() for c in currencies}
        self.pre_window = pre_event_window
        self.post_window = post_event_stabilization

        self._suspend_callbacks = list(suspension_callbacks or [])
        self._resume_callbacks = list(resume_callbacks or [])

        # Active suspension windows: id -> (start, end, event)
        self._windows: Dict[str, tuple] = {}
        self._lock = asyncio.Lock()

    # -- public API -------------------------------------------------------
    def is_suspended(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        # Clean expired windows (cheap for typical small counts).
        expired = [eid for eid, (_, end, _) in self._windows.items() if now >= end]
        for eid in expired:
            self._windows.pop(eid, None)
            for cb in self._resume_callbacks:
                try:
                    cb()
                except Exception:
                    logger.exception("Resume callback raised")
        return bool(self._windows)

    def suspension_reason(self) -> Optional[str]:
        if not self._windows:
            return None
        # Report the event with the latest end time.
        _, (_, end, event) = max(self._windows.items(), key=lambda kv: kv[1][1])
        return f"Suspended until {end.isoformat()} due to: {event.title}"

    def seconds_until_resume(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        if not self._windows:
            return 0.0
        end = max(end for _, end, _ in self._windows.values())
        return max(0.0, (end - now).total_seconds())

    async def handle_event(self, event: NewsEvent) -> None:
        """Feed a NewsEvent into the guard. Suspends if relevant + HIGH."""
        if event.impact != NewsImpact.HIGH:
            return
        if not self._is_relevant(event):
            return

        # Timing: pre-event window if scheduled, else published_at.
        anchor = event.scheduled_at or event.published_at
        start = anchor - self.pre_window
        end = anchor + self.post_window
        now = datetime.now(timezone.utc)

        if now >= end:
            logger.debug("Ignoring expired news event: %s", event.title)
            return

        async with self._lock:
            was_suspended = self.is_suspended(now)
            self._windows[event.id] = (start, end, event)
            if not was_suspended:
                logger.warning(
                    "Trading suspended by news: %s (until %s)",
                    event.title,
                    end.isoformat(),
                )
                for cb in self._suspend_callbacks:
                    try:
                        cb(event)
                    except Exception:
                        logger.exception("Suspension callback raised")

    # -- internals --------------------------------------------------------
    def _is_relevant(self, event: NewsEvent) -> bool:
        ev_syms = {s.upper() for s in event.symbols}
        ev_ccys = {c.upper() for c in event.currencies}
        if ev_syms & self.symbols:
            return True
        if ev_ccys & self.currencies:
            return True
        return False


# ---------------------------------------------------------------------
# Convenience wiring
# ---------------------------------------------------------------------
class LiveDataHub:
    """
    Convenience wrapper that runs the data feed, news parser, and guard
    together on a single asyncio event loop.
    """

    def __init__(
        self,
        feed: DataFeed,
        news: NewsStreamParser,
        guard: TradingGuard,
    ) -> None:
        self.feed = feed
        self.news = news
        self.guard = guard
        self._price_history: Dict[str, LivePrice] = {}

    async def run(self, symbols: Iterable[str], timeframe: str = "1m") -> None:
        await self.feed.start()
        await self.feed.subscribe(symbols)
        await self.news.start()

        await asyncio.gather(
            self._pump_prices(),
            self._pump_news(),
            self._pump_candles(timeframe),
        )

    async def _pump_prices(self) -> None:
        async for price in self.feed.stream_prices():
            # Fast-path cache write; execution layer reads this directly.
            self._price_history[price.symbol] = price

    async def _pump_news(self) -> None:
        async for event in self.news.stream():
            await self.guard.handle_event(event)

    async def _pump_candles(self, timeframe: str) -> None:
        async for candle in self.feed.stream_candles(timeframe):
            logger.debug(
                "Candle %s %s o=%s c=%s",
                candle.symbol,
                candle.timeframe,
                candle.open,
                candle.close,
            )

    def last_price(self, symbol: str) -> Optional[LivePrice]:
        return self._price_history.get(symbol)
