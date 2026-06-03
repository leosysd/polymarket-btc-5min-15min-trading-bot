//! Polymarket 盘口 WebSocket 长连接。
//!
//!   wss://ws-subscriptions-clob.polymarket.com/ws/market
//!
//! 订阅当前盘的 2 个 token（Up/Down）。服务端推 `book`（全量快照）与
//! `price_change`（增量）。每批更新写入带时间戳的缓存，并 notify 主循环——
//! 盘口一变即事件驱动决策（替代轮询）。订阅集变化触发重连（只订当前盘 token，
//! 避免 token 只增不减累积压垮连接）。
//!
//! 改编自 leosysd/JY_RUST 的 ws.rs 架构。

use crate::clob::{parse_book, BookCache, CachedBook, OrderBook};
use futures_util::{SinkExt, StreamExt};
use rust_decimal::Decimal;
use serde_json::json;
use std::collections::HashSet;
use std::str::FromStr;
use std::sync::Arc;
use tokio::sync::Mutex;
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tracing::{info, warn};

struct Inner {
    url: String,
    subscribed: Mutex<HashSet<String>>,
    cache: BookCache,
    reconnect: tokio::sync::Notify,
    book_updated: tokio::sync::Notify,
}

#[derive(Clone)]
pub struct MarketWs(Arc<Inner>);

impl MarketWs {
    pub fn new(url: &str, cache: BookCache) -> Self {
        Self(Arc::new(Inner {
            url: url.to_string(),
            subscribed: Mutex::new(HashSet::new()),
            cache,
            reconnect: tokio::sync::Notify::new(),
            book_updated: tokio::sync::Notify::new(),
        }))
    }

    /// 主循环 await 此方法，盘口一更新即被唤醒。
    pub async fn wait_book_update(&self) {
        self.0.book_updated.notified().await;
    }

    /// 替换订阅集为当前盘 token（不累加）。变化则触发重连。
    pub async fn ensure_subscribed(&self, token_ids: &[String]) {
        let new_set: HashSet<String> = token_ids.iter().cloned().collect();
        let mut sub = self.0.subscribed.lock().await;
        if *sub != new_set {
            *sub = new_set;
            drop(sub);
            self.0.reconnect.notify_one();
        }
    }

    pub fn run(&self) -> tokio::task::JoinHandle<()> {
        let ws = self.clone();
        tokio::spawn(async move {
            loop {
                match ws.connect_once().await {
                    Ok(true) => info!("[WS] 订阅变化，重连"),
                    Ok(false) => {
                        info!("[WS] 连接关闭，3s 后重连");
                        tokio::time::sleep(tokio::time::Duration::from_secs(3)).await;
                    }
                    Err(e) => {
                        warn!("[WS] 错误: {e}，3s 后重连");
                        tokio::time::sleep(tokio::time::Duration::from_secs(3)).await;
                    }
                }
            }
        })
    }

    async fn connect_once(&self) -> anyhow::Result<bool> {
        let (ws_stream, _) = connect_async(&self.0.url).await?;
        info!("[WS] connected {}", self.0.url);
        let (mut write, mut read) = ws_stream.split();

        let assets: Vec<String> = self.0.subscribed.lock().await.iter().cloned().collect();
        if !assets.is_empty() {
            let msg = json!({ "assets_ids": assets, "type": "market" });
            write
                .send(Message::Text(msg.to_string().into()))
                .await
                .map_err(|e| anyhow::anyhow!("ws send: {e}"))?;
            info!("[WS] subscribed {} tokens", assets.len());
        }

        loop {
            tokio::select! {
                _ = self.0.reconnect.notified() => return Ok(true),
                msg_opt = read.next() => {
                    let Some(msg) = msg_opt else { return Ok(false); };
                    match msg? {
                        Message::Text(text) => self.handle_message(&text).await,
                        Message::Ping(data) => { write.send(Message::Pong(data)).await.ok(); }
                        Message::Close(_) => return Ok(false),
                        _ => {}
                    }
                }
            }
        }
    }

    async fn handle_message(&self, text: &str) {
        let Ok(data): Result<serde_json::Value, _> = serde_json::from_str(text) else {
            return;
        };
        let events: &[serde_json::Value] = match &data {
            serde_json::Value::Array(arr) => arr.as_slice(),
            serde_json::Value::Object(_) => std::slice::from_ref(&data),
            _ => return,
        };

        let now_ms = chrono::Utc::now().timestamp_millis();
        let mut touched = false;
        {
            let mut cache = self.0.cache.write().await;
            for event in events {
                let ev = event.get("event_type").and_then(|v| v.as_str()).unwrap_or("");
                let Some(asset) = event.get("asset_id").and_then(|v| v.as_str()) else {
                    continue;
                };
                if ev == "book" {
                    cache.insert(
                        asset.to_string(),
                        CachedBook { book: parse_book(event), ts_ms: now_ms },
                    );
                    touched = true;
                } else if ev == "price_change" {
                    if let Some(entry) = cache.get_mut(asset) {
                        apply_price_change(&mut entry.book, event);
                        entry.ts_ms = now_ms;
                        touched = true;
                    }
                }
            }
        }
        if touched {
            self.0.book_updated.notify_one();
        }
    }
}

/// 增量更新：用事件里的 asks/bids 覆盖缓存对应层级。
fn apply_price_change(book: &mut OrderBook, event: &serde_json::Value) {
    let parse_levels = |arr: &serde_json::Value| -> Vec<(Decimal, Decimal)> {
        arr.as_array()
            .map(|a| {
                a.iter()
                    .filter_map(|l| {
                        let p = Decimal::from_str(l.get("price")?.as_str()?).ok()?;
                        let s = Decimal::from_str(l.get("size")?.as_str()?).ok()?;
                        Some((p, s))
                    })
                    .collect()
            })
            .unwrap_or_default()
    };

    if let Some(asks) = event.get("asks") {
        for (price, size) in parse_levels(asks) {
            book.asks.retain(|(p, _)| *p != price);
            if size > Decimal::ZERO {
                book.asks.push((price, size));
            }
        }
        book.asks.sort_by(|a, b| a.0.cmp(&b.0));
    }
    if let Some(bids) = event.get("bids") {
        for (price, size) in parse_levels(bids) {
            book.bids.retain(|(p, _)| *p != price);
            if size > Decimal::ZERO {
                book.bids.push((price, size));
            }
        }
        book.bids.sort_by(|a, b| b.0.cmp(&a.0));
    }
}
