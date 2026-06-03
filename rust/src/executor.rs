//! 下单执行器。
//!
//! * DRY-RUN（默认）：不签名、不下真实单。按实时盘口模拟成交，纸面 PnL 真实。
//! * LIVE：把订单交给 Python 助手 `scripts/place_order.py`（使用官方
//!   py-clob-client 完成 EIP-712 签名与提交）。行情/盘口判断全在 Rust 走 WS，
//!   仅"真实提交订单"这一步复用官方 SDK——符合需求。
//!
//! 下单规则（与 Python 版一致）：
//!   BUY  价 = best_ask × (1 + SLIPPAGE_BPS)，按 tick 向上取整
//!   SELL 价 = best_bid × (1 - SLIPPAGE_BPS)，按 tick 向下取整，size ≤ 持仓
//!   FOK 全成或撤 / FAK 部分成交后撤 / GTC 剩余挂单

use crate::clob::{dec_f64, OrderBook};
use crate::config::Config;
use serde_json::json;
use std::sync::Arc;
use tracing::{error, info};

pub struct OrderResult {
    pub status: String, // FILLED | PARTIAL | CANCELLED | REJECTED | FAILED | RESTING
    pub side: String,
    pub filled_size: f64,
    pub avg_price: f64,
    pub limit_price: f64,
    pub reason: String,
}

impl OrderResult {
    pub fn is_filled(&self) -> bool {
        matches!(self.status.as_str(), "FILLED" | "PARTIAL") && self.filled_size > 0.0
    }
    fn rejected(side: &str, reason: &str) -> Self {
        OrderResult {
            status: "REJECTED".into(),
            side: side.into(),
            filled_size: 0.0,
            avg_price: 0.0,
            limit_price: 0.0,
            reason: reason.into(),
        }
    }
}

fn round_tick(price: f64, tick: f64, up: bool) -> f64 {
    let tick = if tick <= 0.0 { 0.01 } else { tick };
    let steps = price / tick;
    let r = if up { steps.ceil() } else { steps.floor() } * tick;
    r.clamp(tick, 1.0 - tick)
}

pub fn buy_limit_price(book: &OrderBook, slip: f64) -> Option<f64> {
    let ask = dec_f64(book.best_ask()?);
    Some(round_tick(ask * (1.0 + slip), dec_f64(book.tick_size), true))
}

pub fn sell_limit_price(book: &OrderBook, slip: f64) -> Option<f64> {
    let bid = dec_f64(book.best_bid()?);
    Some(round_tick(bid * (1.0 - slip), dec_f64(book.tick_size), false))
}

/// 沿盘口逐档撮合，返回 (成交量, 均价)。
fn simulate_fill(book: &OrderBook, side: &str, size: f64, limit: f64) -> (f64, f64) {
    let mut filled = 0.0;
    let mut notional = 0.0;
    if side == "BUY" {
        for (p, s) in &book.asks {
            let (p, s) = (dec_f64(*p), dec_f64(*s));
            if p > limit + 1e-9 {
                break;
            }
            let take = (size - filled).min(s);
            if take <= 0.0 {
                break;
            }
            filled += take;
            notional += take * p;
            if filled >= size - 1e-9 {
                break;
            }
        }
    } else {
        for (p, s) in &book.bids {
            let (p, s) = (dec_f64(*p), dec_f64(*s));
            if p < limit - 1e-9 {
                break;
            }
            let take = (size - filled).min(s);
            if take <= 0.0 {
                break;
            }
            filled += take;
            notional += take * p;
            if filled >= size - 1e-9 {
                break;
            }
        }
    }
    let avg = if filled > 0.0 { notional / filled } else { limit };
    (filled, avg)
}

fn resolve_sim_status(order_type: &str, size: f64, filled: f64) -> (&'static str, &'static str) {
    let full = filled >= size - 1e-9;
    match order_type {
        "FOK" => {
            if full {
                ("FILLED", "fully filled")
            } else {
                ("CANCELLED", "FOK: insufficient liquidity")
            }
        }
        "FAK" => {
            if full {
                ("FILLED", "fully filled")
            } else if filled > 0.0 {
                ("PARTIAL", "FAK: partial fill, remainder cancelled")
            } else {
                ("CANCELLED", "FAK: nothing marketable")
            }
        }
        _ => {
            if full {
                ("FILLED", "fully filled")
            } else if filled > 0.0 {
                ("PARTIAL", "GTC: partial fill, remainder rests")
            } else {
                ("RESTING", "GTC: rests on book")
            }
        }
    }
}

pub struct Executor {
    cfg: Arc<Config>,
}

impl Executor {
    pub fn new(cfg: Arc<Config>) -> Self {
        Self { cfg }
    }

    pub async fn place_buy(&self, token_id: &str, outcome: &str, book: &OrderBook, size: f64) -> OrderResult {
        let Some(price) = buy_limit_price(book, self.cfg.slippage_frac()) else {
            return OrderResult::rejected("BUY", "empty book (no ask)");
        };
        self.place("BUY", token_id, outcome, book, size, price).await
    }

    pub async fn place_sell(
        &self,
        token_id: &str,
        outcome: &str,
        book: &OrderBook,
        size: f64,
        position: f64,
    ) -> OrderResult {
        let size = size.min(position); // SELL 不超持仓
        if size <= 0.0 {
            return OrderResult::rejected("SELL", "no position to sell");
        }
        let Some(price) = sell_limit_price(book, self.cfg.slippage_frac()) else {
            return OrderResult::rejected("SELL", "empty book (no bid)");
        };
        self.place("SELL", token_id, outcome, book, size, price).await
    }

    async fn place(
        &self,
        side: &str,
        token_id: &str,
        outcome: &str,
        book: &OrderBook,
        size: f64,
        price: f64,
    ) -> OrderResult {
        if self.cfg.can_trade_live() {
            self.place_live(side, token_id, size, price).await
        } else {
            self.place_paper(side, outcome, book, size, price)
        }
    }

    fn place_paper(&self, side: &str, outcome: &str, book: &OrderBook, size: f64, price: f64) -> OrderResult {
        let (mut filled, mut avg) = simulate_fill(book, side, size, price);
        let (status, reason) = resolve_sim_status(&self.cfg.order_type, size, filled);
        if matches!(status, "CANCELLED" | "REJECTED") {
            filled = 0.0;
            avg = price;
        }
        info!(
            "[paper] {} {} size={} @ {:.3} ({}) -> {} filled={:.2} avg={:.3}",
            side, outcome, size, price, self.cfg.order_type, status, filled, avg
        );
        OrderResult {
            status: status.into(),
            side: side.into(),
            filled_size: filled,
            avg_price: avg,
            limit_price: price,
            reason: reason.into(),
        }
    }

    /// LIVE：调用 Python 官方 SDK 助手下单。
    async fn place_live(&self, side: &str, token_id: &str, size: f64, price: f64) -> OrderResult {
        let payload = json!({
            "token_id": token_id,
            "side": side,
            "size": size,
            "price": price,
            "order_type": self.cfg.order_type,
        });
        let mut cmd = tokio::process::Command::new(&self.cfg.python_bin);
        cmd.arg(&self.cfg.place_order_script);
        cmd.stdin(std::process::Stdio::piped());
        cmd.stdout(std::process::Stdio::piped());
        cmd.stderr(std::process::Stdio::piped());

        let child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                error!("[live] 无法启动下单助手: {e}");
                return OrderResult {
                    status: "FAILED".into(),
                    side: side.into(),
                    filled_size: 0.0,
                    avg_price: 0.0,
                    limit_price: price,
                    reason: format!("spawn place_order.py failed: {e}"),
                };
            }
        };

        use tokio::io::AsyncWriteExt;
        let mut child = child;
        if let Some(mut stdin) = child.stdin.take() {
            let _ = stdin.write_all(payload.to_string().as_bytes()).await;
            let _ = stdin.shutdown().await;
        }
        let out = match child.wait_with_output().await {
            Ok(o) => o,
            Err(e) => {
                return OrderResult {
                    status: "FAILED".into(),
                    side: side.into(),
                    filled_size: 0.0,
                    avg_price: 0.0,
                    limit_price: price,
                    reason: format!("place_order.py error: {e}"),
                }
            }
        };
        let stdout = String::from_utf8_lossy(&out.stdout);
        info!("[live] {} {} size={} @ {:.3} -> {}", side, token_id, size, price, stdout.trim());

        let parsed: serde_json::Value = serde_json::from_str(stdout.trim()).unwrap_or(json!({}));
        let status = parsed.get("status").and_then(|v| v.as_str()).unwrap_or("FAILED");
        let filled = parsed.get("filled_size").and_then(|v| v.as_f64()).unwrap_or(0.0);
        let avg = parsed.get("avg_price").and_then(|v| v.as_f64()).unwrap_or(price);
        OrderResult {
            status: status.to_string(),
            side: side.into(),
            filled_size: filled,
            avg_price: avg,
            limit_price: price,
            reason: parsed.get("reason").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        }
    }
}
