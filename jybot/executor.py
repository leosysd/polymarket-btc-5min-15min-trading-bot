"""
jybot.executor -- order placement (paper + live).

Order rules (per spec)
----------------------
* Fixed-share sizing: every order uses ``size = FIXED_SHARES`` (BUY) or
  ``size <= position`` (SELL). We NEVER pass a USD ``amount`` as the share
  count -- share count and notional are kept distinct.
* BUY price  = best_ask * (1 + SLIPPAGE_BPS)         (rounded to tick)
* SELL price = best_bid * (1 - SLIPPAGE_BPS)         (rounded to tick)
* ORDER_TYPE:
    FOK = fill-or-kill   -> must fill in full or it is cancelled
    FAK = fill-and-kill  -> partial fills allowed, remainder cancelled
    GTC = good-till-cancel -> remainder rests on the book
* DRY_RUN (default): no real order is signed. The fill is simulated against the
  live order book so paper P&L is realistic.

The live path imports ``py-clob-client`` lazily -- it is only needed when both
``DRY_RUN=false`` and ``LIVE_TRADING=true`` (see Config.can_trade_live).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional

from jybot.config import Config
from jybot.log import logger
from jybot.markets import OrderBook, Token


@dataclass
class OrderResult:
    status: str                 # FILLED | PARTIAL | CANCELLED | REJECTED | FAILED | RESTING
    side: str                   # BUY | SELL
    token_id: str
    requested_size: float
    filled_size: float
    limit_price: float
    avg_price: float
    order_type: str
    order_id: Optional[str] = None
    reason: str = ""
    dry_run: bool = True

    @property
    def is_filled(self) -> bool:
        return self.status in ("FILLED", "PARTIAL") and self.filled_size > 0


def _round_to_tick(price: float, tick: float, *, up: bool) -> float:
    if tick <= 0:
        tick = 0.01
    steps = price / tick
    rounded = (math_ceil(steps) if up else math_floor(steps)) * tick
    # clamp to valid CLOB band
    rounded = max(tick, min(1.0 - tick, rounded))
    return round(rounded, 4)


def math_ceil(x: float) -> int:
    import math
    return int(math.ceil(x - 1e-9))


def math_floor(x: float) -> int:
    import math
    return int(math.floor(x + 1e-9))


def buy_limit_price(book: OrderBook, slippage_frac: float) -> Optional[float]:
    if not book.best_ask:
        return None
    raw = book.best_ask.price * (1.0 + slippage_frac)
    return _round_to_tick(raw, book.tick_size, up=True)


def sell_limit_price(book: OrderBook, slippage_frac: float) -> Optional[float]:
    if not book.best_bid:
        return None
    raw = book.best_bid.price * (1.0 - slippage_frac)
    return _round_to_tick(raw, book.tick_size, up=False)


def _simulate_fill(book: OrderBook, side: str, size: float, limit_price: float):
    """Walk the book to compute fillable size & average price at/under (BUY) or
    at/over (SELL) the limit price. Returns (filled_size, avg_price)."""
    filled = 0.0
    notional = 0.0
    if side == "BUY":
        # consume asks from lowest price up
        for lvl in book.asks:  # ascending
            if lvl.price > limit_price + 1e-9:
                break
            take = min(size - filled, lvl.size)
            if take <= 0:
                break
            filled += take
            notional += take * lvl.price
            if filled >= size - 1e-9:
                break
    else:  # SELL -- consume bids from highest price down
        for lvl in reversed(book.bids):  # descending
            if lvl.price < limit_price - 1e-9:
                break
            take = min(size - filled, lvl.size)
            if take <= 0:
                break
            filled += take
            notional += take * lvl.price
            if filled >= size - 1e-9:
                break
    avg = (notional / filled) if filled > 0 else limit_price
    return filled, avg


class Executor:
    """Places orders. One instance per run; lazily connects the live client."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client = None
        self._client_ready = False

    # ── live client (lazy) ────────────────────────────────────────────────
    def _ensure_live_client(self) -> bool:
        if self._client_ready:
            return self._client is not None
        self._client_ready = True
        try:
            from py_clob_client.client import ClobClient
        except Exception as exc:
            logger.error(f"[exec] py-clob-client not installed: {exc}")
            logger.error("[exec] install it for live trading: pip install py-clob-client")
            self._client = None
            return False
        try:
            funder = self.cfg.funder_address or None
            client = ClobClient(
                host=self.cfg.clob_api_url,
                key=self.cfg.private_key,
                chain_id=self.cfg.chain_id,
                signature_type=self.cfg.signature_type,
                funder=funder,
            )
            client.set_api_creds(
                # creds object; py-clob-client accepts kwargs via ApiCreds
                creds=_make_api_creds(self.cfg),
            )
            self._client = client
            logger.info("[exec] live ClobClient ready")
            return True
        except Exception as exc:
            logger.error(f"[exec] failed to init ClobClient: {exc}")
            self._client = None
            return False

    # ── public API ────────────────────────────────────────────────────────
    def place_buy(self, token: Token, book: OrderBook, size: float) -> OrderResult:
        price = buy_limit_price(book, self.cfg.slippage_frac)
        if price is None:
            return OrderResult(
                status="REJECTED", side="BUY", token_id=token.token_id,
                requested_size=size, filled_size=0.0, limit_price=0.0,
                avg_price=0.0, order_type=self.cfg.order_type,
                reason="empty order book (no ask)", dry_run=not self.cfg.can_trade_live,
            )
        return self._place("BUY", token, book, size, price)

    def place_sell(self, token: Token, book: OrderBook, size: float, position: float) -> OrderResult:
        # SELL size can never exceed the current position.
        size = min(size, position)
        if size <= 0:
            return OrderResult(
                status="REJECTED", side="SELL", token_id=token.token_id,
                requested_size=size, filled_size=0.0, limit_price=0.0,
                avg_price=0.0, order_type=self.cfg.order_type,
                reason="no position to sell", dry_run=not self.cfg.can_trade_live,
            )
        price = sell_limit_price(book, self.cfg.slippage_frac)
        if price is None:
            return OrderResult(
                status="REJECTED", side="SELL", token_id=token.token_id,
                requested_size=size, filled_size=0.0, limit_price=0.0,
                avg_price=0.0, order_type=self.cfg.order_type,
                reason="empty order book (no bid)", dry_run=not self.cfg.can_trade_live,
            )
        return self._place("SELL", token, book, size, price)

    # ── internal ──────────────────────────────────────────────────────────
    def _place(self, side: str, token: Token, book: OrderBook, size: float, price: float) -> OrderResult:
        if self.cfg.can_trade_live:
            return self._place_live(side, token, size, price)
        return self._place_paper(side, token, book, size, price)

    def _place_paper(self, side: str, token: Token, book: OrderBook, size: float, price: float) -> OrderResult:
        filled, avg = _simulate_fill(book, side, size, price)
        order_type = self.cfg.order_type
        status, reason = _resolve_sim_status(order_type, size, filled)
        if status in ("CANCELLED", "REJECTED"):
            filled, avg = 0.0, price
        logger.info(
            f"[paper] {side} {token.outcome} size={size} @ {price:.3f} "
            f"({order_type}) -> {status} filled={filled:.2f} avg={avg:.3f}"
        )
        return OrderResult(
            status=status, side=side, token_id=token.token_id,
            requested_size=size, filled_size=filled, limit_price=price,
            avg_price=avg, order_type=order_type,
            order_id=f"paper-{int(time.time()*1000)}", reason=reason, dry_run=True,
        )

    def _place_live(self, side: str, token: Token, size: float, price: float) -> OrderResult:
        if not self._ensure_live_client():
            return OrderResult(
                status="FAILED", side=side, token_id=token.token_id,
                requested_size=size, filled_size=0.0, limit_price=price,
                avg_price=0.0, order_type=self.cfg.order_type,
                reason="live client unavailable", dry_run=False,
            )
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            poly_side = BUY if side == "BUY" else SELL
            order_args = OrderArgs(
                token_id=token.token_id,
                price=float(price),
                size=float(size),
                side=poly_side,
                fee_rate_bps=0,
            )
            signed = self._client.create_order(order_args)
            ot_map = {
                "FOK": OrderType.FOK,
                "FAK": getattr(OrderType, "FAK", OrderType.GTC),
                "GTC": OrderType.GTC,
            }
            poly_ot = ot_map.get(self.cfg.order_type, OrderType.FOK)
            resp = self._client.post_order(signed, orderType=poly_ot)

            order_id = (resp or {}).get("orderID") or (resp or {}).get("orderId")
            success = bool(resp and (resp.get("success") or order_id))
            matched = float((resp or {}).get("makingAmount", 0) or 0)
            status = "FILLED" if success and matched else ("RESTING" if success else "FAILED")
            logger.info(f"[live] {side} {token.outcome} size={size} @ {price:.3f} -> {resp}")
            return OrderResult(
                status=status, side=side, token_id=token.token_id,
                requested_size=size, filled_size=size if status == "FILLED" else 0.0,
                limit_price=price, avg_price=price, order_type=self.cfg.order_type,
                order_id=order_id, reason=str(resp)[:200], dry_run=False,
            )
        except Exception as exc:
            logger.error(f"[live] order failed: {exc}")
            return OrderResult(
                status="FAILED", side=side, token_id=token.token_id,
                requested_size=size, filled_size=0.0, limit_price=price,
                avg_price=0.0, order_type=self.cfg.order_type,
                reason=str(exc)[:200], dry_run=False,
            )


def _resolve_sim_status(order_type: str, size: float, filled: float):
    full = filled >= size - 1e-9
    if order_type == "FOK":
        if full:
            return "FILLED", "fully filled"
        return "CANCELLED", "FOK: insufficient liquidity for full fill"
    if order_type == "FAK":
        if full:
            return "FILLED", "fully filled"
        if filled > 0:
            return "PARTIAL", "FAK: partial fill, remainder cancelled"
        return "CANCELLED", "FAK: nothing marketable"
    # GTC
    if full:
        return "FILLED", "fully filled"
    if filled > 0:
        return "PARTIAL", "GTC: partial fill, remainder rests"
    return "RESTING", "GTC: nothing marketable yet, order rests"


def _make_api_creds(cfg: Config):
    """Build py-clob-client ApiCreds object."""
    from py_clob_client.clob_types import ApiCreds

    return ApiCreds(
        api_key=cfg.api_key,
        api_secret=cfg.api_secret,
        api_passphrase=cfg.api_passphrase,
    )
