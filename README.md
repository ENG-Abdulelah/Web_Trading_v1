markdown
# Automated ICT/SMC Trading Bot

A modular, production-shaped automated trading bot built around **Inner Circle
Trader (ICT) / Smart Money Concepts** strategies — with adaptive strategy
selection, macro/geopolitical bias scoring, strict risk enforcement on a
**$50 capital / 1.5% risk per trade** budget, and a **bilingual (EN / AR)
real-time dashboard**.

Supported assets: **XAU/USD, XAG/USD, WTI/USD, EUR/USD, GBP/USD, USD/JPY**.

---

## Table of Contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Project Structure](#project-structure)
4. [Quickstart](#quickstart)
5. [Configuration](#configuration)
6. [Running Modes](#running-modes)
7. [Web Dashboard](#web-dashboard)
8. [Monte Carlo Simulation](#monte-carlo-simulation)
9. [Wiring a Real Broker / Data Feed](#wiring-a-real-broker--data-feed)
10. [Risk Discipline](#risk-discipline)
11. [GitHub Setup](#github-setup)
12. [Deployment](#deployment)
13. [Roadmap](#roadmap)
14. [Disclaimer](#disclaimer)

---

## Features

- **Modular ICT strategies** — Order Blocks, Fair Value Gaps, Liquidity Sweeps
  behind a single `ICTStrategy` interface, each with a `tune()` hook.
- **Adaptive engine** — `AdaptiveICTEngine` ranks signals by regime suitability,
  confidence, and historical edge, and automatically re-tunes underperforming
  strategies every *N* trades.
- **Macro + geopolitical bias** — z-scored rates / growth / inflation, news
  sentiment filtered by impact, and time-decayed geopolitical severity, all
  rolled into a per-symbol `FundamentalBias ∈ [-1, +1]`.
- **Multi-dimensional scoring gate** — technical (50%) · macro (30%) ·
  geopolitical (20%), with a **hard 5.5/10 minimum** enforced by
  `TradeScorer.validate_or_raise()`.
- **Session-aware execution** — Asian / London / New York windows, per-asset
  market hours, and an automatic shutdown callback when all markets close.
- **News guard** — high-impact events suspend trading with a **5-minute
  pre-window** and a **10-minute post-news stabilization** wait.
- **Strict risk enforcement** — position sizing from capital × risk% ÷
  (SL pips × pip value). With $50 capital and 1.5% risk, the $0.75 budget is
  too small for many min-lot trades, so `strict_min_lot=True` **rejects** them
  with a clear reason instead of silently over-risking.
- **Structural SL/TP planning** — swing-anchored stops with an ATR floor,
  optional partial TP at +1R, optional breakeven + trailing stop.
- **Transparent trade tickets** — every fill is rendered as a full ASCII
  ticket including slippage, spread, commission, R:R, and realized R when
  closed.
- **10,000-run Monte Carlo** — one history replay + bootstrap to produce
  win rate, expectancy, profit factor, drawdown distribution, and risk of ruin.
- **Bilingual dashboard** — FastAPI + Jinja + WebSocket, fully responsive,
  RTL-aware, live-updating every second.

---

## Architecture

```

┌─────────┐             ┌──────────────┐             ┌──────────────┐
│ core.   │             │ analysis.    │             │ execution.   │
│ sessions│             │ ict_engine   │             │ risk_manager │
│ data_   │             │ fundamentals │             │ entry_exit   │
│ feed    │             │ scorer       │             │ timemachine  │
└────┬────┘             └──────┬───────┘             │ broker_      │
│                         │                     │ connector    │
│                         │                     └──────┬───────┘
│                         │                            │
└────────────┬────────────┴────────────┬───────────────┘
│                         │
▼                         ▼
┌───────────────┐          ┌──────────────┐
│    main.py    │◄────────►│  web_app/    │
│  orchestrator │  state   │  FastAPI +   │
└───────────────┘  bridge  │  i18n + Jinja│
└──────────────┘

```

Signal flow per bar close:

```

new candle ──▶ sessions/guard checks ──▶ engine.select_best()
│
▼
exit_planner.plan()
│
▼
risk.size()
│
▼
fundamentals.bias + geopolitical risk
│
▼
scorer.score()
│
approved (≥5.5/10)? ──no──▶ reject log
│ yes
▼
broker.submit() + ticket
│
▼
risk.reserve() + live position
│
┌──────────────────┴────────────────────┐
▼                                       ▼
bar-by-bar SL/TP                     timeout / shutdown
│                                       │
└──────────────► broker.close() ◄───────┘
│
▼
engine.record_outcome() + risk.release()

```

---

## Project Structure

```

.
├── main.py                  # master orchestrator + CLI
├── config.py                # capital, assets, automation modes, timeframes
├── requirements.txt
├── README.md
├── core/
│   ├── sessions.py          # Asian/London/NY windows + market hours + shutdown
│   └── data_feed.py         # price streams, news parser, TradingGuard
├── analysis/
│   ├── ict_engine.py        # Order Blocks / FVG / Liquidity Sweeps + adaptive loop
│   ├── fundamentals.py      # macro + geopolitical bias
│   └── scorer.py            # multi-dim score + 5.5/10 hard gate
├── execution/
│   ├── risk_manager.py      # lot sizing + 1-or-3-trade concurrency cap
│   ├── entry_exit.py        # SL/TP planning + bar-by-bar simulation
│   ├── timemachine.py       # 10,000-run Monte Carlo
│   └── broker_connector.py  # order submission + TradeTicket renderer
└── web_app/
├── app.py               # FastAPI server (SSR + WebSocket + REST)
├── i18n.py              # EN/AR dictionaries + helpers
└── templates/
└── index.html       # responsive bilingual dashboard

```










## Quickstart

### 1. Clone & enter the project

```bash
git clone https://github.com/<you>/<repo>.git
cd <repo>
```

2. Create a virtual environment

```bash
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

3. Install dependencies

```bash
pip install -r requirements.txt
```

4. Run in demo mode

```bash
python main.py
```

This starts the bot in paper trading using a synthetic feed. Bars close
every 30 seconds, strategies evaluate, and trades are simulated through
SimulatedBroker. You'll see ticket renders and close-out logs in the console.

5. Run with the live dashboard

```bash
python main.py --with-web
```

Then open http://localhost:8000. The top-right pill toggles EN ⇄ AR.

---

Configuration

All defaults live in config.py:

Setting Default Notes
DEFAULT_CAPITAL $50 Starting capital
RISK_PER_TRADE 1.5% Per-trade risk (≈ $0.75 at default capital)
ASSETS 6 symbols XAU, XAG, WTI, EUR/USD, GBP/USD, USD/JPY
AUTOMATION_MODES single / multi 1 or up to 3 concurrent trades
TIMEFRAMES scalping / swing Entry/trend/exit TF presets

Runtime CLI overrides:

```bash
python main.py --capital 100 --mode multi --bar-seconds 15 --log-level DEBUG
```

---

Running Modes

Flag Behaviour
(default) Paper trading, synthetic feed, 30s bars
--mode multi Allow up to 3 concurrent trades
--live Use the live broker adapter (PlaceholderBroker raises until wired)
--with-web Start the FastAPI dashboard on :8000
--sim Run a 10,000-run Monte Carlo and exit
--log-file logs/bot.log Duplicate logs to a file

Live mode requires replacing PlaceholderBroker with a real adapter in
execution/broker_connector.py — see Wiring a Real Broker.

---

Web Dashboard

· Language toggle in the top-right → persists via cookie + Accept-Language
· Live updates via WebSocket (/ws), one state push per second
· REST fallback at /api/state for polling clients
· Responsive layout – 3-col (≥1240px), 2-col (≥900px), single column on
  mobile, with full RTL support when Arabic is active
· Panels: config bar · active sessions with countdowns · open positions
  with P&L · monitored assets with % change · per-symbol alignment scores
  with the 5.5/10 threshold highlighted

Endpoints:

```
GET   /                 SSR dashboard
GET   /lang/{en|ar}     Set language cookie + redirect
GET   /api/state        JSON snapshot
GET   /healthz          Liveness probe
WS    /ws               Live state stream
```

---

Monte Carlo Simulation

```bash
python main.py --sim --sim-runs 10000 --sim-trades 40
```

Uses execution/timemachine.py to:

1. Replay the historical feed once to collect real R-multiples
   (via simulate_trade_outcome), feeding results back into the adaptive
   engine so strategy tuning happens naturally.
2. Bootstrap 10,000 randomized sessions with optional Gaussian slippage
   jitter and print:

```
Monte Carlo: 10,000 runs x 40 trades (400,000 trades total)
  Win rate      : 45.32%  (181288W / 218712L)
  Avg R         : +0.354R
  Expectancy    : +0.354R per trade
  Profit factor : 1.58
  Max DD (mean) : 4.21R
  Max DD (p95)  : 9.14R
  Worst run     : -4.82R
  Best run      : +29.61R
  Risk of ruin  : 6.20%
```

If no historical feed is supplied, it falls back to a synthetic sample so
the module is testable in isolation.

---

Wiring a Real Broker / Data Feed

The project ships with stubs for every vendor-specific hook. Replace
them with your SDK of choice:

Data feed

core/data_feed.py → StreamingDataFeed:

```python
async def _connect_ws(self):              # return an open websocket
async def _subscribe_ws(self, symbols):   # send subscription payload
def _normalize_ws_message(self, raw):     # -> list[LivePrice | Candle]
```

News

core/data_feed.py → NewsStreamParser:

```python
async def _connect_news_stream(self):     # async iterator of raw dicts
def _parse_raw_news(self, raw):           # -> NewsEvent | None
```

Broker

execution/broker_connector.py → BrokerConnector subclass:

```python
def submit(self, request, spec, decision) -> TradeTicket
def close(self, ticket_id, exit_price, r_multiple) -> TradeTicket
def get(self, ticket_id) -> TradeTicket | None
```

The existing SimulatedBroker already produces full TradeTicket objects —
keep that rendering intact for compliance / audit logs.

---

Risk Discipline

The bot is deliberately strict about sizing:

· Risk budget per trade = capital × risk_per_trade (default $0.75).
· Lots = budget ÷ (SL_pips × pip_value_per_lot), floored to lot_step.
· If the resulting lots < min_lot and the min-lot trade would risk more
  than the budget, the trade is rejected (strict_min_lot=True).
  This prevents the classic "0.01 lots on $50 account = 4% risk" trap.
· Concurrent trades are hard-capped at 1 (single) or 3 (multi),
  with a portfolio risk ceiling of capital × risk% × max_concurrent.
· A high-impact news event suspends trading with a 5-minute pre-window
  and a 10-minute post-event stabilization wait.
· When all markets close, SessionManager.check_market_and_shutdown() fires
  once and closes any remaining positions at the last known price.

You can relax any of these rules via TradingBot(...) kwargs or the
RiskManager constructor — but do so deliberately.

---

GitHub Setup

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

Add a .gitignore:

```gitignore
__pycache__/
*.py[cod]
.venv/
.env
*.log
logs/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/
.DS_Store
```

Add a .env.example (never commit real secrets):

```env
BROKER_API_KEY=
BROKER_API_SECRET=
BROKER_ACCOUNT_ID=
NEWS_API_KEY=
WEB_SECRET_KEY=
LOG_LEVEL=INFO
```

Optional CI (.github/workflows/ci.yml):

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: ruff check .
      - run: pytest -q
```

---

Deployment

The bot is a single long-lived Python process (async event loop with an
optional embedded Uvicorn server). Any platform that runs Python 3.10+ works.

Option A — Render

1. Create a Web Service pointing at your repo.
2. Build command: pip install -r requirements.txt
3. Start command: python main.py --with-web --web-host 0.0.0.0 --web-port $PORT
4. Health check path: /healthz

Option B — Railway

```bash
railway init
railway up
```

Set the start command in railway.json:

```json
{
  "deploy": {
    "startCommand": "python main.py --with-web --web-host 0.0.0.0 --web-port $PORT"
  }
}
```

Option C — Fly.io

fly.toml:

```toml
app = "trading-bot"
primary_region = "iad"

[build]
  builder = "paketobuildpacks/builder:base"

[env]
  PORT = "8000"

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = false
  auto_start_machines = true

[[http_service.checks]]
  grace_period = "10s"
  interval = "30s"
  method = "GET"
  path = "/healthz"
  timeout = "5s"
```

```bash
fly launch --no-deploy
fly secrets set BROKER_API_KEY=... BROKER_API_SECRET=...
fly deploy
```

Option D — Docker

Dockerfile:

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
EXPOSE 8000

CMD ["python", "main.py", "--with-web", "--web-host", "0.0.0.0", "--web-port", "8000"]
```

```bash
docker build -t trading-bot .
docker run --rm -p 8000:8000 --env-file .env trading-bot
```

Option E — VPS (systemd)

/etc/systemd/system/trading-bot.service:

```ini
[Unit]
Description=Automated Trading Bot
After=network.target

[Service]
Type=simple
User=trader
WorkingDirectory=/opt/trading-bot
ExecStart=/opt/trading-bot/.venv/bin/python main.py --with-web --web-port 8000
Restart=on-failure
RestartSec=5
Environment=LOG_LEVEL=INFO

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot
sudo journalctl -u trading-bot -f
```

Deployment checklist

☐ Real broker adapter wired and tested in paper mode first
☐ Secrets injected via env vars (never hardcoded)
☐ --live flag explicitly set (default is paper)
☐ /healthz returning {"ok": true} before routing traffic
☐ Log aggregation configured (stdout → platform logger)
☐ Alerting on shutdown callbacks and news suspensions
☐ Small-capital sanity run: verify dollar risk per trade before enabling

---

Roadmap

☐ Native MT5 / OANDA / Binance adapters
☐ Persistent trade ledger (SQLite → Postgres)
☐ Telegram / email alerts on opens, closes, and suspensions
☐ Walk-forward parameter sweeps with timemachine
☐ Multi-account / multi-broker routing
☐ Order-flow (tape) features for smarter liquidity-sweep scoring

---

Disclaimer

This project is for educational and research purposes only. It is not
financial advice. Automated trading carries substantial risk of loss,
including the loss of all deposited capital. Past performance — including
Monte Carlo results from synthetic or historical data — does not guarantee
future results. You are solely responsible for any deployment using real
funds, and for ensuring compliance with your broker's terms of service and
all applicable regulations in your jurisdiction.
