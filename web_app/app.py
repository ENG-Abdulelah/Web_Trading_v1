"""
Web Trading App - Main Flask Application
Developed & Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)
"""

from flask import Flask, render_template
from datetime import datetime
import random

app = Flask(__name__)

# =============================================================================
# BADGE HELPERS
# =============================================================================

def tf_badge_class(tf):
    return {
        "1W": "bg-dark",
        "1D": "bg-secondary",
        "4H": "bg-primary",
        "1H": "bg-info text-dark",
        "5M": "bg-success",
    }.get(tf, "bg-secondary")


def style_badge_class(style):
    return {
        "Swing":    "bg-primary",
        "Scalping": "bg-warning text-dark",
    }.get(style, "bg-secondary")


# Register template helpers
app.jinja_env.filters["tf_badge"]     = tf_badge_class
app.jinja_env.filters["style_badge"]  = style_badge_class


# =============================================================================
# POSITION DATA  (replace these functions with your live broker/DB queries)
# =============================================================================

def get_live_positions():
    """Active positions that are currently open in the market."""
    return [
        {"id": "L-1001", "symbol": "EUR/USD", "direction": "BUY",
         "entry": 1.08720, "current": 1.09080, "sl": 1.08350, "tp": 1.09500,
         "style": "Swing", "timeframe": "4H", "pnl": 36.00,
         "opened": "2026-10-31 08:14", "strategy": "Silver Bullet"},
        {"id": "L-1002", "symbol": "GBP/JPY", "direction": "SELL",
         "entry": 189.420, "current": 189.115, "sl": 189.900, "tp": 188.200,
         "style": "Swing", "timeframe": "1D", "pnl": 30.50,
         "opened": "2026-10-30 14:02", "strategy": "Judas Swing"},
        {"id": "L-1003", "symbol": "XAU/USD", "direction": "BUY",
         "entry": 2032.10, "current": 2038.40, "sl": 2025.00, "tp": 2050.00,
         "style": "Scalping", "timeframe": "5M", "pnl": 63.00,
         "opened": "2026-11-01 09:35", "strategy": "FVG"},
        {"id": "L-1004", "symbol": "USD/JPY", "direction": "SELL",
         "entry": 151.240, "current": 151.090, "sl": 151.450, "tp": 150.500,
         "style": "Scalping", "timeframe": "1H", "pnl": 15.00,
         "opened": "2026-11-01 10:12", "strategy": "Kill Zones"},
        {"id": "L-1005", "symbol": "BTC/USD", "direction": "BUY",
         "entry": 68120.0, "current": 68540.0, "sl": 67600.0, "tp": 69500.0,
         "style": "Swing", "timeframe": "1W", "pnl": 420.00,
         "opened": "2026-10-28 00:00", "strategy": "Power of 3"},
    ]


def get_pending_orders():
    """Orders placed but not yet triggered."""
    return [
        {"id": "P-2001", "symbol": "AUD/USD", "direction": "BUY LIMIT",
         "entry": 0.65200, "current": 0.65480, "sl": 0.64800, "tp": 0.66200,
         "style": "Swing", "timeframe": "4H", "pnl": None,
         "opened": "2026-11-01 06:00", "strategy": "OTE"},
        {"id": "P-2002", "symbol": "USD/CAD", "direction": "SELL LIMIT",
         "entry": 1.37450, "current": 1.37120, "sl": 1.37950, "tp": 1.36500,
         "style": "Swing", "timeframe": "1D", "pnl": None,
         "opened": "2026-10-31 20:30", "strategy": "Breaker Block"},
        {"id": "P-2003", "symbol": "EUR/GBP", "direction": "BUY STOP",
         "entry": 0.86350, "current": 0.86210, "sl": 0.85950, "tp": 0.87000,
         "style": "Scalping", "timeframe": "5M", "pnl": None,
         "opened": "2026-11-01 10:45", "strategy": "Turtle Soup"},
        {"id": "P-2004", "symbol": "NAS100", "direction": "SELL LIMIT",
         "entry": 15420.0, "current": 15380.0, "sl": 15550.0, "tp": 15100.0,
         "style": "Scalping", "timeframe": "1H", "pnl": None,
         "opened": "2026-11-01 11:05", "strategy": "London Open Reversal"},
        {"id": "P-2005", "symbol": "XAG/USD", "direction": "BUY LIMIT",
         "entry": 22.850, "current": 22.940, "sl": 22.500, "tp": 23.800,
         "style": "Swing", "timeframe": "1W", "pnl": None,
         "opened": "2026-10-29 04:00", "strategy": "Order Block"},
    ]


def get_closed_positions():
    """Completed trades."""
    return [
        {"id": "C-3001", "symbol": "EUR/USD", "direction": "BUY",
         "entry": 1.08200, "current": 1.08650, "sl": 1.07900, "tp": 1.08700,
         "style": "Swing", "timeframe": "1H", "pnl": 45.00,
         "opened": "2026-10-29 09:00", "closed": "2026-10-30 15:22",
         "strategy": "Silver Bullet"},
        {"id": "C-3002", "symbol": "GBP/USD", "direction": "SELL",
         "entry": 1.21800, "current": 1.21450, "sl": 1.22100, "tp": 1.21400,
         "style": "Scalping", "timeframe": "5M", "pnl": 35.00,
         "opened": "2026-10-31 07:45", "closed": "2026-10-31 09:10",
         "strategy": "SMT Divergence"},
        {"id": "C-3003", "symbol": "XAU/USD", "direction": "BUY",
         "entry": 2040.00, "current": 2036.20, "sl": 2035.00, "tp": 2055.00,
         "style": "Swing", "timeframe": "4H", "pnl": -38.00,
         "opened": "2026-10-30 16:00", "closed": "2026-10-31 03:12",
         "strategy": "Order Block"},
        {"id": "C-3004", "symbol": "USD/JPY", "direction": "BUY",
         "entry": 150.900, "current": 151.420, "sl": 150.500, "tp": 151.500,
         "style": "Scalping", "timeframe": "1H", "pnl": 52.00,
         "opened": "2026-10-31 12:05", "closed": "2026-10-31 17:48",
         "strategy": "Kill Zones"},
        {"id": "C-3005", "symbol": "BTC/USD", "direction": "SELL",
         "entry": 69200.0, "current": 68900.0, "sl": 69600.0, "tp": 68800.0,
         "style": "Swing", "timeframe": "1D", "pnl": 300.00,
         "opened": "2026-10-27 03:00", "closed": "2026-10-28 11:30",
         "strategy": "Judas Swing"},
    ]


# =============================================================================
# 20 ICT STRATEGY RANKINGS
# =============================================================================

def get_strategy_rankings():
    raw = [
        {"name": "Silver Bullet",            "usage": 47, "success_rate": 68.5},
        {"name": "Judas Swing",              "usage": 39, "success_rate": 72.1},
        {"name": "Optimal Trade Entry (OTE)","usage": 33, "success_rate": 74.2},
        {"name": "Order Block (OB)",         "usage": 52, "success_rate": 70.3},
        {"name": "Fair Value Gap (FVG)",     "usage": 61, "success_rate": 66.9},
        {"name": "Breaker Block",            "usage": 22, "success_rate": 71.4},
        {"name": "Mitigation Block",         "usage": 18, "success_rate": 65.8},
        {"name": "Liquidity Sweep",          "usage": 41, "success_rate": 73.6},
        {"name": "Power of 3 (AMD)",         "usage": 29, "success_rate": 77.0},
        {"name": "ICT 2022 Model",           "usage": 24, "success_rate": 75.5},
        {"name": "Kill Zones",               "usage": 55, "success_rate": 69.8},
        {"name": "Market Structure Shift",   "usage": 36, "success_rate": 67.2},
        {"name": "Change of Character",      "usage": 31, "success_rate": 66.4},
        {"name": "Smart Money Reversal",     "usage": 19, "success_rate": 72.9},
        {"name": "Institutional Order Flow", "usage": 16, "success_rate": 78.3},
        {"name": "SMT Divergence",           "usage": 27, "success_rate": 71.0},
        {"name": "Turtle Soup",              "usage": 21, "success_rate": 64.7},
        {"name": "Stop Hunt",                "usage": 34, "success_rate": 68.1},
        {"name": "London Open Reversal",     "usage": 25, "success_rate": 70.6},
        {"name": "New York Open Reversal",   "usage": 23, "success_rate": 69.4},
    ]
    ranked = sorted(raw, key=lambda s: s["success_rate"], reverse=True)
    for i, s in enumerate(ranked, start=1):
        s["rank"] = i
    return ranked


# =============================================================================
# NEWS FEEDS
# =============================================================================

def get_news_feeds():
    return {
        "forexfactory": {
            "title": "ForexFactory",
            "title_ar": "فوركس فاكتوري",
            "icon": "bi-globe2",
            "color": "primary",
            "items": [
                {"time": "10:30", "impact": "high",   "event": "US Non-Farm Payrolls", "forecast": "180K", "previous": "150K"},
                {"time": "12:00", "impact": "medium", "event": "GBP BoE Rate Decision", "forecast": "5.00%", "previous": "5.00%"},
                {"time": "14:15", "impact": "high",   "event": "EUR ECB Press Conference", "forecast": "—", "previous": "—"},
            ],
        },
        "investing": {
            "title": "Investing.com",
            "title_ar": "إنفستنغ",
            "icon": "bi-graph-up-arrow",
            "color": "success",
            "items": [
                {"time": "09:00", "impact": "low",    "event": "JPY Household Spending", "forecast": "0.6%",  "previous": "0.4%"},
                {"time": "13:45", "impact": "medium", "event": "USD ISM Manufacturing",  "forecast": "48.5",  "previous": "47.9"},
                {"time": "15:30", "impact": "high",   "event": "Crude Oil Inventories",  "forecast": "-1.2M", "previous": "-0.8M"},
            ],
        },
        "fxdaily": {
            "title": "FXDaily",
            "title_ar": "فوركس ديلي",
            "icon": "bi-currency-exchange",
            "color": "info",
            "items": [
                {"time": "07:00", "impact": "medium", "event": "AUD Retail Sales MoM", "forecast": "0.3%", "previous": "0.2%"},
                {"time": "11:00", "impact": "low",    "event": "CAD Building Permits", "forecast": "1.1%", "previous": "0.9%"},
                {"time": "16:00", "impact": "high",   "event": "Fed Chair Speech",     "forecast": "—",    "previous": "—"},
            ],
        },
        "cpi": {
            "title": "CPI Inflation Data",
            "title_ar": "بيانات التضخم CPI",
            "icon": "bi-box-seam",
            "color": "warning",
            "items": [
                {"time": "—", "impact": "high", "event": "US CPI YoY",   "forecast": "3.2%", "previous": "3.4%"},
                {"time": "—", "impact": "high", "event": "EU CPI YoY",   "forecast": "2.6%", "previous": "2.9%"},
                {"time": "—", "impact": "high", "event": "UK CPI YoY",   "forecast": "3.8%", "previous": "4.0%"},
            ],
        },
        "cot": {
            "title": "COT Report",
            "title_ar": "تقرير COT",
            "icon": "bi-clipboard-data",
            "color": "danger",
            "items": [
                {"time": "Fri", "impact": "medium", "event": "EUR Net Long",  "forecast": "—", "previous": "+82.3K"},
                {"time": "Fri", "impact": "medium", "event": "JPY Net Short", "forecast": "—", "previous": "-110.4K"},
                {"time": "Fri", "impact": "medium", "event": "GBP Net Long",  "forecast": "—", "previous": "+28.6K"},
            ],
        },
        "cftc": {
            "title": "CFTC Positioning",
            "title_ar": "بيانات CFTC",
            "icon": "bi-bank",
            "color": "secondary",
            "items": [
                {"time": "Fri", "impact": "medium", "event": "Speculative Net Long — Gold", "forecast": "—", "previous": "187.5K"},
                {"time": "Fri", "impact": "medium", "event": "Speculative Net Long — S&P",  "forecast": "—", "previous": "42.1K"},
                {"time": "Fri", "impact": "medium", "event": "Speculative Net Long — Oil",  "forecast": "—", "previous": "246.8K"},
            ],
        },
    }


# =============================================================================
# ROUTES
# =============================================================================

@app.route("/")
def index():
    live        = get_live_positions()
    pending     = get_pending_orders()
    closed      = get_closed_positions()
    strategies  = get_strategy_rankings()
    news        = get_news_feeds()

    total_pnl   = sum(p["pnl"] for p in live if p.get("pnl") is not None)
    closed_pnl  = sum(p["pnl"] for p in closed if p.get("pnl") is not None)
    wins        = sum(1 for p in closed if p.get("pnl", 0) > 0)
    win_rate    = (wins / len(closed) * 100) if closed else 0

    return render_template(
        "index.html",
        live_positions=live,
        pending_orders=pending,
        closed_positions=closed,
        strategies=strategies,
        news=news,
        stats={
            "live_count":     len(live),
            "pending_count":  len(pending),
            "closed_count":   len(closed),
            "total_pnl":      round(total_pnl, 2),
            "closed_pnl":     round(closed_pnl, 2),
            "win_rate":       round(win_rate, 1),
        },
        now=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
