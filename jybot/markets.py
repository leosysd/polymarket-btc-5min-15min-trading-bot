"""
jybot.markets -- dynamic discovery of BTC UP/DOWN markets + order-book access.

Nothing is hard-coded. Markets are discovered live from the Polymarket Gamma
API by computing the slug for the current (and upcoming) interval windows:

    btc-updown-5m-{unix_ts}      where unix_ts is aligned to 300s (window START)
    btc-updown-15m-{unix_ts}     where unix_ts is aligned to 900s

Settlement time = unix_ts + interval_seconds (verified against live Gamma data).

Order books come from the CLOB REST API:

    GET {clob}/book?token_id=...     we normalise bids/asks to ascending price,
                                     so best bid = bids[-1], best ask = asks[0]
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from jybot.config import Config
from jybot.http import get_json_safe
from jybot.log import logger


@dataclass
class Token:
    token_id: str
    outcome: str          # "Up" or "Down"


@dataclass
class Market:
    slug: str
    question: str
    condition_id: str
    interval: str         # "5m" / "15m"
    start_ts: int         # unix window start (from slug)
    settle_ts: int        # start_ts + interval_seconds
    up: Optional[Token]
    down: Optional[Token]
    accepting_orders: bool
    active: bool
    closed: bool
    order_min_size: float
    tick_size: float
    raw: dict

    @property
    def tokens(self) -> List[Token]:
        return [t for t in (self.up, self.down) if t is not None]

    def seconds_to_settle(self, now: Optional[float] = None) -> float:
        now = now if now is not None else time.time()
        return self.settle_ts - now

    def seconds_since_start(self, now: Optional[float] = None) -> float:
        now = now if now is not None else time.time()
        return now - self.start_ts

    def is_tradeable(self) -> bool:
        return (
            self.active
            and not self.closed
            and self.accepting_orders
            and self.up is not None
            and self.down is not None
        )


@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: List[BookLevel]        # ascending by price
    asks: List[BookLevel]        # ascending by price
    tick_size: float
    min_order_size: float

    @property
    def best_bid(self) -> Optional[BookLevel]:
        # bids are ascending -> highest (best) bid is last
        return self.bids[-1] if self.bids else None

    @property
    def best_ask(self) -> Optional[BookLevel]:
        # asks are ascending -> lowest (best) ask is first
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid.price + self.best_ask.price) / 2.0
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return self.best_ask.price - self.best_bid.price
        return None

    @property
    def spread_pct(self) -> Optional[float]:
        mid = self.mid
        spread = self.spread
        if mid and spread is not None and mid > 0:
            return spread / mid
        return None


# ── slug math ──────────────────────────────────────────────────────────────

def aligned_start(now_ts: float, interval_seconds: int) -> int:
    return (int(now_ts) // interval_seconds) * interval_seconds


def slug_for(interval_label: str, start_ts: int) -> str:
    return f"btc-updown-{interval_label}-{start_ts}"


def candidate_slugs(cfg: Config, now_ts: float, look_back: int = 1, look_ahead: int = 3) -> List[str]:
    """Slugs for the current window plus a few neighbours (handles clock skew
    and lets us pre-load the next markets)."""
    start = aligned_start(now_ts, cfg.interval_seconds)
    slugs = []
    for i in range(-look_back, look_ahead + 1):
        ts = start + i * cfg.interval_seconds
        slugs.append(slug_for(cfg.market_interval, ts))
    return slugs


# ── parsing ────────────────────────────────────────────────────────────────

def _parse_json_array(value) -> List:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _slug_start_ts(slug: str) -> Optional[int]:
    try:
        return int(slug.rsplit("-", 1)[1])
    except (ValueError, IndexError):
        return None


def parse_market(raw: dict, cfg: Config) -> Optional[Market]:
    """Turn a Gamma market dict into a :class:`Market`. Returns None if it is
    missing token ids (not yet fully deployed)."""
    slug = raw.get("slug") or ""
    start_ts = _slug_start_ts(slug)
    if start_ts is None:
        return None

    token_ids = _parse_json_array(raw.get("clobTokenIds"))
    outcomes = _parse_json_array(raw.get("outcomes"))

    up: Optional[Token] = None
    down: Optional[Token] = None
    for tid, outcome in zip(token_ids, outcomes):
        if not tid:
            continue
        oc = str(outcome).strip().lower()
        if oc in ("up", "yes"):
            up = Token(token_id=str(tid), outcome="Up")
        elif oc in ("down", "no"):
            down = Token(token_id=str(tid), outcome="Down")

    def _to_float(v, default):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    return Market(
        slug=slug,
        question=raw.get("question") or slug,
        condition_id=raw.get("conditionId") or "",
        interval=cfg.market_interval,
        start_ts=start_ts,
        settle_ts=start_ts + cfg.interval_seconds,
        up=up,
        down=down,
        accepting_orders=bool(raw.get("acceptingOrders", False)),
        active=bool(raw.get("active", False)),
        closed=bool(raw.get("closed", True)),
        order_min_size=_to_float(raw.get("orderMinSize"), 5.0),
        tick_size=_to_float(raw.get("orderPriceMinTickSize"), 0.01),
        raw=raw,
    )


# ── discovery ──────────────────────────────────────────────────────────────

def fetch_markets_by_slugs(cfg: Config, slugs: List[str]) -> List[Market]:
    """Query Gamma for the given slugs and parse them into Market objects."""
    if not slugs:
        return []
    data = get_json_safe(
        f"{cfg.gamma_api_url}/markets",
        params={"slug": slugs, "limit": 100},
        timeout=20.0,
    )
    if not isinstance(data, list):
        # Gamma sometimes wraps in {"data": [...]}
        if isinstance(data, dict):
            data = data.get("data") or []
        else:
            data = []
    markets: List[Market] = []
    for raw in data:
        market = parse_market(raw, cfg)
        if market is not None:
            markets.append(market)
    return markets


def discover_active_market(cfg: Config, now_ts: Optional[float] = None) -> Optional[Market]:
    """Find the single best market to trade *right now*: the active window whose
    settlement is still in the future and which is accepting orders. Returns the
    soonest-settling tradeable market (the current window)."""
    now_ts = now_ts if now_ts is not None else time.time()
    slugs = candidate_slugs(cfg, now_ts, look_back=1, look_ahead=2)
    markets = fetch_markets_by_slugs(cfg, slugs)

    tradeable = [
        m for m in markets
        if m.is_tradeable() and m.seconds_to_settle(now_ts) > 0
    ]
    if not tradeable:
        return None
    # Prefer the current window (soonest settlement still in the future).
    tradeable.sort(key=lambda m: m.settle_ts)
    return tradeable[0]


def discover_upcoming_markets(cfg: Config, now_ts: Optional[float] = None, count: int = 4) -> List[Market]:
    """Return current + upcoming tradeable markets, soonest first (for display)."""
    now_ts = now_ts if now_ts is not None else time.time()
    slugs = candidate_slugs(cfg, now_ts, look_back=1, look_ahead=count + 1)
    markets = fetch_markets_by_slugs(cfg, slugs)
    future = [m for m in markets if m.seconds_to_settle(now_ts) > 0]
    future.sort(key=lambda m: m.settle_ts)
    return future[:count]


# ── order book ─────────────────────────────────────────────────────────────

def fetch_orderbook(cfg: Config, token_id: str) -> Optional[OrderBook]:
    """Fetch and parse the CLOB order book for a token id."""
    data = get_json_safe(
        f"{cfg.clob_api_url}/book",
        params={"token_id": token_id},
        timeout=15.0,
    )
    if not isinstance(data, dict):
        return None

    def _levels(key) -> List[BookLevel]:
        out: List[BookLevel] = []
        for lvl in data.get(key, []) or []:
            try:
                out.append(BookLevel(price=float(lvl["price"]), size=float(lvl["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda l: l.price)  # ensure ascending
        return out

    def _to_float(v, default):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    return OrderBook(
        token_id=token_id,
        bids=_levels("bids"),
        asks=_levels("asks"),
        tick_size=_to_float(data.get("tick_size"), 0.01),
        min_order_size=_to_float(data.get("min_order_size"), 5.0),
    )
