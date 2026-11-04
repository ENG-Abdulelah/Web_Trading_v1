# price_feed.py
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
HTTP_TIMEOUT = 8  # تقليل المهلة لتجنب تعليق الخادم
NEWS_BLACKOUT_BEFORE_MIN = 5
NEWS_BLACKOUT_AFTER_MIN = 10

SYMBOLS = {
    "XAU/USD": {"yahoo": "GC=F", "spot_code": "XAU", "metals_spot": True, "decimals": 2},
    "XAG/USD": {"yahoo": "SI=F", "spot_code": "XAG", "metals_spot": True, "decimals": 3},
    "WTI/USD": {"yahoo": "CL=F", "spot_code": None, "metals_spot": False, "decimals": 2},
    "EUR/USD": {"yahoo": "EURUSD=X", "spot_code": None, "metals_spot": False, "decimals": 5},
    "GBP/USD": {"yahoo": "GBPUSD=X", "spot_code": None, "metals_spot": False, "decimals": 5},
    "USD/JPY": {"yahoo": "USDJPY=X", "spot_code": None, "metals_spot": False, "decimals": 3},
    "DXY":     {"yahoo": "DX-Y.NYB", "spot_code": None, "metals_spot": False, "decimals": 3},
}

PRICE_BOUNDS = {
    "XAU/USD": (1000.0, 5000.0), "XAG/USD": (10.0, 100.0),
    "WTI/USD": (20.0, 200.0), "EUR/USD": (0.5, 2.0),
    "GBP/USD": (0.5, 2.5), "USD/JPY": (80.0, 250.0), "DXY": (70.0, 150.0)
}

PRICE_LOCK = threading.RLock()
PRICES = {sym: {"price": 0.0, "prev_price": 0.0, "prev_close": 0.0, "high": 0.0, "low": 0.0, "history": [], "last_update": "", "source": "init", "stale": True} for sym in SYMBOLS}
LAST_FETCH = {}
PRICE_POOL = ThreadPoolExecutor(max_workers=7)
SHUTDOWN = threading.Event()

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def should_fetch(sym):
    entry = LAST_FETCH.get(sym)
    if not entry: return True
    return (time.time() - entry["time"]) >= entry["backoff"]

def record_fetch(sym, success):
    LAST_FETCH[sym] = {"time": time.time(), "backoff": 1.0 if success else 30.0} # زيادة وقت الفشل

def fetch_metals_spot(spot_code):
    url = f"https://api.gold-api.com/price/{spot_code}"
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    price = payload.get("price")
    if price is None: raise ValueError("no price")
    return float(price), None, None, None

def fetch_yahoo_quote(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d"
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8")
    payload = json.loads(raw)
    result = payload["chart"]["result"][0]
    meta = result.get("meta", {})
    price = meta.get("regularMarketPrice")
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    high = meta.get("regularMarketDayHigh")
    low = meta.get("regularMarketDayLow")
    if price is None: raise ValueError("no price")
    return (float(price), float(prev) if prev else None, float(high) if high else None, float(low) if low else None)

def apply_tick(sym, price, prev_close, day_high, day_low, source):
    try:
        lo, hi = PRICE_BOUNDS.get(sym, (0.000001, 1e12))
        if not (lo <= price <= hi): return False
        
        with PRICE_LOCK:
            st = PRICES[sym]
            st["prev_price"] = st["price"]
            st["price"] = float(price)
            if prev_close is not None: st["prev_close"] = float(prev_close)
            
            # حماية من القيم الفارغة
            if day_high is not None: st["high"] = max(st["high"], float(day_high))
            if day_low is not None: 
                st["low"] = min(st["low"], float(day_low)) if st["low"] != 0 else float(day_low)
            
            st["high"] = max(st["high"], st["price"])
            st["low"] = min(st["low"], st["price"]) if st["low"] != 0 else st["price"]
            st["last_update"] = now_iso()
            st["source"] = source
            st["stale"] = False
            st["history"].append(st["price"])
            if len(st["history"]) > 120: st["history"].pop(0)
        return True
    except Exception as e:
        print(f"[price] apply_tick error for {sym}: {e}", flush=True)
        return False

def fetch_one_safe(sym):
    if not should_fetch(sym): return
    cfg = SYMBOLS[sym]
    try:
        if cfg["metals_spot"]:
            try:
                price, _, _, _ = fetch_metals_spot(cfg["spot_code"])
                if apply_tick(sym, price, None, None, None, "gold-api-spot"):
                    record_fetch(sym, True)
                    return
            except Exception:
                pass # تجاهل الخطأ بصمت وانتقل لياهو

        price, prev_close, hi, lo = fetch_yahoo_quote(cfg["yahoo"])
        if apply_tick(sym, price, prev_close, hi, lo, "yahoo"):
            record_fetch(sym, True)
        else:
            record_fetch(sym, False)
    except Exception as e:
        record_fetch(sym, False)

def _price_feed_loop():
    while not SHUTDOWN.is_set():
        try:
            for sym in SYMBOLS.keys():
                PRICE_POOL.submit(fetch_one_safe, sym)
        except Exception:
            pass
        SHUTDOWN.wait(2.0) # تحديث كل ثانيتين

def start_price_feed():
    thread = threading.Thread(target=_price_feed_loop, daemon=True)
    thread.start()
    return thread

# --- News Calendar ---
CALENDAR_SEED = [
    {"month": 1, "day": 28, "event_en": "FOMC Rate Decision", "event_ar": "قرار الفائدة الفيدرالي", "currency": "USD", "impact": "HIGH", "forecast": "3.75%", "previous": "4.00%"},
    {"month": 3, "day": 18, "event_en": "FOMC Rate Decision", "event_ar": "قرار الفائدة الفيدرالي", "currency": "USD", "impact": "HIGH", "forecast": "3.75%", "previous": "3.75%"},
    {"month": 4, "day": 29, "event_en": "FOMC Rate Decision", "event_ar": "قرار الفائدة الفيدرالي", "currency": "USD", "impact": "HIGH", "forecast": "3.50%", "previous": "3.75%"},
    {"month": 6, "day": 17, "event_en": "FOMC Rate Decision", "event_ar": "قرار الفائدة الفيدرالي", "currency": "USD", "impact": "HIGH", "forecast": "3.50%", "previous": "3.50%"},
    {"month": 9, "day": 16, "event_en": "FOMC Rate Decision", "event_ar": "قرار الفائدة الفيدرالي", "currency": "USD", "impact": "HIGH", "forecast": "3.25%", "previous": "3.50%"},
    {"month": 12, "day": 16, "event_en": "FOMC Rate Decision", "event_ar": "قرار الفائدة الفيدرالي", "currency": "USD", "impact": "HIGH", "forecast": "3.25%", "previous": "3.25%"},
    {"month": 1, "day": 2, "event_en": "US Non-Farm Payrolls", "event_ar": "التغير في الوظائف غير الزراعية", "currency": "USD", "impact": "HIGH", "forecast": "175K", "previous": "160K"},
    {"month": 2, "day": 6, "event_en": "US Non-Farm Payrolls", "event_ar": "التغير في الوظائف غير الزراعية", "currency": "USD", "impact": "HIGH", "forecast": "180K", "previous": "175K"},
    {"month": 3, "day": 6, "event_en": "US Non-Farm Payrolls", "event_ar": "التغير في الوظائف غير الزراعية", "currency": "USD", "impact": "HIGH", "forecast": "185K", "previous": "180K"},
    {"month": 4, "day": 3, "event_en": "US Non-Farm Payrolls", "event_ar": "التغير في الوظائف غير الزراعية", "currency": "USD", "impact": "HIGH", "forecast": "190K", "previous": "185K"},
    {"month": 5, "day": 1, "event_en": "US Non-Farm Payrolls", "event_ar": "التغير في الوظائف غير الزراعية", "currency": "USD", "impact": "HIGH", "forecast": "185K", "previous": "190K"},
    {"month": 6, "day": 5, "event_en": "US Non-Farm Payrolls", "event_ar": "التغير في الوظائف غير الزراعية", "currency": "USD", "impact": "HIGH", "forecast": "180K", "previous": "185K"},
    {"month": 7, "day": 3, "event_en": "US Non-Farm Payrolls", "event_ar": "التغير في الوظائف غير الزراعية", "currency": "USD", "impact": "HIGH", "forecast": "175K", "previous": "180K"},
    {"month": 8, "day": 7, "event_en": "US Non-Farm Payrolls", "event_ar": "التغير في الوظائف غير الزراعية", "currency": "USD", "impact": "HIGH", "forecast": "180K", "previous": "175K"},
    {"month": 9, "day": 4, "event_en": "US Non-Farm Payrolls", "event_ar": "التغير في الوظائف غير الزراعية", "currency": "USD", "impact": "HIGH", "forecast": "185K", "previous": "180K"},
    {"month": 10, "day": 2, "event_en": "US Non-Farm Payrolls", "event_ar": "التغير في الوظائف غير الزراعية", "currency": "USD", "impact": "HIGH", "forecast": "190K", "previous": "185K"},
    {"month": 11, "day": 6, "event_en": "US Non-Farm Payrolls", "event_ar": "التغير في الوظائف غير الزراعية", "currency": "USD", "impact": "HIGH", "forecast": "185K", "previous": "190K"},
    {"month": 12, "day": 4, "event_en": "US Non-Farm Payrolls", "event_ar": "التغير في الوظائف غير الزراعية", "currency": "USD", "impact": "HIGH", "forecast": "180K", "previous": "185K"},
    {"month": 1, "day": 13, "event_en": "US CPI YoY", "event_ar": "مؤشر أسعار المستهلك السنوي", "currency": "USD", "impact": "HIGH", "forecast": "3.1%", "previous": "3.3%"},
    {"month": 2, "day": 11, "event_en": "US CPI YoY", "event_ar": "مؤشر أسعار المستهلك السنوي", "currency": "USD", "impact": "HIGH", "forecast": "3.0%", "previous": "3.1%"},
    {"month": 3, "day": 11, "event_en": "US CPI YoY", "event_ar": "مؤشر أسعار المستهلك السنوي", "currency": "USD", "impact": "HIGH", "forecast": "2.9%", "previous": "3.0%"},
    {"month": 4, "day": 14, "event_en": "US CPI YoY", "event_ar": "مؤشر أسعار المستهلك السنوي", "currency": "USD", "impact": "HIGH", "forecast": "2.8%", "previous": "2.9%"},
    {"month": 5, "day": 12, "event_en": "US CPI YoY", "event_ar": "مؤشر أسعار المستهلك السنوي", "currency": "USD", "impact": "HIGH", "forecast": "2.7%", "previous": "2.8%"},
    {"month": 6, "day": 10, "event_en": "US CPI YoY", "event_ar": "مؤشر أسعار المستهلك السنوي", "currency": "USD", "impact": "HIGH", "forecast": "2.6%", "previous": "2.7%"},
    {"month": 7, "day": 14, "event_en": "US CPI YoY", "event_ar": "مؤشر أسعار المستهلك السنوي", "currency": "USD", "impact": "HIGH", "forecast": "2.5%", "previous": "2.6%"},
    {"month": 8, "day": 12, "event_en": "US CPI YoY", "event_ar": "مؤشر أسعار المستهلك السنوي", "currency": "USD", "impact": "HIGH", "forecast": "2.4%", "previous": "2.5%"},
    {"month": 9, "day": 10, "event_en": "US CPI YoY", "event_ar": "مؤشر أسعار المستهلك السنوي", "currency": "USD", "impact": "HIGH", "forecast": "2.4%", "previous": "2.4%"},
    {"month": 10, "day": 13, "event_en": "US CPI YoY", "event_ar": "مؤشر أسعار المستهلك السنوي", "currency": "USD", "impact": "HIGH", "forecast": "2.3%", "previous": "2.4%"},
    {"month": 1, "day": 30, "event_en": "ECB Press Conference", "event_ar": "المؤتمر الصحفي للبنك المركزي الأوروبي", "currency": "EUR", "impact": "MEDIUM", "forecast": "Hold", "previous": "Hold"},
    {"month": 3, "day": 12, "event_en": "ECB Press Conference", "event_ar": "المؤتمر الصحفي للبنك المركزي الأوروبي", "currency": "EUR", "impact": "MEDIUM", "forecast": "Cut", "previous": "Hold"},
    {"month": 2, "day": 5, "event_en": "BoE Bank Rate", "event_ar": "قرار الفائدة لبنك إنجلترا", "currency": "GBP", "impact": "MEDIUM", "forecast": "4.50%", "previous": "4.75%"},
    {"month": 5, "day": 7, "event_en": "BoE Bank Rate", "event_ar": "قرار الفائدة لبنك إنجلترا", "currency": "GBP", "impact": "MEDIUM", "forecast": "4.25%", "previous": "4.50%"},
    {"month": 1, "day": 23, "event_en": "BoJ Policy Statement", "event_ar": "بيان سياسة بنك اليابان", "currency": "JPY", "impact": "HIGH", "forecast": "Hold", "previous": "Hold"},
    {"month": 4, "day": 30, "event_en": "BoJ Policy Statement", "event_ar": "بيان سياسة بنك اليابان", "currency": "JPY", "impact": "HIGH", "forecast": "Hike", "previous": "Hold"},
    {"month": 7, "day": 30, "event_en": "BoJ Policy Statement", "event_ar": "بيان سياسة بنك اليابان", "currency": "JPY", "impact": "HIGH", "forecast": "Hold", "previous": "Hike"},
    {"month": 1, "day": 15, "event_en": "US Retail Sales MoM", "event_ar": "مبيعات التجزئة الأمريكية", "currency": "USD", "impact": "MEDIUM", "forecast": "0.4%", "previous": "0.3%"},
    {"month": 4, "day": 15, "event_en": "US Retail Sales MoM", "event_ar": "مبيعات التجزئة الأمريكية", "currency": "USD", "impact": "MEDIUM", "forecast": "0.3%", "previous": "0.4%"},
    {"month": 2, "day": 18, "event_en": "German ZEW Sentiment", "event_ar": "مؤشر ZEW الألماني", "currency": "EUR", "impact": "LOW", "forecast": "13.0", "previous": "12.0"},
    {"month": 5, "day": 19, "event_en": "German ZEW Sentiment", "event_ar": "مؤشر ZEW الألماني", "currency": "EUR", "impact": "LOW", "forecast": "14.0", "previous": "13.0"},
]

EVENT_TIME_MAP = {
    "FOMC Rate Decision": (18, 0), "US Non-Farm Payrolls": (12, 30),
    "US CPI YoY": (12, 30), "ECB Press Conference": (12, 45),
    "BoE Bank Rate": (11, 0), "BoJ Policy Statement": (3, 0),
    "US Retail Sales MoM": (12, 30), "German ZEW Sentiment": (9, 0),
}

def build_calendar():
    now = datetime.now(timezone.utc)
    out = []
    for item in CALENDAR_SEED:
        h, m = EVENT_TIME_MAP.get(item["event_en"], (12, 0))
        try:
            candidate = datetime(now.year, item["month"], item["day"], h, m, 0, tzinfo=timezone.utc)
        except ValueError:
            continue
        if candidate < now - timedelta(days=1):
            try:
                candidate = datetime(now.year + 1, item["month"], item["day"], h, m, 0, tzinfo=timezone.utc)
            except ValueError:
                continue
        out.append({
            "event_en": item["event_en"], "event_ar": item["event_ar"],
            "currency": item["currency"], "impact": item["impact"],
            "forecast": item["forecast"], "previous": item["previous"],
            "when": candidate.isoformat(),
            "when_str": candidate.strftime("%Y-%m-%d %H:%M UTC"),
            "in_days": (candidate - now).days,
        })
    out.sort(key=lambda e: e["when"])
    return out[:14]

def get_news_blackout() -> dict:
    now = datetime.now(timezone.utc)
    try:
        events = build_calendar()
    except Exception:
        return {"active": False}
    for e in events:
        if e.get("impact") != "HIGH": continue
        try:
            when = datetime.fromisoformat(e["when"])
        except Exception:
            continue
        blackout_start = when - timedelta(minutes=NEWS_BLACKOUT_BEFORE_MIN)
        blackout_end   = when + timedelta(minutes=NEWS_BLACKOUT_AFTER_MIN)
        if blackout_start <= now <= blackout_end:
            return {
                "active": True, "event_en": e.get("event_en", ""), "event_ar": e.get("event_ar", ""),
                "currency": e.get("currency", ""), "impact": e.get("impact", "HIGH"),
                "when": e.get("when"), "blackout_start": blackout_start.isoformat(),
                "blackout_end": blackout_end.isoformat(),
                "resume_in_sec": max(0, int((blackout_end - now).total_seconds())),
            }
    return {"active": False}
