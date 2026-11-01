# run.py
"""
Clean entry point for Web Trading Analyst.

Usage:
    python run.py                    # run on default host 0.0.0.0:5000
    python run.py --host 127.0.0.1 --port 8000 --debug

Developed & Engineered by Engineer Abdulelah Sadiq (المهندس عبد الإله صادق)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

BANNER = r"""
╔══════════════════════════════════════════════════════════════════╗
║                                                                  ║
║   Web Trading Analyst — Personal Analysis Platform  v1.0        ║
║                                                                  ║
║   Developed & Engineered by Engineer Abdulelah Sadiq            ║
║                     ( المهندس عبد الإله صادق )                  ║
║                                                                  ║
║   Modules: Real Feed · ICT×20 · Risk Gate · News Guard          ║
║   No random trading · No mock data · User-approved execution    ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Web Trading Analyst — Developed by Engineer Abdulelah Sadiq"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=5000, type=int)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    # Ensure data dir exists
    Path("data").mkdir(parents=True, exist_ok=True)
    Path("logs").mkdir(parents=True, exist_ok=True)

    print(BANNER)

    # Import AFTER banner so first-time errors show cleanly
    from app import app, start_scheduler, GUARD, logger

    st = GUARD.state()
    logger.info("=" * 60)
    logger.info("Market open=%s | Sessions=%s | KillZones=%s",
                st.is_open,
                [s["name"] for s in st.active_sessions],
                [k["name"] for k in st.active_kill_zones])
    if not st.is_open:
        logger.warning("Market is closed: %s", st.blocked_reason)
    logger.info("=" * 60)

    # Start background analysis scheduler
    start_scheduler()

    logger.info("Dashboard: http://%s:%d/",
                "127.0.0.1" if args.host == "0.0.0.0" else args.host,
                args.port)

    try:
        app.run(host=args.host, port=args.port,
                debug=args.debug, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
