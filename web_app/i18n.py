# web_app/i18n.py
"""
web_app/i18n.py

English / Arabic localization for the trading bot dashboard.

Exposes:
    SUPPORTED_LANGS, DEFAULT_LANG
    get_lang(request)              -> "en" | "ar"
    is_rtl(lang)                   -> bool
    get_translations(lang)         -> dict[str, str]
    t(key, lang, **kwargs)         -> formatted string
"""

from __future__ import annotations

from typing import Dict

SUPPORTED_LANGS = ("en", "ar")
DEFAULT_LANG = "en"

LANG_NAMES = {"en": "English", "ar": "العربية"}


# ---------------------------------------------------------------------
# Translations
# ---------------------------------------------------------------------
TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "en": {
        # Header
        "app_title": "Trading Bot Dashboard",
        "app_subtitle": "Live sessions · open positions · alignment",
        "language": "Language",
        "live": "LIVE",
        "demo": "DEMO",
        "connected": "Connected",
        "disconnected": "Disconnected",
        "market_open": "Market Open",
        "market_closed": "Market Closed",
        "last_updated": "Last updated",

        # Capital / config bar
        "capital": "Capital",
        "risk_per_trade": "Risk / trade",
        "max_concurrent": "Max concurrent",
        "mode": "Mode",
        "mode_single": "Single",
        "mode_multi": "Multi",

        # Sessions
        "sessions": "Active Sessions",
        "session_asian": "Asian",
        "session_london": "London",
        "session_new_york": "New York",
        "active": "Active",
        "inactive": "Inactive",
        "next_open_in": "Next open in",
        "closes_in": "Closes in",
        "utc": "UTC",

        # Positions
        "positions": "Open Positions",
        "no_positions": "No open positions",
        "no_positions_hint": "The bot will open a trade when an approved signal appears.",
        "symbol": "Symbol",
        "side": "Side",
        "long": "Long",
        "short": "Short",
        "lots": "Lots",
        "entry": "Entry",
        "stop_loss": "SL",
        "take_profit": "TP",
        "rr": "R:R",
        "risk": "Risk",
        "pnl": "P&L",
        "strategy": "Strategy",
        "opened_at": "Opened",
        "suspended": "Trading Suspended",
        "suspended_reason": "High-impact news window active — resuming in",

        # Assets
        "assets": "Monitored Assets",
        "price": "Price",
        "change": "Change",
        "no_data": "—",

        # Scores
        "scores": "Alignment Scores",
        "score_total": "Total",
        "score_technical": "Technical",
        "score_macro": "Macro",
        "score_geo": "Geopolitical",
        "approved": "Approved",
        "rejected": "Rejected",
        "min_score": "Min score",
        "threshold": "Threshold",

        # Footer
        "footer_note": "Auto-refreshes every second · All times UTC · $50 capital, 1.5% risk per trade",
    },
    "ar": {
        # Header
        "app_title": "لوحة تحكم روبوت التداول",
        "app_subtitle": "الجلسات المباشرة · المراكز المفتوحة · التوافق",
        "language": "اللغة",
        "live": "مباشر",
        "demo": "تجريبي",
        "connected": "متصل",
        "disconnected": "غير متصل",
        "market_open": "السوق مفتوح",
        "market_closed": "السوق مغلق",
        "last_updated": "آخر تحديث",

        # Capital / config bar
        "capital": "رأس المال",
        "risk_per_trade": "المخاطرة / صفقة",
        "max_concurrent": "أقصى عدد مراكز",
        "mode": "الوضع",
        "mode_single": "فردي",
        "mode_multi": "متعدد",

        # Sessions
        "sessions": "الجلسات النشطة",
        "session_asian": "الآسيوية",
        "session_london": "اللندنية",
        "session_new_york": "النيويوركية",
        "active": "نشطة",
        "inactive": "غير نشطة",
        "next_open_in": "تفتح خلال",
        "closes_in": "تغلق خلال",
        "utc": "UTC",

        # Positions
        "positions": "المراكز المفتوحة",
        "no_positions": "لا توجد مراكز مفتوحة",
        "no_positions_hint": "سيفتح الروبوت صفقة عند ظهور إشارة معتمدة.",
        "symbol": "الرمز",
        "side": "الاتجاه",
        "long": "شراء",
        "short": "بيع",
        "lots": "اللوتات",
        "entry": "الدخول",
        "stop_loss": "وقف الخسارة",
        "take_profit": "جني الأرباح",
        "rr": "العائد/المخاطرة",
        "risk": "المخاطرة",
        "pnl": "الربح/الخسارة",
        "strategy": "الاستراتيجية",
        "opened_at": "فُتح",
        "suspended": "التداول معلق",
        "suspended_reason": "نافذة أخبار عالية التأثير نشطة — سيستأنف خلال",

        # Assets
        "assets": "الأصول المراقبة",
        "price": "السعر",
        "change": "التغير",
        "no_data": "—",

        # Scores
        "scores": "درجات التوافق",
        "score_total": "الإجمالي",
        "score_technical": "فني",
        "score_macro": "اقتصادي",
        "score_geo": "جيوسياسي",
        "approved": "معتمد",
        "rejected": "مرفوض",
        "min_score": "أدنى درجة",
        "threshold": "الحد الأدنى",

        # Footer
        "footer_note": "يُحدَّث تلقائياً كل ثانية · جميع الأوقات بتوقيت UTC · رأس مال 50$ ومخاطرة 1.5% لكل صفقة",
    },
}


# ---------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------
def is_rtl(lang: str) -> bool:
    return lang == "ar"


def get_lang(request) -> str:
    """Resolve language from: query param ?lang=xx → cookie → Accept-Language → default."""
    try:
        q = request.query_params.get("lang")
        if q in SUPPORTED_LANGS:
            return q
    except AttributeError:
        pass
    try:
        cookie = request.cookies.get("lang")
        if cookie in SUPPORTED_LANGS:
            return cookie
    except AttributeError:
        pass
    try:
        accept = request.headers.get("accept-language", "")
        for part in accept.split(","):
            code = part.split(";")[0].strip().lower()[:2]
            if code in SUPPORTED_LANGS:
                return code
    except AttributeError:
        pass
    return DEFAULT_LANG


def get_translations(lang: str) -> Dict[str, str]:
    if lang not in TRANSLATIONS:
        lang = DEFAULT_LANG
    return TRANSLATIONS[lang]


def t(key: str, lang: str, **kwargs) -> str:
    value = TRANSLATIONS.get(lang, TRANSLATIONS[DEFAULT_LANG]).get(key, key)
    if kwargs:
        try:
            return value.format(**kwargs)
        except (KeyError, IndexError):
            return value
    return value
