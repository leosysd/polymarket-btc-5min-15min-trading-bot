"""
jybot.config -- the single source of truth for ALL runtime parameters.

Everything is read from environment variables (typically loaded from ``.env``).
After deployment you only ever edit ``.env`` -- never the code. The exact
variable names below match ``.env.example``.

Safety model
------------
Two independent switches must BOTH be set for real money to move:

    DRY_RUN=false           # turn off paper trading
    LIVE_TRADING=true       # explicitly opt in to live orders

...and the bot must be launched with ``--live``. If any of those three is not
satisfied, the bot runs in paper mode and never signs a real order. This is the
"切换 LIVE_TRADING=true 后才允许实盘" guarantee.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from jybot.envload import load_dotenv
from jybot.log import logger

# Load .env exactly once on import (does not override real env vars).
load_dotenv()


# ── typed getters ──────────────────────────────────────────────────────────

def _get(name: str, default: Optional[str] = None, *aliases: str) -> Optional[str]:
    """Read a string env var, trying ``aliases`` (legacy names) as fallback."""
    val = os.getenv(name)
    if val is not None and val.strip() != "":
        return val.strip()
    for alias in aliases:
        v = os.getenv(alias)
        if v is not None and v.strip() != "":
            return v.strip()
    return default


def _get_bool(name: str, default: bool, *aliases: str) -> bool:
    raw = _get(name, None, *aliases)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


def _get_float(name: str, default: float, *aliases: str) -> float:
    raw = _get(name, None, *aliases)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"[config] {name}={raw!r} is not a number; using {default}")
        return default


def _get_int(name: str, default: int, *aliases: str) -> int:
    raw = _get(name, None, *aliases)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except ValueError:
        logger.warning(f"[config] {name}={raw!r} is not an int; using {default}")
        return default


# Placeholder values found in .env.example that should count as "unset".
_PLACEHOLDERS = {
    "", "0x...", "...", "your_key", "your-key", "changeme",
    "https://mainnet.infura.io/v3/your_key",
    "https://mainnet.infura.io/v3/YOUR_KEY",
}


def _is_placeholder(value: Optional[str]) -> bool:
    if value is None:
        return True
    v = value.strip().lower()
    return v in {p.lower() for p in _PLACEHOLDERS} or v.endswith("your_key")


@dataclass
class Config:
    """Fully-resolved runtime configuration."""

    # ── market cycle ──
    market_interval: str = "5m"          # "5m" or "15m"
    interval_seconds: int = 300

    # ── safety switches ──
    dry_run: bool = True
    live_trading: bool = False

    # ── credentials ──
    private_key: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_passphrase: Optional[str] = None
    funder_address: Optional[str] = None
    signature_type: int = 2
    chain_id: int = 137

    # ── infra ──
    rpc_url: Optional[str] = None
    wss_url: Optional[str] = None
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_api_url: str = "https://clob.polymarket.com"
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 2

    # ── order sizing / type ──
    fixed_shares: float = 5.0
    order_type: str = "FOK"              # FOK | FAK | GTC
    slippage_bps: float = 100.0          # 100 bps = 1%
    max_position_usdc: float = 5.0
    max_trades_per_market: int = 1

    # ── entry filters ──
    min_entry_price: float = 0.25
    max_entry_price: float = 0.75
    max_spread_pct: float = 0.05
    late_entry_cutoff_sec: int = 45
    early_entry_cutoff_sec: int = 0      # don't enter before this many secs elapsed
    min_ml_edge: float = 0.10

    # ── exits ──
    take_profit_pct: float = 0.40
    enable_stop_loss: bool = False
    stop_loss_pct: float = 0.50

    # ── runtime ──
    poll_interval_sec: float = 3.0
    signal_lookback_min: int = 3
    price_feed: str = "coinbase"         # coinbase | binance (binance geo-blocks some regions)
    test_mode: bool = False              # accelerated/simulated clock

    # ── derived ──
    paper_trades_path: str = "paper_trades.json"

    @property
    def slippage_frac(self) -> float:
        return self.slippage_bps / 10_000.0

    @property
    def can_trade_live(self) -> bool:
        """Real money allowed ONLY when both switches agree."""
        return self.live_trading and not self.dry_run

    def credential_status(self) -> List[str]:
        """Return list of missing/placeholder live-trading credentials."""
        missing: List[str] = []
        checks = {
            "POLYMARKET_PRIVATE_KEY": self.private_key,
            "POLYMARKET_API_KEY": self.api_key,
            "POLYMARKET_API_SECRET": self.api_secret,
            "POLYMARKET_API_PASSPHRASE": self.api_passphrase,
            "PM_FUNDER_ADDRESS": self.funder_address,
        }
        for name, value in checks.items():
            if _is_placeholder(value):
                missing.append(name)
        return missing


def _normalize_interval(raw: str) -> "tuple[str, int]":
    v = (raw or "5m").strip().lower()
    if v in ("5m", "5min", "5", "300"):
        return "5m", 300
    if v in ("15m", "15min", "15", "900"):
        return "15m", 900
    logger.warning(f"[config] unknown MARKET_INTERVAL={raw!r}; defaulting to 5m")
    return "5m", 300


def load_config(test_mode: bool = False) -> Config:
    """Build a :class:`Config` from the current environment."""
    interval_label, interval_secs = _normalize_interval(_get("MARKET_INTERVAL", "5m"))

    order_type = (_get("ORDER_TYPE", "FOK") or "FOK").strip().upper()
    if order_type not in ("FOK", "FAK", "GTC"):
        logger.warning(f"[config] ORDER_TYPE={order_type!r} invalid; using FOK")
        order_type = "FOK"

    price_feed = (_get("PRICE_FEED", "coinbase") or "coinbase").strip().lower()
    if price_feed not in ("binance", "coinbase"):
        price_feed = "coinbase"

    cfg = Config(
        market_interval=interval_label,
        interval_seconds=interval_secs,
        dry_run=_get_bool("DRY_RUN", True),
        live_trading=_get_bool("LIVE_TRADING", False),
        # credentials (with legacy aliases for backward compatibility)
        private_key=_get("POLYMARKET_PRIVATE_KEY", None, "POLYMARKET_PK"),
        api_key=_get("POLYMARKET_API_KEY"),
        api_secret=_get("POLYMARKET_API_SECRET"),
        api_passphrase=_get("POLYMARKET_API_PASSPHRASE", None, "POLYMARKET_PASSPHRASE"),
        funder_address=_get("PM_FUNDER_ADDRESS", None, "POLYMARKET_FUNDER"),
        signature_type=_get_int("POLYMARKET_SIG_TYPE", 2),
        chain_id=_get_int("CHAIN_ID", 137),
        # infra
        rpc_url=_get("RPC_URL", None, "ETH_RPC_URL"),
        wss_url=_get("WSS_URL"),
        gamma_api_url=(_get("GAMMA_API_URL", "https://gamma-api.polymarket.com") or "").rstrip("/"),
        clob_api_url=(_get("CLOB_API_URL", "https://clob.polymarket.com") or "").rstrip("/"),
        redis_host=_get("REDIS_HOST", "localhost"),
        redis_port=_get_int("REDIS_PORT", 6379),
        redis_db=_get_int("REDIS_DB", 2),
        # sizing
        fixed_shares=_get_float("FIXED_SHARES", 5.0),
        order_type=order_type,
        slippage_bps=_get_float("SLIPPAGE_BPS", 100.0),
        max_position_usdc=_get_float("MAX_POSITION_USDC", 5.0),
        max_trades_per_market=_get_int("MAX_TRADES_PER_MARKET", 1),
        # entry filters
        min_entry_price=_get_float("MIN_ENTRY_PRICE", 0.25),
        max_entry_price=_get_float("MAX_ENTRY_PRICE", 0.75),
        max_spread_pct=_get_float("MAX_SPREAD_PCT", 0.05),
        late_entry_cutoff_sec=_get_int("LATE_ENTRY_CUTOFF_SEC", 45),
        early_entry_cutoff_sec=_get_int("EARLY_ENTRY_CUTOFF_SEC", 0),
        min_ml_edge=_get_float("MIN_ML_EDGE", 0.10),
        # exits
        take_profit_pct=_get_float("TAKE_PROFIT_PCT", 0.40),
        enable_stop_loss=_get_bool("ENABLE_STOP_LOSS", False),
        stop_loss_pct=_get_float("STOP_LOSS_PCT", 0.50),
        # runtime
        poll_interval_sec=_get_float("POLL_INTERVAL_SEC", 3.0),
        signal_lookback_min=_get_int("SIGNAL_LOOKBACK_MIN", 3),
        price_feed=price_feed,
        test_mode=test_mode,
    )
    return cfg


def describe(cfg: Config) -> str:
    """Human-readable one-block summary for startup logs."""
    mode = "LIVE (real money)" if cfg.can_trade_live else "DRY-RUN (paper)"
    lines = [
        f"  MARKET_INTERVAL      = {cfg.market_interval}  ({cfg.interval_seconds}s)",
        f"  MODE                 = {mode}",
        f"    DRY_RUN            = {cfg.dry_run}",
        f"    LIVE_TRADING       = {cfg.live_trading}",
        f"  FIXED_SHARES         = {cfg.fixed_shares}",
        f"  ORDER_TYPE           = {cfg.order_type}",
        f"  SLIPPAGE_BPS         = {cfg.slippage_bps}  ({cfg.slippage_frac:.4%})",
        f"  MAX_POSITION_USDC    = {cfg.max_position_usdc}",
        f"  MAX_TRADES_PER_MARKET= {cfg.max_trades_per_market}",
        f"  ENTRY_PRICE_BAND     = [{cfg.min_entry_price}, {cfg.max_entry_price}]",
        f"  MAX_SPREAD_PCT       = {cfg.max_spread_pct}",
        f"  LATE_ENTRY_CUTOFF_SEC= {cfg.late_entry_cutoff_sec}",
        f"  MIN_ML_EDGE          = {cfg.min_ml_edge}",
        f"  TAKE_PROFIT_PCT      = {cfg.take_profit_pct}",
        f"  ENABLE_STOP_LOSS     = {cfg.enable_stop_loss}  (pct={cfg.stop_loss_pct})",
    ]
    return "\n".join(lines)
