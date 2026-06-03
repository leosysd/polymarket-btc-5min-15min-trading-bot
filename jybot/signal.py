"""
jybot.signal -- BTC short-horizon UP/DOWN probability estimator.

For a 5-minute UP/DOWN market, "Up" settles in-the-money when BTC's price at
settlement is above its price at the window start. We estimate p(UP) from live
spot data (Binance or Coinbase 1-minute candles):

    * drift  = (last_price - window_start_price) / window_start_price
    * mom    = short EMA slope over the lookback window
    * p_up   = logistic( k1 * drift_norm + k2 * mom_norm )

This is intentionally simple, transparent and dependency-free (stdlib HTTP).
If the price feed is unavailable the signal degrades to "no edge" so the bot
simply does not trade -- except in ``test_mode``, where a deterministic
pseudo-signal is produced so the full pipeline can be demonstrated offline.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from jybot.config import Config
from jybot.http import get_json_safe
from jybot.log import logger


@dataclass
class Signal:
    direction: Optional[str]   # "UP" | "DOWN" | None
    p_up: float                # probability BTC closes up (0..1)
    confidence: float          # 0..1
    reason: str
    spot: Optional[float] = None
    ref: Optional[float] = None

    @property
    def p_down(self) -> float:
        return 1.0 - self.p_up


def _logistic(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


# ── price feeds (each returns list of (close_time_sec, close_price)) ─────────

def _binance_candles(limit: int = 10) -> List[tuple]:
    data = get_json_safe(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": "BTCUSDT", "interval": "1m", "limit": limit},
        timeout=10.0,
        retries=2,
    )
    if not isinstance(data, list):
        return []
    out = []
    for row in data:
        try:
            close_time = int(row[6]) / 1000.0
            close = float(row[4])
            out.append((close_time, close))
        except (IndexError, TypeError, ValueError):
            continue
    return out


def _coinbase_candles(limit: int = 10) -> List[tuple]:
    data = get_json_safe(
        "https://api.exchange.coinbase.com/products/BTC-USD/candles",
        params={"granularity": 60},
        timeout=10.0,
        retries=2,
    )
    if not isinstance(data, list):
        return []
    rows = []
    for row in data:  # [time, low, high, open, close, volume], newest first
        try:
            rows.append((int(row[0]), float(row[4])))
        except (IndexError, TypeError, ValueError):
            continue
    rows.sort(key=lambda r: r[0])
    return rows[-limit:]


def _fetch_candles(cfg: Config, limit: int) -> List[tuple]:
    primary = _binance_candles if cfg.price_feed == "binance" else _coinbase_candles
    secondary = _coinbase_candles if cfg.price_feed == "binance" else _binance_candles
    candles = primary(limit)
    if not candles:
        logger.warning(f"[signal] primary feed ({cfg.price_feed}) empty; trying fallback")
        candles = secondary(limit)
    return candles


def _pseudo_signal(start_ts: int) -> Signal:
    """Deterministic offline signal for test_mode demos (no network)."""
    # Use the window start timestamp to vary direction predictably.
    phase = (start_ts // 300) % 4
    p_up = {0: 0.66, 1: 0.38, 2: 0.59, 3: 0.41}[phase]
    direction = "UP" if p_up >= 0.5 else "DOWN"
    return Signal(
        direction=direction,
        p_up=p_up,
        confidence=abs(p_up - 0.5) * 2,
        reason=f"test-mode pseudo-signal (phase={phase})",
    )


def compute_signal(cfg: Config, window_start_ts: int) -> Signal:
    """Estimate p(UP) for the market window starting at ``window_start_ts``."""
    lookback = max(3, cfg.signal_lookback_min + 2)
    candles = _fetch_candles(cfg, lookback)

    if not candles:
        if cfg.test_mode:
            return _pseudo_signal(window_start_ts)
        return Signal(None, 0.5, 0.0, "price feed unavailable -- no trade")

    spot = candles[-1][1]

    # Reference price = candle closest to the window start.
    ref_candle = min(candles, key=lambda c: abs(c[0] - window_start_ts))
    ref = ref_candle[1]

    drift = (spot - ref) / ref if ref else 0.0

    # Short momentum: average per-step return over the lookback window.
    rets = []
    for i in range(1, len(candles)):
        prev = candles[i - 1][1]
        cur = candles[i][1]
        if prev:
            rets.append((cur - prev) / prev)
    mom = sum(rets) / len(rets) if rets else 0.0

    # Scale factors: BTC 5-min moves are ~0.1%, so amplify into logit space.
    drift_norm = drift / 0.0015
    mom_norm = mom / 0.0008
    logit = 3.0 * drift_norm + 2.0 * mom_norm
    p_up = _logistic(logit)
    # Clamp to a sane band so we never claim near-certainty from noise.
    p_up = max(0.15, min(0.85, p_up))

    direction = "UP" if p_up >= 0.5 else "DOWN"
    confidence = abs(p_up - 0.5) * 2
    reason = (
        f"spot={spot:.1f} ref={ref:.1f} drift={drift*100:+.3f}% "
        f"mom={mom*100:+.4f}% -> p_up={p_up:.3f}"
    )
    return Signal(direction, p_up, confidence, reason, spot=spot, ref=ref)
