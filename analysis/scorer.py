"""
Real-time per-strategy scoring for the 20 ICT strategies.

Tracks for each strategy:
    • usage count
    • wins / losses / breakeven
    • total_pnl, avg_pnl, best_trade, worst_trade
    • gross_profit / gross_loss → profit_factor
    • win_rate (%)
    • last_used timestamp
    • open (pending) setups awaiting outcome

Thread-safe, JSON-persistent.

Public API:
    get_scorer() -> StrategyScorer         (module-level singleton)
    StrategyScorer.record_usage(setup)
    StrategyScorer.record_outcome(setup_id, pnl, is_win)
    StrategyScorer.get_stats(name) -> dict
    StrategyScorer.get_all_stats(min_closed=0) -> list[dict]   (ranked)
    StrategyScorer.snapshot() -> dict      (dashboard payload)

Developed & Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

# Canonical names (kept in sync with analysis.ict_engine.STRATEGY_NAMES)
try:
    from analysis.ict_engine import STRATEGY_NAMES as _ENGINE_NAMES
    STRATEGY_NAMES = list(_ENGINE_NAMES)
except Exception:  # pragma: no cover
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
# Per-strategy stats
# ---------------------------------------------------------------------------
@dataclass
class StrategyStats:
    name: str
    usage: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    last_used: Optional[str] = None

    # ------------------------------------------------------------------ derived
    @property
    def closed(self) -> int:
        return self.wins + self.losses + self.breakeven

    @property
    def win_rate(self) -> float:
        if self.closed == 0:
            return 0.0
        return round(self.wins / self.closed * 100, 2)

    @property
    def avg_pnl(self) -> float:
        if self.closed == 0:
            return 0.0
        return round(self.total_pnl / self.closed, 2)

    @property
    def profit_factor(self) -> float:
        if self.gross_loss == 0:
            return 99.0 if self.gross_profit > 0 else 0.0
        return round(self.gross_profit / self.gross_loss, 2)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "usage": self.usage,
            "wins": self.wins,
            "losses": self.losses,
            "breakeven": self.breakeven,
            "closed": self.closed,
            "win_rate": self.win_rate,
            "total_pnl": round(self.total_pnl, 2),
            "avg_pnl": self.avg_pnl,
            "gross_profit": round(self.gross_profit, 2),
            "gross_loss": round(self.gross_loss, 2),
            "profit_factor": self.profit_factor,
            "best_trade": round(self.best_trade, 2),
            "worst_trade": round(self.worst_trade, 2),
            "last_used": self.last_used,
        }


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------
class StrategyScorer:
    """Thread-safe tracker with JSON persistence."""

    _lock = threading.Lock()

    def __init__(self, storage_path: str = "data/strategy_scores.json"):
        self.storage_path = storage_path
        self._stats: dict[str, StrategyStats] = {}
        self._open_setups: dict[str, dict] = {}
        self._load()
        self._seed_missing()

    # ---------------------------------------------------------- persistence
    def _load(self) -> None:
        if not os.path.exists(self.storage_path):
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        with self._lock:
            for name, payload in (data.get("stats") or {}).items():
                stats = StrategyStats(name=name)
                for key, val in payload.items():
                    if hasattr(stats, key) and key != "name":
                        try:
                            setattr(stats, key, val)
                        except Exception:
                            pass
                self._stats[name] = stats
            self._open_setups = data.get("open_setups") or {}

    def _save(self) -> None:
        parent = os.path.dirname(self.storage_path)
        if parent and not os.path.exists(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except Exception:
                pass

        payload = {
            "stats": {n: s.to_dict() for n, s in self._stats.items()},
            "open_setups": self._open_setups,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _seed_missing(self) -> None:
        with self._lock:
            for name in STRATEGY_NAMES:
                if name not in self._stats:
                    self._stats[name] = StrategyStats(name=name)
            self._save()

    # ---------------------------------------------------------- accessors
    def _get(self, name: str) -> StrategyStats:
        if name not in self._stats:
            self._stats[name] = StrategyStats(name=name)
        return self._stats[name]

    # ---------------------------------------------------------- recording
    def record_usage(self, setup) -> None:
        """
        Call once when a new setup is generated.
        `setup` must expose: id, strategy, direction, entry, sl, tp,
                              bar_index, symbol, timeframe
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            stats = self._get(setup.strategy)
            stats.usage += 1
            stats.last_used = now_iso
            self._open_setups[setup.id] = {
                "strategy": setup.strategy,
                "direction": setup.direction,
                "entry": float(setup.entry),
                "sl": float(setup.sl),
                "tp": float(setup.tp),
                "opened_at": now_iso,
                "bar_index": int(setup.bar_index),
                "symbol": setup.symbol,
                "timeframe": setup.timeframe,
            }
            self._save()

    def record_outcome(
        self, setup_id: str, pnl: float, is_win: Optional[bool] = None
    ) -> Optional[dict]:
        """
        Call when a tracked setup closes.
        If `is_win` is omitted, it's derived from pnl sign.
        """
        pnl = float(pnl)
        if is_win is None:
            is_win = pnl > 0

        with self._lock:
            meta = self._open_setups.pop(setup_id, None)
            if meta is None:
                return None

            stats = self._get(meta["strategy"])

            if pnl > 0:
                stats.wins += 1
                stats.gross_profit += pnl
            elif pnl < 0:
                stats.losses += 1
                stats.gross_loss += abs(pnl)
            else:
                stats.breakeven += 1

            stats.total_pnl += pnl
            if pnl > stats.best_trade:
                stats.best_trade = pnl
            if pnl < stats.worst_trade:
                stats.worst_trade = pnl

            self._save()
            return {
                "setup_id": setup_id,
                "strategy": meta["strategy"],
                "pnl": pnl,
                "is_win": is_win,
                "stats": stats.to_dict(),
            }

    # ---------------------------------------------------------- queries
    def get_stats(self, name: str) -> dict:
        with self._lock:
            return self._get(name).to_dict()

    def get_all_stats(self, min_closed: int = 0) -> list[dict]:
        """Return all strategies ranked by win_rate (then usage, then name)."""
        with self._lock:
            items = [
                s.to_dict()
                for s in self._stats.values()
                if s.closed >= min_closed
            ]
        items.sort(key=lambda d: (d["win_rate"], d["usage"], d["name"]), reverse=True)
        for rank, item in enumerate(items, start=1):
            item["rank"] = rank
        return items

    def get_open_setups(self) -> list[dict]:
        with self._lock:
            return [
                {"setup_id": sid, **meta}
                for sid, meta in self._open_setups.items()
            ]

    def snapshot(self) -> dict:
        with self._lock:
            rankings = self.get_all_stats()
            return {
                "total_strategies": len(self._stats),
                "total_usage": sum(s.usage for s in self._stats.values()),
                "total_closed": sum(s.closed for s in self._stats.values()),
                "total_open": len(self._open_setups),
                "total_pnl": round(
                    sum(s.total_pnl for s in self._stats.values()), 2
                ),
                "rankings": rankings,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

    # ---------------------------------------------------------- admin
    def reset(self) -> None:
        """Wipe all stats and open setups, then re-seed the 20 names."""
        with self._lock:
            self._stats.clear()
            self._open_setups.clear()
            self._save()
        self._seed_missing()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_scorer: Optional[StrategyScorer] = None
_scorer_lock = threading.Lock()


def get_scorer() -> StrategyScorer:
    global _scorer
    if _scorer is None:
        with _scorer_lock:
            if _scorer is None:
                _scorer = StrategyScorer()
    return _scorer


# ---------------------------------------------------------------------------
# Self-test (run:  python -m analysis.scorer)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uuid

    # Reset for a clean demo
    scorer = StrategyScorer(storage_path="data/_self_test_scores.json")
    scorer.reset()

    class _FakeSetup:
        def __init__(self, strategy: str, direction: str = "BUY"):
            self.id = uuid.uuid4().hex[:12]
            self.strategy = strategy
            self.direction = direction
            self.entry = 1.1000
            self.sl = 1.0950
            self.tp = 1.1100
            self.bar_index = 100
            self.symbol = "EUR/USD"
            self.timeframe = "1H"

    # Simulate a run
    demo_strategies = [
        "Order Block",
        "Fair Value Gap",
        "Liquidity Sweep",
        "Silver Bullet",
        "Judas Swing",
    ]
    results = []
    for name in demo_strategies * 3:
        s = _FakeSetup(name, direction="BUY" if len(results) % 2 == 0 else "SELL")
        scorer.record_usage(s)
        import random

        pnl = round(random.uniform(-50, 100), 2)
        out = scorer.record_outcome(s.id, pnl)
        results.append((name, pnl, "win" if pnl > 0 else "loss"))

    print("=" * 68)
    print("  StrategyScorer self-test")
    print("=" * 68)
    snap = scorer.snapshot()
    print(f"Total strategies tracked: {snap['total_strategies']}")
    print(f"Total usage:              {snap['total_usage']}")
    print(f"Total closed:             {snap['total_closed']}")
    print(f"Total PnL:                ${snap['total_pnl']}")
    print()
    print(f"{'Rank':<5}{'Strategy':<28}{'Usage':<7}{'Closed':<8}{'WinRate':<9}{'PnL':<10}")
    for item in snap["rankings"]:
        if item["usage"] == 0:
            continue
        print(
            f"{item['rank']:<5}{item['name']:<28}{item['usage']:<7}"
            f"{item['closed']:<8}{item['win_rate']:<9}{item['total_pnl']:<10}"
        )

    # Cleanup
    try:
        os.remove("data/_self_test_scores.json")
    except Exception:
        pass
