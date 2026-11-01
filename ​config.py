"""
config.py
Configuration for an automated trading bot.

Supported assets:
  - Gold (XAU/USD)
  - Silver (XAG/USD)
  - Crude Oil (WTI/USD)
  - EUR/USD
  - GBP/USD
  - USD/JPY

Default capital is $50, risk per trade is 1.5% of capital.
Timeframe presets are provided for scalping and swing trading.
Automation modes allow either a single concurrent trade or up to three.
"""

from typing import Dict, List, Any

# ---------------------------------------------------------------------
# Capital and risk management
# ---------------------------------------------------------------------
DEFAULT_CAPITAL: float = 50.0          # USD
RISK_PER_TRADE: float = 0.015          # 1.5% of capital
RISK_AMOUNT_PER_TRADE: float = DEFAULT_CAPITAL * RISK_PER_TRADE  # $0.75

# ---------------------------------------------------------------------
# Supported assets
# ---------------------------------------------------------------------
# pip_size and contract_size are included as common defaults.
# Adjust them to match your broker / instrument specifications.
ASSETS: List[Dict[str, Any]] = [
    {
        "symbol": "XAU/USD",
        "name": "Gold",
        "asset_class": "commodity",
        "pip_size": 0.01,
        "contract_size": 100,
    },
    {
        "symbol": "XAG/USD",
        "name": "Silver",
        "asset_class": "commodity",
        "pip_size": 0.001,
        "contract_size": 5000,
    },
    {
        "symbol": "WTI/USD",
        "name": "Crude Oil",
        "asset_class": "commodity",
        "pip_size": 0.01,
        "contract_size": 1000,
    },
    {
        "symbol": "EUR/USD",
        "name": "Euro / US Dollar",
        "asset_class": "forex",
        "pip_size": 0.0001,
        "contract_size": 100000,
    },
    {
        "symbol": "GBP/USD",
        "name": "British Pound / US Dollar",
        "asset_class": "forex",
        "pip_size": 0.0001,
        "contract_size": 100000,
    },
    {
        "symbol": "USD/JPY",
        "name": "US Dollar / Japanese Yen",
        "asset_class": "forex",
        "pip_size": 0.01,
        "contract_size": 100000,
    },
]

ASSET_SYMBOLS: List[str] = [asset["symbol"] for asset in ASSETS]

# ---------------------------------------------------------------------
# Timeframe configurations
# ---------------------------------------------------------------------
# Each preset defines entry, trend, and exit timeframes.
# stop_loss_pips / take_profit_pips are example values and should be
# tuned per asset and strategy.
TIMEFRAMES: Dict[str, Dict[str, Any]] = {
    "scalping": {
        "entry": "1m",
        "trend": "5m",
        "exit": "1m",
        "stop_loss_pips": 10,
        "take_profit_pips": 20,
    },
    "swing": {
        "entry": "1h",
        "trend": "4h",
        "exit": "1h",
        "stop_loss_pips": 50,
        "take_profit_pips": 150,
    },
}

DEFAULT_TIMEFRAME: str = "scalping"

# ---------------------------------------------------------------------
# Automation modes
# ---------------------------------------------------------------------
AUTOMATION_MODES: Dict[str, Dict[str, Any]] = {
    "single": {
        "max_concurrent_trades": 1,
        "description": "Only one trade at a time across all assets.",
    },
    "multi": {
        "max_concurrent_trades": 3,
        "description": "Up to three concurrent trades across all assets.",
    },
}

DEFAULT_AUTOMATION_MODE: str = "single"

# ---------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------
def get_max_concurrent_trades(mode: str = DEFAULT_AUTOMATION_MODE) -> int:
    """Return the maximum number of concurrent trades for a mode."""
    return AUTOMATION_MODES[mode]["max_concurrent_trades"]


def get_risk_amount(
    capital: float = DEFAULT_CAPITAL,
    risk_pct: float = RISK_PER_TRADE,
) -> float:
    """Return the absolute risk amount in USD for a single trade."""
    return capital * risk_pct
