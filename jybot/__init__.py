"""
jybot -- Polymarket BTC 5-min UP/DOWN automated trading bot (self-contained engine).

This package is the deployable core of the bot. It is intentionally
dependency-light: the dry-run / simulation path runs on the Python standard
library alone, so ``python main.py --test-mode`` works on a clean machine
before ``pip install``. Real-money trading additionally requires
``py-clob-client`` (imported lazily, only when LIVE trading is engaged).

Public surface:
    from jybot.config import load_config
    from jybot.engine import TradingEngine
"""

__version__ = "2.0.0"
