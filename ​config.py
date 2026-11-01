"""
Central configuration for Web_Trading_v1.
Developed & Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)
"""

from datetime import time  # noqa: F401  (imported for convenience in type hints)

# =============================================================================
# APP IDENTITY
# =============================================================================
APP_NAME    = "Web Trading Bot"
APP_VERSION = "1.0.0"
AUTHOR      = "Engineer Abdulelah Sadiq"
AUTHOR_AR   = "المهندس عبد الإله صادق"

# =============================================================================
# TRADING STYLES & TIMEFRAMES
# =============================================================================
TRADING_STYLES = ["Swing", "Scalping"]

# Swing — higher timeframe directional plays
SWING_TIMEFRAMES    = ["1W", "1D", "4H", "1H"]

# Scalping — low-latency intraday plays
SCALPING_TIMEFRAMES     = ["1W", "1D", "1H", "5M"]
SCALPING_TIMEFRAMES_ALT = ["1W", "1D", "1H", "15M", "1M"]  # optional variant

# Minutes-per-bar mapping (used for lookback / expiry calculations)
TIMEFRAME_MINUTES = {
    "1M":  1,
    "5M":  5,
    "15M": 15,
    "30M": 30,
    "1H":  60,
    "4H":  240,
    "1D":  1440,
    "1W":  10080,
}

# Per-style timeframe stack used by signal engines
STYLE_TIMEFRAMES = {
    "Swing":    SWING_TIMEFRAMES,
    "Scalping": SCALPING_TIMEFRAMES,
}

# Timeframe used to enter the trade (LTF)
STYLE_ENTRY_TF = {
    "Swing":    "1H",
    "Scalping": "5M",
}

# Timeframe used for HTF bias (HTF)
STYLE_HTF_BIAS_TF = {
    "Swing":    "1D",
    "Scalping": "1H",
}

# =============================================================================
# SESSIONS (all times UTC, inclusive start / exclusive end)
# =============================================================================
SESSIONS = {
    "sydney": {
        "name":    "Sydney",
        "name_ar": "سيدني",
        "start":   "21:00",
        "end":     "06:00",   # crosses midnight
        "active":  True,
    },
    "tokyo": {
        "name":    "Tokyo",
        "name_ar": "طوكيو",
        "start":   "00:00",
        "end":     "09:00",
        "active":  True,
    },
    "london": {
        "name":    "London",
        "name_ar": "لندن",
        "start":   "07:00",
        "end":     "16:00",
        "active":  True,
    },
    "new_york": {
        "name":    "New York",
        "name_ar": "نيويورك",
        "start":   "12:00",
        "end":     "21:00",
        "active":  True,
    },
}

# =============================================================================
# ICT KILL ZONES (UTC) — highest-conviction windows
# =============================================================================
KILL_ZONES = {
    "asian_kz": {
        "name":     "Asian Kill Zone",
        "name_ar":  "منطقة قتل آسيا",
        "start":    "00:00",
        "end":      "03:00",
        "priority": "medium",
        "styles":   ["Swing"],
    },
    "london_kz": {
        "name":     "London Kill Zone",
        "name_ar":  "منطقة قتل لندن",
        "start":    "07:00",
        "end":      "10:00",
        "priority": "high",
        "styles":   ["Swing", "Scalping"],
    },
    "ny_am_kz": {
        "name":     "New York AM Kill Zone",
        "name_ar":  "منطقة قتل نيويورك الصباحية",
        "start":    "12:00",
        "end":      "15:00",
        "priority": "high",
        "styles":   ["Swing", "Scalping"],
    },
    "london_close_kz": {
        "name":     "London Close Kill Zone",
        "name_ar":  "منطقة قتل إغلاق لندن",
        "start":    "15:00",
        "end":      "17:00",
        "priority": "medium",
        "styles":   ["Swing"],
    },
}

# =============================================================================
# DEAD LIQUIDITY ZONES (UTC) — HARD BLOCK, no trades allowed
# =============================================================================
DEAD_LIQUIDITY_ZONES = {
    "asian_lunch": {
        "name":    "Asian Session Lunch",
        "name_ar": "استراحة الجلسة الآسيوية",
        "start":   "03:00",
        "end":     "05:00",
    },
    "london_lunch": {
        "name":    "London Mid-Session Lull",
        "name_ar": "هدوء منتصف لندن",
        "start":   "10:00",
        "end":     "11:00",
    },
    "ny_lunch": {
        "name":    "New York Lunch",
        "name_ar": "استراحة نيويورك",
        "start":   "16:00",
        "end":     "17:00",
    },
    "daily_rollover": {
        "name":    "Daily Rollover",
        "name_ar": "التحول اليومي",
        "start":   "20:55",
        "end":     "21:15",
    },
}

# =============================================================================
# MARKET TRANSITIONS (UTC) — HARD BLOCK, unstable price action
# =============================================================================
MARKET_TRANSITIONS = {
    "london_pre_open": {
        "name":    "London Pre-Open",
        "name_ar": "ما قبل افتتاح لندن",
        "start":   "06:45",
        "end":     "07:15",
    },
    "ny_pre_open": {
        "name":    "New York Pre-Open",
        "name_ar": "ما قبل افتتاح نيويورك",
        "start":   "11:45",
        "end":     "12:15",
    },
    "london_close": {
        "name":    "London Close Transition",
        "name_ar": "تحول إغلاق لندن",
        "start":   "15:45",
        "end":     "16:15",
    },
    "daily_rollover_transition": {
        "name":    "Rollover Transition",
        "name_ar": "تحول يومي",
        "start":   "20:50",
        "end":     "21:20",
    },
}

# =============================================================================
# WEEKEND RULES (UTC)
# =============================================================================
FOREX_CLOSE_FRIDAY_HOUR = 21   # Friday 21:00 UTC → market closes
FOREX_OPEN_SUNDAY_HOUR  = 21   # Sunday 21:00 UTC → market opens

# Instruments that close earlier on Fridays (UTC hour)
EARLY_CLOSE_INSTRUMENTS = {
    "XAU/USD": 20,
    "XAG/USD": 20,
    "US30":    20,
    "NAS100":  20,
}

# =============================================================================
# RISK PARAMETERS PER STYLE
# =============================================================================
RISK_PER_TRADE = {
    "Swing":    1.0,   # % of account equity
    "Scalping": 0.5,
}

MAX_OPEN_POSITIONS = {
    "Swing":    5,
    "Scalping": 3,
}

MIN_RR = {
    "Swing":    2.0,
    "Scalping": 1.5,
}

# =============================================================================
# TIMEZONE
# =============================================================================
TIMEZONE = "UTC"
