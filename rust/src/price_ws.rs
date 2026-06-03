//! BTC 价格 WebSocket 长连接（事件驱动）+ REST 兜底。
//!
//! 主路径：Binance 公共镜像 aggTrade 流（全球可达，无需订阅消息）：
//!     wss://data-stream.binance.vision/ws/btcusdt@aggTrade
//! 每笔成交即推送，写入滑动窗口并 notify 主循环——价一动即可重算信号。
//!
//! 兜底：WS 长时间无数据时，按 PRICE_FEED(coinbase|binance) 用 REST 回填，
//! 保证 WS 断线期间信号仍有价可用。改编自 JY_RUST 的 feeds/binance.rs。

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use futures_util::StreamExt;
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tracing::{info, warn};

const BINANCE_WS: &str = "wss://data-stream.binance.vision/ws/btcusdt@aggTrade";
const HISTORY_SEC: i64 = 600;

#[derive(Clone)]
pub struct PriceFeed {
    history: Arc<Mutex<VecDeque<(i64, f64)>>>, // (ts_sec, price)
    last_ms: Arc<Mutex<i64>>,
    updated: Arc<tokio::sync::Notify>,
    rest_feed: String, // coinbase | binance
}

impl PriceFeed {
    pub fn new(rest_feed: &str) -> Self {
        Self {
            history: Arc::new(Mutex::new(VecDeque::new())),
            last_ms: Arc::new(Mutex::new(0)),
            updated: Arc::new(tokio::sync::Notify::new()),
            rest_feed: rest_feed.to_string(),
        }
    }

    pub fn updated_handle(&self) -> Arc<tokio::sync::Notify> {
        self.updated.clone()
    }

    pub fn latest_price(&self) -> Option<f64> {
        self.history.lock().unwrap().back().map(|(_, p)| *p)
    }

    /// 距某时间戳最近的价格（用作窗口起点参考价）。
    pub fn price_at(&self, target_ts: i64) -> Option<f64> {
        let h = self.history.lock().unwrap();
        h.iter().min_by_key(|(ts, _)| (ts - target_ts).abs()).map(|(_, p)| *p)
    }

    pub fn snapshot(&self) -> Vec<(i64, f64)> {
        self.history.lock().unwrap().iter().cloned().collect()
    }

    /// WS 最后更新距今毫秒数（用于判断 WS 是否过期 → 触发 REST 兜底）。
    pub fn age_ms(&self) -> i64 {
        let last = *self.last_ms.lock().unwrap();
        if last == 0 {
            i64::MAX
        } else {
            chrono::Utc::now().timestamp_millis() - last
        }
    }

    fn push(&self, ts: i64, price: f64) {
        let now = chrono::Utc::now().timestamp();
        let cutoff = now - HISTORY_SEC;
        {
            let mut h = self.history.lock().unwrap();
            h.push_back((ts, price));
            while h.front().map(|(t, _)| *t < cutoff).unwrap_or(false) {
                h.pop_front();
            }
        }
        *self.last_ms.lock().unwrap() = chrono::Utc::now().timestamp_millis();
        self.updated.notify_one();
    }

    /// 启动 WS 长连接（自动重连）。
    pub fn run(self) -> tokio::task::JoinHandle<()> {
        tokio::spawn(async move {
            loop {
                match self.connect_once().await {
                    Ok(()) => info!("[Price] WS 关闭，重连中..."),
                    Err(e) => warn!("[Price] WS 错误: {e}，3s 后重连"),
                }
                tokio::time::sleep(tokio::time::Duration::from_secs(3)).await;
            }
        })
    }

    async fn connect_once(&self) -> anyhow::Result<()> {
        let (ws, _) = connect_async(BINANCE_WS).await?;
        info!("[Price] connected aggTrade");
        let (_, mut read) = ws.split();
        while let Some(msg) = read.next().await {
            match msg? {
                Message::Text(t) => self.handle(&t),
                Message::Close(_) => break,
                _ => {}
            }
        }
        Ok(())
    }

    fn handle(&self, text: &str) {
        let Ok(v): Result<serde_json::Value, _> = serde_json::from_str(text) else {
            return;
        };
        let payload = v.get("data").unwrap_or(&v);
        let Some(price) = payload
            .get("p")
            .and_then(|x| x.as_str())
            .and_then(|s| s.parse::<f64>().ok())
        else {
            return;
        };
        let ts = payload
            .get("T")
            .and_then(|x| x.as_f64())
            .map(|ms| (ms / 1000.0) as i64)
            .unwrap_or_else(|| chrono::Utc::now().timestamp());
        self.push(ts, price);
    }

    /// REST 兜底回填（仅在 WS 过期时由主循环调用）。
    pub async fn rest_backfill(&self) {
        let candles = if self.rest_feed == "binance" {
            binance_klines().await
        } else {
            coinbase_candles().await
        };
        for (ts, price) in candles {
            self.push(ts, price);
        }
    }
}

async fn binance_klines() -> Vec<(i64, f64)> {
    let url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=10";
    let Ok(resp) = reqwest::get(url).await else { return vec![] };
    let Ok(v): Result<serde_json::Value, _> = resp.json().await else { return vec![] };
    let Some(arr) = v.as_array() else { return vec![] };
    arr.iter()
        .filter_map(|row| {
            let a = row.as_array()?;
            let ts = (a.get(6)?.as_i64()?) / 1000;
            let close = a.get(4)?.as_str()?.parse::<f64>().ok()?;
            Some((ts, close))
        })
        .collect()
}

async fn coinbase_candles() -> Vec<(i64, f64)> {
    let url = "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60";
    let client = reqwest::Client::builder()
        .user_agent("jybot-rs")
        .build()
        .unwrap_or_default();
    let Ok(resp) = client.get(url).send().await else { return vec![] };
    let Ok(v): Result<serde_json::Value, _> = resp.json().await else { return vec![] };
    let Some(arr) = v.as_array() else { return vec![] };
    let mut out: Vec<(i64, f64)> = arr
        .iter()
        .filter_map(|row| {
            let a = row.as_array()?;
            let ts = a.get(0)?.as_i64()?;
            let close = a.get(4)?.as_f64()?;
            Some((ts, close))
        })
        .collect();
    out.sort_by_key(|(ts, _)| *ts);
    out
}
