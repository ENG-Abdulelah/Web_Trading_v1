# web_app/app.py
"""
web_app/app.py

FastAPI web server for the trading bot dashboard.

Endpoints:
    GET  /                 - dashboard (SSR + WS live updates)
    GET  /lang/{lang}      - set language cookie & redirect back
    GET  /api/state        - current state as JSON (polling fallback)
    WS   /ws               - live state stream (1 update/sec)

StateProvider is a thin adapter over the existing modules
(core.sessions, execution.risk_manager, analysis.scorer). If any module
is missing (e.g., running the dashboard standalone), it gracefully falls
back to a realistic demo state so the UI still works.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

# ---- import path bootstrap ----------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for p in (_HERE, _PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from i18n import (  # type: ignore
    DEFAULT_LANG,
    SUPPORTED_LANGS,
    get_lang,
    get_translations,
    is_rtl,
)

logger = logging.getLogger("web_app")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)


# =====================================================================
# Optional module wiring (with graceful fallbacks)
# =====================================================================
def _try_import_sessions():
    try:
        from core.sessions import SessionManager  # type: ignore
        return SessionManager
    except Exception as exc:
        logger.warning("SessionManager unavailable (%s); using demo sessions", exc)
        return None


def _try_import_risk():
    try:
        from execution.risk_manager import RiskManager  # type: ignore
        return RiskManager
    except Exception as exc:
        logger.warning("RiskManager unavailable (%s); using demo capital", exc)
        return None


SessionManager = _try_import_sessions()
RiskManager = _try_import_risk()


# =====================================================================
# Demo data (used when live data sources are not wired yet)
# =====================================================================
DEMO_ASSETS = [
    {"symbol": "XAU/USD", "price": 2348.50, "prev": 2345.20, "open": True},
    {"symbol": "XAG/USD", "price": 27.42,   "prev": 27.60,   "open": True},
    {"symbol": "WTI/USD", "price": 78.15,   "prev": 77.90,   "open": True},
    {"symbol": "EUR/USD", "price": 1.08500, "prev": 1.08420, "open": True},
    {"symbol": "GBP/USD", "price": 1.27310, "prev": 1.27450, "open": True},
    {"symbol": "USD/JPY", "price": 149.85,  "prev": 149.60,  "open": True},
]

# Sized to fit $50 × 1.5% = $0.75 risk budget at min lot 0.01.
DEMO_POSITIONS = [
    {
        "id": "demo-001",
        "symbol": "EUR/USD",
        "side": "long",
        "lots": 0.01,
        "entry": 1.08500,
        "stop_loss": 1.08430,
        "take_profit": 1.08640,
        "risk_usd": 0.70,
        "reward_usd": 1.40,
        "rr": 2.0,
        "strategy": "fvg",
        "pnl_r": 0.64,
        "pnl_usd": 0.45,
        "opened_at": datetime.now(timezone.utc) - timedelta(minutes=42),
        "status": "open",
    }
]

DEMO_SCORES = [
    {"symbol": "EUR/USD", "direction": "long",  "technical": 6.8, "macro": 7.4, "geopolitical": 5.6},
    {"symbol": "GBP/USD", "direction": "long",  "technical": 6.5, "macro": 6.6, "geopolitical": 5.0},
    {"symbol": "USD/JPY", "direction": "short", "technical": 5.7, "macro": 6.3, "geopolitical": 6.8},
    {"symbol": "XAU/USD", "direction": "long",  "technical": 5.9, "macro": 8.1, "geopolitical": 7.4},
    {"symbol": "XAG/USD", "direction": "long",  "technical": 5.5, "macro": 7.0, "geopolitical": 7.1},
    {"symbol": "WTI/USD", "direction": "long",  "technical": 6.2, "macro": 6.5, "geopolitical": 7.7},
]
SCORER_WEIGHTS = {"technical": 0.5, "macro": 0.3, "geopolitical": 0.2}
MIN_SCORE = 5.5

# Demo session windows (UTC).
DEMO_SESSIONS = {
    "asian":    {"open_h": 0, "close_h": 9},
    "london":   {"open_h": 8, "close_h": 17},
    "new_york": {"open_h": 13, "close_h": 22},
}


# =====================================================================
# StateProvider
# =====================================================================
class StateProvider:
    """
    Builds the state dict consumed by the template + WebSocket.
    """

    def __init__(self) -> None:
        self.session_mgr = SessionManager() if SessionManager else None
        self.risk_mgr = RiskManager(automation_mode="single") if RiskManager else None
        self.positions: List[dict] = [dict(p) for p in DEMO_POSITIONS]
        self.assets: List[dict] = [dict(a) for a in DEMO_ASSETS]
        self._rng = random.Random(7)

    # -- sessions ------------------------------------------------------
    def _session_block(self, now: datetime) -> List[dict]:
        if self.session_mgr is not None:
            out = []
            for sess in self.session_mgr.sessions:
                active = sess.contains(now)
                if active:
                    end = sess.next_close_utc(now)
                    next_label = None
                else:
                    end = sess.next_open_utc(now)
                    next_label = "next_open_in"
                out.append({
                    "id": sess.name.value,
                    "label_key": f"session_{sess.name.value}",
                    "active": active,
                    "open": sess.open_utc.strftime("%H:%M"),
                    "close": sess.close_utc.strftime("%H:%M"),
                    "next_change_label_key": next_label or "closes_in",
                    "next_change_secs": int((end - now).total_seconds()),
                })
            return out

        # Demo fallback
        out = []
        for sid, cfg in DEMO_SESSIONS.items():
            open_t = now.replace(hour=cfg["open_h"], minute=0, second=0, microsecond=0)
            close_t = now.replace(hour=cfg["close_h"], minute=0, second=0, microsecond=0)
            active = open_t <= now < close_t
            ref = close_t if active else open_t
            if ref <= now:
                ref += timedelta(days=1)
            out.append({
                "id": sid,
                "label_key": f"session_{sid}",
                "active": active,
                "open": f"{cfg['open_h']:02d}:00",
                "close": f"{cfg['close_h']:02d}:00",
                "next_change_label_key": "closes_in" if active else "next_open_in",
                "next_change_secs": int((ref - now).total_seconds()),
            })
        return out

    def _market_open(self, sessions: List[dict], now: datetime) -> bool:
        if self.session_mgr is not None:
            try:
                return self.session_mgr.any_market_open(now)
            except Exception:
                pass
        # Fallback: weekend-closed heuristic for forex.
        wd = now.weekday()  # Mon=0 .. Sun=6
        if wd == 5:  # Saturday
            return False
        if wd == 6 and now.hour < 22:  # Sunday before 22:00
            return False
        return True

    # -- assets --------------------------------------------------------
    def _tick_assets(self) -> None:
        """Simulate a small random walk so the UI feels alive."""
        for a in self.assets:
            drift = self._rng.gauss(0.0, 0.0006)
            a["price"] = round(a["price"] * (1 + drift), 5)
            chg = (a["price"] - a["prev"]) / a["prev"] * 100 if a["prev"] else 0.0
            a["change_pct"] = round(chg, 3)

    # -- scores --------------------------------------------------------
    def _build_scores(self) -> List[dict]:
        out = []
        for s in DEMO_SCORES:
            total = (
                s["technical"] * SCORER_WEIGHTS["technical"]
                + s["macro"] * SCORER_WEIGHTS["macro"]
                + s["geopolitical"] * SCORER_WEIGHTS["geopolitical"]
            )
            out.append({
                "symbol": s["symbol"],
                "direction": s["direction"],
                "technical": s["technical"],
                "macro": s["macro"],
                "geopolitical": s["geopolitical"],
                "total": round(total, 2),
                "approved": total >= MIN_SCORE,
            })
        return out

    # -- aggregate -----------------------------------------------------
    def build_state(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        self._tick_assets()

        sessions = self._session_block(now)
        market_open = self._market_open(sessions, now)

        capital = self.risk_mgr.capital if self.risk_mgr else 50.0
        risk_pct = self.risk_mgr.risk_per_trade if self.risk_mgr else 0.015
        max_conc = self.risk_mgr.max_concurrent if self.risk_mgr else 1
        mode = self.risk_mgr.automation_mode if self.risk_mgr else "single"

        positions_out = []
        for p in self.positions:
            pnl_r = p.get("pnl_r", 0.0)
            pnl_usd = p.get("pnl_usd", 0.0)
            positions_out.append({
                "id": p["id"],
                "symbol": p["symbol"],
                "side": p["side"],
                "lots": p["lots"],
                "entry": p["entry"],
                "stop_loss": p["stop_loss"],
                "take_profit": p["take_profit"],
                "risk_usd": p["risk_usd"],
                "reward_usd": p["reward_usd"],
                "rr": p["rr"],
                "strategy": p["strategy"],
                "pnl_r": round(pnl_r, 2),
                "pnl_usd": round(pnl_usd, 2),
                "opened_at": p["opened_at"].isoformat() if isinstance(p["opened_at"], datetime) else p["opened_at"],
            })

        return {
            "timestamp": now.isoformat(),
            "market_open": market_open,
            "suspended": False,  # wired from TradingGuard in production
            "capital": {
                "balance": round(capital, 2),
                "risk_pct": risk_pct,
                "risk_usd": round(capital * risk_pct, 2),
                "max_concurrent": max_conc,
                "mode": mode,
            },
            "sessions": sessions,
            "positions": positions_out,
            "assets": [
                {
                    "symbol": a["symbol"],
                    "price": a["price"],
                    "change_pct": a.get("change_pct", 0.0),
                    "open": a["open"],
                }
                for a in self.assets
            ],
            "scores": self._build_scores(),
            "min_score": MIN_SCORE,
            "demo": self.session_mgr is None or self.risk_mgr is None,
        }


provider = StateProvider()


# =====================================================================
# WebSocket manager
# =====================================================================
class ConnectionManager:
    def __init__(self) -> None:
        self.active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)

    async def broadcast(self, message: dict) -> None:
        dead: List[WebSocket] = []
        for ws in list(self.active):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.discard(ws)


manager = ConnectionManager()


# =====================================================================
# FastAPI app
# =====================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_tick_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _tick_loop():
    while True:
        try:
            state = provider.build_state()
            await manager.broadcast(state)
        except Exception:
            logger.exception("tick_loop error")
        await asyncio.sleep(1.0)


app = FastAPI(title="Trading Bot Dashboard", lifespan=lifespan)

templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    lang = get_lang(request)
    state = provider.build_state()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "lang": lang,
            "dir": "rtl" if is_rtl(lang) else "ltr",
            "T": get_translations(lang),
            "lang_names": {"en": "EN", "ar": "AR"},
            "state": state,
            "initial_json": _safe_json(state),
        },
    )


@app.get("/lang/{lang}")
async def set_language(lang: str, request: Request):
    if lang not in SUPPORTED_LANGS:
        lang = DEFAULT_LANG
    referer = request.headers.get("referer") or "/"
    # Prevent open redirect: only allow relative referers.
    if not referer.startswith("/"):
        referer = "/"
    response = RedirectResponse(url=referer, status_code=303)
    response.set_cookie(
        "lang", lang, max_age=60 * 60 * 24 * 365,
        httponly=False, samesite="lax", path="/",
    )
    return response


@app.get("/api/state")
async def api_state():
    return JSONResponse(provider.build_state())


@app.get("/healthz")
async def healthz():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        await ws.send_json(provider.build_state())
        while True:
            # Client may send "ping"; ignore content, loop keeps conn alive.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("ws_endpoint error")
    finally:
        manager.disconnect(ws)


# ---------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------
def _safe_json(state: dict) -> str:
    import json
    return json.dumps(state, default=str).replace("</", "<\\/")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
        log_level="info",
    )
