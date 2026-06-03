//! 策略主体：市场轮换 + 入场决策 + 持仓管理 + 结算。
//! 与 Python 版 jybot/engine.py 同口径。
//!
//! 盘口获取优先用 WS 缓存（事件驱动、零延迟）；当某 token 在缓存中缺失或
//! 数据过期（WS_STALENESS_SEC）时，自动回退 REST 拉取并回填缓存。

use std::collections::HashMap;
use std::sync::Arc;

use tokio::time::Instant;
use tracing::{info, warn};

use crate::clob::{dec_f64, BookCache, CachedBook, ClobClient, Market, OrderBook};
use crate::config::Config;
use crate::executor::Executor;
use crate::market_ws::MarketWs;
use crate::price_ws::PriceFeed;
use crate::signal;
use crate::state::{Position, TradeLog};

pub struct Strategy {
    cfg: Arc<Config>,
    clob: Arc<ClobClient>,
    cache: BookCache,
    ws: MarketWs,
    price: PriceFeed,
    exec: Arc<Executor>,
    log: TradeLog,

    current: Option<Market>,
    trades_in_market: u32,
    position: Option<Position>,
    throttle: HashMap<String, Instant>,
}

impl Strategy {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        cfg: Arc<Config>,
        clob: Arc<ClobClient>,
        cache: BookCache,
        ws: MarketWs,
        price: PriceFeed,
        exec: Arc<Executor>,
    ) -> Self {
        let log = TradeLog::load(&cfg.paper_trades_path);
        Self {
            cfg,
            clob,
            cache,
            ws,
            price,
            exec,
            log,
            current: None,
            trades_in_market: 0,
            position: None,
            throttle: HashMap::new(),
        }
    }

    fn now(&self) -> i64 {
        chrono::Utc::now().timestamp()
    }

    fn throttled(&mut self, key: &str, period_sec: f64) -> bool {
        let now = Instant::now();
        if let Some(last) = self.throttle.get(key) {
            if now.duration_since(*last).as_secs_f64() < period_sec {
                return true;
            }
        }
        self.throttle.insert(key.to_string(), now);
        false
    }

    /// 盘口：先看 WS 缓存（新鲜则用），否则回退 REST 并回填缓存。
    async fn book_for(&self, token_id: &str) -> Option<OrderBook> {
        let now_ms = chrono::Utc::now().timestamp_millis();
        {
            let cache = self.cache.read().await;
            if let Some(entry) = cache.get(token_id) {
                if now_ms - entry.ts_ms < self.cfg.ws_staleness_sec * 1000 {
                    return Some(entry.book.clone());
                }
            }
        }
        // 过期或缺失 → REST 兜底
        match self.clob.fetch_book(token_id).await {
            Ok(book) => {
                let mut cache = self.cache.write().await;
                cache.insert(token_id.to_string(), CachedBook { book: book.clone(), ts_ms: now_ms });
                Some(book)
            }
            Err(e) => {
                warn!("[book] REST 兜底失败 {token_id}: {e}");
                None
            }
        }
    }

    fn tp_price(&self, entry: f64) -> f64 {
        (entry + self.cfg.take_profit_pct * (1.0 - entry)).min(0.99)
    }
    fn sl_price(&self, entry: f64) -> f64 {
        (entry - self.cfg.stop_loss_pct * entry).max(0.01)
    }

    pub async fn run_once(&mut self) -> anyhow::Result<()> {
        // ── 市场轮换 ──
        let need_refresh = match &self.current {
            None => true,
            Some(m) => self.now() >= m.end_ts,
        };
        if need_refresh {
            if let Some(m) = self.clob.find_current_market().await {
                let is_new = self.current.as_ref().map(|c| c.slug != m.slug).unwrap_or(true);
                if is_new {
                    self.on_new_market(&m).await;
                }
                self.current = Some(m);
            } else {
                if self.throttled("nomarket", 20.0) {
                    return Ok(());
                }
                info!("[wait] 当前无活跃 BTC {} 市场，扫描中...", self.cfg.market_interval);
                return Ok(());
            }
        }

        // ── 持仓管理 ──
        if self.position.is_some() {
            self.manage_position().await?;
        }

        // ── 入场评估 ──
        if self.position.is_none() {
            // clone 出当前市场避免借用冲突
            if let Some(market) = self.current.clone() {
                self.evaluate_entry(&market).await?;
            }
        }
        Ok(())
    }

    async fn on_new_market(&mut self, m: &Market) {
        self.trades_in_market = 0;
        let up = m.token_for("Up").unwrap_or("N/A");
        let down = m.token_for("Down").unwrap_or("N/A");
        // 订阅当前盘两个 token 的盘口 WS
        let tokens: Vec<String> = m.token_ids.clone();
        self.ws.ensure_subscribed(&tokens).await;
        info!("{}", "=".repeat(60));
        info!("[market] ACTIVE: {}", m.slug);
        info!("[market] {}", m.title);
        info!("[market] UP={} DOWN={}", short(up), short(down));
        info!(
            "[market] settle in {}s  min_size={} tick={}",
            m.seconds_left(),
            m.order_min_size,
            m.tick_size
        );
        info!("{}", "=".repeat(60));
    }

    async fn evaluate_entry(&mut self, market: &Market) -> anyhow::Result<()> {
        let secs_left = market.seconds_left();
        let secs_since = market.seconds_since_start();

        if secs_left <= self.cfg.late_entry_cutoff_sec {
            if !self.throttled(&format!("late:{}", market.slug), 15.0) {
                info!(
                    "[skip] {}: 距结算 {}s <= LATE_ENTRY_CUTOFF_SEC={}",
                    market.slug, secs_left, self.cfg.late_entry_cutoff_sec
                );
            }
            return Ok(());
        }
        if secs_since < self.cfg.early_entry_cutoff_sec {
            return Ok(());
        }
        if self.trades_in_market >= self.cfg.max_trades_per_market {
            return Ok(());
        }

        let sig = signal::compute(&self.price, market.start_ts, self.cfg.signal_lookback_min);
        let Some(direction) = sig.direction else {
            if !self.throttled(&format!("nosig:{}", market.slug), 15.0) {
                info!("[skip] {}: {}", market.slug, sig.reason);
            }
            return Ok(());
        };

        let outcome = if direction == "UP" { "Up" } else { "Down" };
        let Some(token_id) = market.token_for(outcome).map(|s| s.to_string()) else {
            return Ok(());
        };

        let Some(book) = self.book_for(&token_id).await else {
            if !self.throttled(&format!("nobook:{}", market.slug), 15.0) {
                warn!("[skip] {} {}: 盘口为空/拉取失败", market.slug, outcome);
            }
            return Ok(());
        };
        let Some(ask) = book.best_ask().map(dec_f64) else {
            return Ok(());
        };

        let p_side = if direction == "UP" { sig.p_up } else { sig.p_down() };
        let edge = p_side - ask;
        let banner = format!(
            "[signal] {} | {} token={} | {} | ask={:.3} p({})={:.3} edge={:+.3}",
            market.slug, outcome, short(&token_id), sig.reason, ask, direction, p_side, edge
        );

        // 过滤
        let mut reasons = Vec::new();
        if !(self.cfg.min_entry_price <= ask && ask <= self.cfg.max_entry_price) {
            reasons.push(format!(
                "价格 {:.3} 不在 [{}, {}]",
                ask, self.cfg.min_entry_price, self.cfg.max_entry_price
            ));
        }
        if let Some(sp) = book.spread_pct() {
            if sp > self.cfg.max_spread_pct {
                reasons.push(format!("点差 {:.3} > {}", sp, self.cfg.max_spread_pct));
            }
        }
        if edge < self.cfg.min_ml_edge {
            reasons.push(format!("edge {:+.3} < MIN_ML_EDGE {}", edge, self.cfg.min_ml_edge));
        }
        let size = self.cfg.fixed_shares;
        if size < dec_f64(market.order_min_size) {
            reasons.push(format!("FIXED_SHARES {} < 最小 {}", size, market.order_min_size));
        }
        let notional = size * ask;
        if notional > self.cfg.max_position_usdc {
            reasons.push(format!(
                "名义 ${:.2} > MAX_POSITION_USDC ${}",
                notional, self.cfg.max_position_usdc
            ));
        }

        if !reasons.is_empty() {
            if !self.throttled(&format!("reject:{}", market.slug), self.cfg.poll_interval_sec * 3.0) {
                info!("{}", banner);
                info!("[no-trade] {}: {}", market.slug, reasons.join(" ; "));
            }
            return Ok(());
        }

        info!("{}", banner);
        info!(
            "[ENTER] {} BUY {} size={} (notional~${:.2}) {} mode={}",
            market.slug,
            outcome,
            size,
            notional,
            self.cfg.order_type,
            if self.cfg.can_trade_live() { "LIVE" } else { "PAPER" }
        );

        let result = self.exec.place_buy(&token_id, outcome, &book, size).await;
        if !result.is_filled() {
            warn!("[ENTER] {} 未成交: {} ({})", market.slug, result.status, result.reason);
            return Ok(());
        }

        self.trades_in_market += 1;
        let pos = Position {
            market_slug: market.slug.clone(),
            condition_id: market.condition_id.clone(),
            token_id: token_id.clone(),
            outcome: outcome.to_string(),
            direction: direction.to_string(),
            entry_price: result.avg_price,
            size: result.filled_size,
            entry_ts: self.now(),
            settle_ts: market.end_ts,
            p_model: p_side,
            ref_price: sig.reference.unwrap_or(0.0),
            status: "OPEN".to_string(),
            exit_price: None,
            exit_reason: String::new(),
            outcome_result: String::new(),
            pnl_usd: 0.0,
        };
        self.log.append(pos.to_record());
        let tp = self.tp_price(result.avg_price);
        info!(
            "[POSITION] OPEN {} {}@{:.3} TP={:.3} SL={}",
            outcome,
            result.filled_size,
            result.avg_price,
            tp,
            if self.cfg.enable_stop_loss {
                format!("{:.3}", self.sl_price(result.avg_price))
            } else {
                "off".to_string()
            }
        );
        self.position = Some(pos);
        Ok(())
    }

    async fn manage_position(&mut self) -> anyhow::Result<()> {
        let pos = self.position.clone().unwrap();
        // 结算
        if self.now() >= pos.settle_ts {
            self.settle_position(pos).await;
            return Ok(());
        }
        let Some(book) = self.book_for(&pos.token_id).await else {
            return Ok(());
        };
        let Some(bid) = book.best_bid().map(dec_f64) else {
            return Ok(());
        };
        let tp = self.tp_price(pos.entry_price);
        if bid >= tp {
            self.exit_position(pos, &book, format!("止盈 (bid {:.3} >= TP {:.3})", bid, tp)).await;
        } else if self.cfg.enable_stop_loss {
            let sl = self.sl_price(pos.entry_price);
            if bid <= sl {
                self.exit_position(pos, &book, format!("止损 (bid {:.3} <= SL {:.3})", bid, sl)).await;
            }
        }
        Ok(())
    }

    async fn exit_position(&mut self, mut pos: Position, book: &OrderBook, reason: String) {
        let result = self
            .exec
            .place_sell(&pos.token_id, &pos.outcome, book, pos.size, pos.size)
            .await;
        let exit_price = if result.is_filled() {
            result.avg_price
        } else {
            book.best_bid().map(dec_f64).unwrap_or(pos.entry_price)
        };
        let pnl = (exit_price - pos.entry_price) * pos.size;
        pos.status = "CLOSED".into();
        pos.exit_price = Some(exit_price);
        pos.exit_reason = reason.clone();
        pos.outcome_result = if pnl >= 0.0 { "WIN".into() } else { "LOSS".into() };
        pos.pnl_usd = pnl;
        self.log.update_last_open(&pos.token_id, pos.to_record());
        info!(
            "[EXIT] {} {} {} -> {} pnl=${:+.4}",
            pos.market_slug, pos.outcome, reason, pos.outcome_result, pnl
        );
        self.position = None;
    }

    async fn settle_position(&mut self, mut pos: Position) {
        // 优先 Gamma 结算结果
        let resolved: Option<f64> = match self.clob.fetch_winning_outcome(&pos.market_slug).await {
            Some(winner) => Some(if winner.eq_ignore_ascii_case(&pos.outcome) { 1.0 } else { 0.0 }),
            None => {
                // 兜底：现价 vs 窗口起点参考价
                match (self.price.latest_price(), pos.ref_price) {
                    (Some(spot), r) if r > 0.0 => {
                        let up_won = spot > r;
                        let won = (pos.direction == "UP" && up_won)
                            || (pos.direction == "DOWN" && !up_won);
                        Some(if won { 1.0 } else { 0.0 })
                    }
                    _ => None,
                }
            }
        };
        let value = resolved.unwrap_or(pos.entry_price);
        let pnl = (value - pos.entry_price) * pos.size;
        pos.status = "CLOSED".into();
        pos.exit_price = Some(value);
        pos.exit_reason = if resolved.is_some() {
            "settled (resolution)".into()
        } else {
            "settled (unresolved-mark)".into()
        };
        pos.outcome_result = if value >= 0.5 { "WIN".into() } else { "LOSS".into() };
        pos.pnl_usd = pnl;
        self.log.update_last_open(&pos.token_id, pos.to_record());
        info!(
            "[SETTLE] {} {} value={:.2} -> {} pnl=${:+.4}",
            pos.market_slug, pos.outcome, value, pos.outcome_result, pnl
        );
        self.position = None;
    }

    pub fn print_summary(&self) {
        let s = self.log.summary();
        info!("{}", "=".repeat(60));
        info!(
            "[summary] trades={} settled={} win_rate={:.1}% pnl=${:+.4}",
            s.trades, s.settled, s.win_rate, s.pnl_usd
        );
        info!("[summary] 纸面记录: {}", self.cfg.paper_trades_path.display());
        info!("{}", "=".repeat(60));
    }
}

fn short(token: &str) -> String {
    if token.len() > 12 {
        format!("{}..{}", &token[..6], &token[token.len() - 4..])
    } else {
        token.to_string()
    }
}
