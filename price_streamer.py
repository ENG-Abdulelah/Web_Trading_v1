# price_streamer.py
"""
Real-time price streamer via Yahoo Finance WebSocket (yfrlt).
Developed & Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)
"""
import logging
import threading
import time

import yfrlt

logger = logging.getLogger(__name__)

# ذاكرة مركزية تحتفظ بآخر سعر لكل زوج
PRICE_STORE = {}
_LAST_UPDATE = {}

# خريطة تحويل الرموز إلى صيغة Yahoo Finance
YF_SYMBOLS = {
    "XAU/USD": "GC=F",
    "XAG/USD": "SI=F",
    "WTI/USD": "CL=F",
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
    "DXY":    "DX-Y.NYB",
}

class YFPriceStreamer(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.running = True
        self.client = None

    def run(self):
        """تشغيل البث في Thread منفصل"""
        logger.info("Starting Yahoo Finance WebSocket streamer...")
        try:
            self.client = yfrlt.Client()
            # الاشتراك في جميع الرموز
            symbols = list(YF_SYMBOLS.values())
            self.client.subscribe(symbols, self._on_price_update)
            self.client.start()  # يعمل بشكل دائم
        except Exception as e:
            logger.error("Yahoo streamer crashed: %s", e)

    def _on_price_update(self, data):
        """استقبال تحديث السعر وتخزينه في الذاكرة"""
        try:
            yf_symbol = data.symbol
            price = data.price
            change_pct = data.change_percent

            # العثور على الزوج المقابل
            for our_sym, mapped_sym in YF_SYMBOLS.items():
                if mapped_sym == yf_symbol:
                    PRICE_STORE[our_sym] = {
                        "price": round(float(price), 5),
                        "change_pct": round(float(change_pct), 2),
                        "timestamp": time.time(),
                    }
                    _LAST_UPDATE[our_sym] = time.time()
                    break
        except Exception as e:
            logger.debug("Price parse error: %s", e)

    def stop(self):
        self.running = False
        if self.client:
            self.client.stop()


def start_price_streamer():
    """بدء البث في الخلفية"""
    streamer = YFPriceStreamer()
    streamer.start()
    return streamer
