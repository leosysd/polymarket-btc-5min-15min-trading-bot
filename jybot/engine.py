"""
jybot.engine -- the trading loop.

Lifecycle per 5-minute (or 15-minute) market:

    discover active market  ──►  evaluate entry  ──►  place ONE bet (fixed shares)
            ▲                                                  │
            └──────────── rotate on settlement ◄── manage TP/SL/settle

Guarantees enforced here:
    * At most MAX_TRADES_PER_MARKET entries per market (default 1).
    * No entry within LATE_ENTRY_CUTOFF_SEC of settlement.
    * Entry only when price is inside [MIN_ENTRY_PRICE, MAX_ENTRY_PRICE],
      spread <= MAX_SPREAD_PCT, and model edge >= MIN_ML_EDGE.
    * Position notional <= MAX_POSITION_USDC.
    * Every decision is logged: market, YES/NO token, price, signal, traded?, why.
"""
from __future__ import annotations

import time
from typing import Dict, Optional

from jybot.config import Config, describe
from jybot.executor import Executor
from jybot.log import logger
from jybot.markets import (
    Market,
    OrderBook,
    Token,
    discover_active_market,
    discover_upcoming_markets,
    fetch_markets_by_slugs,
    fetch_orderbook,
    parse_market,
)
from jybot.signal import compute_signal
from jybot.state import Position, TradeLog


class TradingEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.executor = Executor(cfg)
        self.log = TradeLog(cfg.paper_trades_path)
        self.current_slug: Optional[str] = None
        self.trades_in_market: int = 0
        self.position: Optional[Position] = None
        self._reject_logged: Dict[str, float] = {}
        self._running = True

    # ── helpers ────────────────────────────────────────────────────────────
    def _now(self) -> float:
        return time.time()

    def _take_profit_price(self, entry: float) -> float:
        return min(0.99, entry + self.cfg.take_profit_pct * (1.0 - entry))

    def _stop_loss_price(self, entry: float) -> float:
        return max(0.01, entry - self.cfg.stop_loss_pct * entry)

    def _throttle(self, key: str, period: float = 15.0) -> bool:
        """Return True if we should log/act (i.e. not throttled)."""
        now = self._now()
        last = self._reject_logged.get(key, 0.0)
        if now - last >= period:
            self._reject_logged[key] = now
            return True
        return False

    # ── market rotation ────────────────────────────────────────────────────
    def _on_new_market(self, market: Market) -> None:
        self.current_slug = market.slug
        self.trades_in_market = 0
        logger.info("=" * 72)
        logger.info(f"[market] ACTIVE: {market.slug}")
        logger.info(f"[market] {market.question}")
        logger.info(
            f"[market] UP token={_short(market.up.token_id) if market.up else 'N/A'}  "
            f"DOWN token={_short(market.down.token_id) if market.down else 'N/A'}"
        )
        logger.info(
            f"[market] settle in {market.seconds_to_settle(self._now()):.0f}s  "
            f"min_size={market.order_min_size} tick={market.tick_size}"
        )
        logger.info("=" * 72)

    # ── entry decision ─────────────────────────────────────────────────────
    def _evaluate_entry(self, market: Market) -> None:
        now = self._now()
        secs_to_settle = market.seconds_to_settle(now)
        secs_since_start = market.seconds_since_start(now)

        # late / early gates
        if secs_to_settle <= self.cfg.late_entry_cutoff_sec:
            if self._throttle(f"late:{market.slug}"):
                logger.info(
                    f"[skip] {market.slug}: too close to settle "
                    f"({secs_to_settle:.0f}s <= LATE_ENTRY_CUTOFF_SEC={self.cfg.late_entry_cutoff_sec})"
                )
            return
        if secs_since_start < self.cfg.early_entry_cutoff_sec:
            return
        if self.trades_in_market >= self.cfg.max_trades_per_market:
            return

        # signal
        sig = compute_signal(self.cfg, market.start_ts)
        if sig.direction is None:
            if self._throttle(f"nosig:{market.slug}"):
                logger.info(f"[skip] {market.slug}: {sig.reason}")
            return

        token = market.up if sig.direction == "UP" else market.down
        if token is None:
            if self._throttle(f"notok:{market.slug}"):
                logger.warning(f"[skip] {market.slug}: missing {sig.direction} token id")
            return

        book = fetch_orderbook(self.cfg, token.token_id)
        if book is None or book.best_ask is None:
            if self._throttle(f"nobook:{market.slug}"):
                logger.warning(
                    f"[skip] {market.slug} {token.outcome}: empty book / no ask (illiquid)"
                )
            return

        ask = book.best_ask.price
        p_side = sig.p_up if sig.direction == "UP" else sig.p_down
        edge = p_side - ask

        # decision banner (always shows the reasoning)
        banner = (
            f"[signal] {market.slug} | {token.outcome} token={_short(token.token_id)} | "
            f"{sig.reason} | ask={ask:.3f} p({sig.direction})={p_side:.3f} edge={edge:+.3f}"
        )

        reasons = []
        if not (self.cfg.min_entry_price <= ask <= self.cfg.max_entry_price):
            reasons.append(
                f"price {ask:.3f} outside band [{self.cfg.min_entry_price},{self.cfg.max_entry_price}]"
            )
        spread_pct = book.spread_pct
        if spread_pct is not None and spread_pct > self.cfg.max_spread_pct:
            reasons.append(f"spread {spread_pct:.3f} > MAX_SPREAD_PCT {self.cfg.max_spread_pct}")
        if edge < self.cfg.min_ml_edge:
            reasons.append(f"edge {edge:+.3f} < MIN_ML_EDGE {self.cfg.min_ml_edge}")

        size = self.cfg.fixed_shares
        if size < market.order_min_size:
            reasons.append(
                f"FIXED_SHARES {size} < market min_size {market.order_min_size}"
            )
        notional = size * ask
        if notional > self.cfg.max_position_usdc:
            reasons.append(
                f"notional ${notional:.2f} > MAX_POSITION_USDC ${self.cfg.max_position_usdc}"
            )

        if reasons:
            if self._throttle(f"reject:{market.slug}", self.cfg.poll_interval_sec * 3):
                logger.info(banner)
                logger.info(f"[no-trade] {market.slug}: " + " ; ".join(reasons))
            return

        logger.info(banner)
        logger.info(
            f"[ENTER] {market.slug} BUY {token.outcome} size={size} "
            f"(notional~${notional:.2f}) order_type={self.cfg.order_type} "
            f"mode={'LIVE' if self.cfg.can_trade_live else 'PAPER'}"
        )

        result = self.executor.place_buy(token, book, size)
        if not result.is_filled:
            logger.warning(
                f"[ENTER] {market.slug} not filled: {result.status} ({result.reason})"
            )
            return

        self.trades_in_market += 1
        self.position = Position(
            market_slug=market.slug,
            condition_id=market.condition_id,
            token_id=token.token_id,
            outcome=token.outcome,
            direction=sig.direction,
            entry_price=result.avg_price,
            size=result.filled_size,
            entry_ts=now,
            settle_ts=market.settle_ts,
            p_model=p_side,
            ref_price=sig.ref or 0.0,
        )
        self.log.append(self.position.to_record())
        tp = self._take_profit_price(result.avg_price)
        sl = self._stop_loss_price(result.avg_price) if self.cfg.enable_stop_loss else None
        logger.info(
            f"[POSITION] OPEN {token.outcome} {result.filled_size}@{result.avg_price:.3f} "
            f"TP={tp:.3f}" + (f" SL={sl:.3f}" if sl else " SL=off")
        )

    # ── position management ────────────────────────────────────────────────
    def _manage_position(self) -> None:
        pos = self.position
        if pos is None:
            return
        now = self._now()

        # settled?
        if now >= pos.settle_ts:
            self._settle_position(pos)
            return

        book = fetch_orderbook(self.cfg, pos.token_id)
        if book is None or book.best_bid is None:
            return
        bid = book.best_bid.price

        tp = self._take_profit_price(pos.entry_price)
        sl = self._stop_loss_price(pos.entry_price) if self.cfg.enable_stop_loss else None

        if bid >= tp:
            self._exit_position(pos, book, reason=f"take-profit (bid {bid:.3f} >= TP {tp:.3f})")
        elif sl is not None and bid <= sl:
            self._exit_position(pos, book, reason=f"stop-loss (bid {bid:.3f} <= SL {sl:.3f})")

    def _exit_position(self, pos: Position, book: OrderBook, reason: str) -> None:
        token = Token(token_id=pos.token_id, outcome=pos.outcome)
        result = self.executor.place_sell(token, book, pos.size, position=pos.size)
        exit_price = result.avg_price if result.is_filled else (book.best_bid.price if book.best_bid else pos.entry_price)
        pnl = (exit_price - pos.entry_price) * pos.size
        pos.status = "CLOSED"
        pos.exit_price = exit_price
        pos.exit_reason = reason
        pos.outcome_result = "WIN" if pnl >= 0 else "LOSS"
        pos.pnl_usd = pnl
        self.log.update_last_open(pos.token_id, pos.to_record())
        logger.info(
            f"[EXIT] {pos.market_slug} {pos.outcome} {reason} -> "
            f"{pos.outcome_result} pnl=${pnl:+.4f}"
        )
        self.position = None

    def _settle_position(self, pos: Position) -> None:
        """Resolve a position at market settlement (binary -> token worth 0 or 1)."""
        resolved = self._fetch_resolution(pos)
        if resolved is None:
            # fallback: compare BTC spot vs window-start reference
            resolved = self._price_feed_resolution(pos)

        value = resolved if resolved is not None else pos.entry_price
        pnl = (value - pos.entry_price) * pos.size
        pos.status = "CLOSED"
        pos.exit_price = value
        pos.exit_reason = "settled (resolution)" if resolved is not None else "settled (unresolved-mark)"
        pos.outcome_result = "WIN" if value >= 0.5 else "LOSS"
        pos.pnl_usd = pnl
        self.log.update_last_open(pos.token_id, pos.to_record())
        logger.info(
            f"[SETTLE] {pos.market_slug} {pos.outcome} value={value:.2f} -> "
            f"{pos.outcome_result} pnl=${pnl:+.4f}"
        )
        self.position = None

    def _fetch_resolution(self, pos: Position) -> Optional[float]:
        """Return 1.0/0.0 if Gamma reports the market resolved, else None."""
        markets = fetch_markets_by_slugs(self.cfg, [pos.market_slug])
        if not markets:
            return None
        raw = markets[0].raw
        if not markets[0].closed:
            return None
        import json as _json
        try:
            prices = raw.get("outcomePrices")
            outcomes = raw.get("outcomes")
            if isinstance(prices, str):
                prices = _json.loads(prices)
            if isinstance(outcomes, str):
                outcomes = _json.loads(outcomes)
            if not prices or not outcomes:
                return None
            for oc, pr in zip(outcomes, prices):
                if str(oc).strip().lower() == pos.outcome.strip().lower():
                    return float(pr)
        except Exception:
            return None
        return None

    def _price_feed_resolution(self, pos: Position) -> Optional[float]:
        start_ts = int(pos.settle_ts - self.cfg.interval_seconds)
        sig = compute_signal(self.cfg, start_ts)
        if sig.spot is None or pos.ref_price <= 0:
            return None
        up_won = sig.spot > pos.ref_price
        won = (pos.direction == "UP" and up_won) or (pos.direction == "DOWN" and not up_won)
        return 1.0 if won else 0.0

    # ── main loop ──────────────────────────────────────────────────────────
    def run_once(self) -> None:
        """One iteration: rotate market, manage position, evaluate entry."""
        market = discover_active_market(self.cfg, self._now())

        # manage an existing position even if its market is no longer "active"
        if self.position is not None:
            if market is None or market.slug != self.position.market_slug:
                # the position's own market may have rolled -- settle by time
                self._manage_position()
            else:
                self._manage_position()

        if market is None:
            if self._throttle("nomarket", 20.0):
                logger.info(f"[wait] no active BTC {self.cfg.market_interval} market right now -- scanning...")
            return

        if market.slug != self.current_slug:
            self._on_new_market(market)

        if self.position is None:
            self._evaluate_entry(market)

    def run(self) -> None:
        logger.info("=" * 72)
        logger.info(f"jybot -- Polymarket BTC {self.cfg.market_interval} UP/DOWN trading engine")
        logger.info("=" * 72)
        for line in describe(self.cfg).splitlines():
            logger.info(line)
        logger.info("=" * 72)

        if not self.cfg.can_trade_live:
            logger.info("SAFE MODE: paper trading only (set DRY_RUN=false AND "
                        "LIVE_TRADING=true AND run --live for real orders)")
        else:
            logger.warning("LIVE TRADING ENABLED -- REAL MONEY AT RISK")

        # show upcoming markets once at startup (proves discovery works)
        try:
            upcoming = discover_upcoming_markets(self.cfg, self._now(), count=4)
            if upcoming:
                logger.info("[discovery] upcoming markets:")
                for m in upcoming:
                    logger.info(
                        f"  {m.slug}  settle_in={m.seconds_to_settle(self._now()):.0f}s "
                        f"accepting={m.accepting_orders}"
                    )
            else:
                logger.warning("[discovery] no upcoming markets found (API/filters?)")
        except Exception as exc:
            logger.warning(f"[discovery] preview failed: {exc}")

        iterations = 0
        max_iters = self._max_iterations()
        try:
            while self._running:
                try:
                    self.run_once()
                except Exception as exc:
                    logger.error(f"[loop] error: {exc}")
                iterations += 1
                if max_iters is not None and iterations >= max_iters:
                    logger.info(f"[test-mode] reached {max_iters} iterations -- stopping")
                    break
                time.sleep(self.cfg.poll_interval_sec)
        except KeyboardInterrupt:
            logger.info("interrupted -- shutting down")
        finally:
            self._print_summary()

    def _max_iterations(self) -> Optional[int]:
        # In test-mode we run a short, bounded demo so `--test-mode` returns.
        if self.cfg.test_mode:
            import os
            return int(os.getenv("TEST_MODE_ITERS", "3"))
        return None

    def _print_summary(self) -> None:
        s = self.log.summary()
        logger.info("=" * 72)
        logger.info(
            f"[summary] trades={s['trades']} settled={s['settled']} "
            f"win_rate={s['win_rate']:.1f}% pnl=${s['pnl_usd']:+.4f}"
        )
        logger.info(f"[summary] paper trades file: {self.cfg.paper_trades_path}")
        logger.info("=" * 72)

    def stop(self) -> None:
        self._running = False


def _short(token_id: str) -> str:
    if not token_id:
        return "N/A"
    return f"{token_id[:6]}...{token_id[-4:]}" if len(token_id) > 12 else token_id
