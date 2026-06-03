"""
main.py -- entry point for the Polymarket BTC 5-min UP/DOWN trading bot.

Usage
-----
    python main.py --test-mode     # bounded offline-friendly paper demo (3 iters)
    python main.py --simulation    # paper trading, real market clock (default)
    python main.py --live          # REAL MONEY -- requires the live gate (below)

Live-trading gate (three independent locks, ALL required)
---------------------------------------------------------
    1. launched with  --live
    2. .env has        DRY_RUN=false
    3. .env has        LIVE_TRADING=true

If any lock is missing the bot refuses to place real orders. ``--test-mode`` and
``--simulation`` always force paper trading regardless of .env.

You only ever edit ``.env`` -- never this file.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from jybot.config import load_config, describe
from jybot.log import logger, configure as configure_logging
from jybot.engine import TradingEngine


def _banner(mode: str, interval: str) -> None:
    line = "=" * 72
    print(line)
    print(f"  POLYMARKET BTC {interval.upper()} UP/DOWN BOT")
    print(f"  Mode: {mode}")
    print(line)


def build_engine(args) -> TradingEngine:
    test_mode = bool(args.test_mode)
    cfg = load_config(test_mode=test_mode)

    if args.interval:
        from jybot.config import _normalize_interval  # type: ignore
        label, secs = _normalize_interval(args.interval)
        cfg.market_interval, cfg.interval_seconds = label, secs

    # Mode resolution + safety gating.
    if args.live:
        if not cfg.live_trading or cfg.dry_run:
            logger.error("=" * 72)
            logger.error("REFUSING TO START LIVE.")
            logger.error("  --live requires BOTH of these in your .env:")
            logger.error(f"    DRY_RUN=false        (currently DRY_RUN={cfg.dry_run})")
            logger.error(f"    LIVE_TRADING=true    (currently LIVE_TRADING={cfg.live_trading})")
            logger.error("  This is the safety lock. Edit .env, then re-run --live.")
            logger.error("=" * 72)
            sys.exit(2)
        missing = cfg.credential_status()
        if missing:
            logger.error("Cannot trade live -- missing/placeholder credentials in .env:")
            for name in missing:
                logger.error(f"    {name}")
            logger.error("Run: python scripts/check_config.py")
            sys.exit(2)
        mode = "LIVE (REAL MONEY)"
    elif args.test_mode:
        # force paper, bounded demo
        cfg.dry_run = True
        cfg.live_trading = False
        mode = "TEST (paper, bounded)"
    else:
        # simulation (default): force paper, real clock
        cfg.dry_run = True
        cfg.live_trading = False
        mode = "SIMULATION (paper)"

    _banner(mode, cfg.market_interval)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    configure_logging(
        level="DEBUG" if args.verbose else "INFO",
        to_file=f"bot_{ts}.log",
    )

    if args.live:
        logger.warning("LIVE TRADING ENABLED -- real orders will be placed on Polymarket.")
        try:
            input("Press ENTER to continue, or Ctrl+C to abort... ")
        except KeyboardInterrupt:
            print("\nAborted.")
            sys.exit(0)

    return TradingEngine(cfg)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Polymarket BTC 5-min UP/DOWN trading bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--test-mode", action="store_true",
                       help="Bounded paper demo (works offline, 3 iterations)")
    group.add_argument("--simulation", action="store_true",
                       help="Paper trading on the real market clock (default)")
    group.add_argument("--live", action="store_true",
                       help="LIVE real-money trading (requires .env safety gate)")
    parser.add_argument("--interval", choices=["5m", "15m"], default=None,
                        help="Override MARKET_INTERVAL for this run")
    parser.add_argument("--verbose", action="store_true", help="DEBUG logging")

    args = parser.parse_args()

    engine = build_engine(args)
    try:
        engine.run()
    except KeyboardInterrupt:
        logger.info("shutting down")


if __name__ == "__main__":
    main()
