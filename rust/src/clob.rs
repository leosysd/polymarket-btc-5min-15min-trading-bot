//! CLOB / Gamma 类型 + REST 备用 + 盘口缓存。
//!
//! 行情主路径是 WebSocket（见 market_ws.rs）；这里的 REST 仅在 WS 尚无该 token
//! 数据、或缓存数据过期（WS_STALENESS_SEC）时作为兜底，以及用于市场发现 / 结算查询。

use anyhow::{bail, Result};
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use std::collections::HashMap;
use std::str::FromStr;
use std::sync::Arc;
use tokio::sync::RwLock;

#[derive(Debug, Clone)]
pub struct Market {
    pub slug: String,
    pub title: String,
    pub condition_id: String,
    pub start_ts: i64,
    pub end_ts: i64,
    pub outcomes: Vec<String>,
    pub token_ids: Vec<String>,
    pub order_min_size: Decimal,
    pub tick_size: Decimal,
}

impl Market {
    pub fn seconds_left(&self) -> i64 {
        (self.end_ts - chrono::Utc::now().timestamp()).max(0)
    }
    pub fn seconds_since_start(&self) -> i64 {
        (chrono::Utc::now().timestamp() - self.start_ts).max(0)
    }
    /// 返回 (up_token, down_token)
    pub fn token_for(&self, outcome: &str) -> Option<&str> {
        self.outcomes
            .iter()
            .position(|o| o.eq_ignore_ascii_case(outcome))
            .and_then(|i| self.token_ids.get(i))
            .map(|s| s.as_str())
    }
}

#[derive(Debug, Clone, Default)]
pub struct OrderBook {
    pub asks: Vec<(Decimal, Decimal)>, // (price, size) 升序
    pub bids: Vec<(Decimal, Decimal)>, // 降序
    pub tick_size: Decimal,
    pub min_order_size: Decimal,
}

impl OrderBook {
    pub fn best_ask(&self) -> Option<Decimal> {
        self.asks.first().map(|(p, _)| *p)
    }
    pub fn best_ask_size(&self) -> Option<Decimal> {
        self.asks.first().map(|(_, s)| *s)
    }
    pub fn best_bid(&self) -> Option<Decimal> {
        self.bids.first().map(|(p, _)| *p)
    }
    pub fn mid(&self) -> Option<f64> {
        match (self.best_ask(), self.best_bid()) {
            (Some(a), Some(b)) => Some((dec_f64(a) + dec_f64(b)) / 2.0),
            _ => None,
        }
    }
    pub fn spread_pct(&self) -> Option<f64> {
        match (self.best_ask(), self.best_bid(), self.mid()) {
            (Some(a), Some(b), Some(m)) if m > 0.0 => Some((dec_f64(a) - dec_f64(b)) / m),
            _ => None,
        }
    }
}

pub fn dec_f64(d: Decimal) -> f64 {
    d.to_f64().unwrap_or(0.0)
}

/// 缓存条目：盘口 + 最后更新时间（毫秒），用于判断 WS 数据是否过期。
#[derive(Debug, Clone)]
pub struct CachedBook {
    pub book: OrderBook,
    pub ts_ms: i64,
}

pub type BookCache = Arc<RwLock<HashMap<String, CachedBook>>>;

pub fn new_book_cache() -> BookCache {
    Arc::new(RwLock::new(HashMap::new()))
}

pub struct ClobClient {
    http: reqwest::Client,
    pub clob_api: String,
    pub gamma_api: String,
    pub slug_prefix: String,
    pub interval_seconds: i64,
}

impl ClobClient {
    pub fn new(clob_api: &str, gamma_api: &str, slug_prefix: &str, interval_seconds: i64) -> Self {
        let http = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(10))
            .build()
            .expect("build http client");
        Self {
            http,
            clob_api: clob_api.to_string(),
            gamma_api: gamma_api.to_string(),
            slug_prefix: slug_prefix.to_string(),
            interval_seconds,
        }
    }

    /// 动态发现当前活跃市场（无任何硬编码 token_id）。
    pub async fn find_current_market(&self) -> Option<Market> {
        let now = chrono::Utc::now().timestamp();
        let iv = self.interval_seconds;
        let slot = (now / iv) * iv;
        for cand in [slot, slot - iv, slot + iv] {
            if let Some(m) = self.fetch_market(cand).await {
                if m.start_ts <= now && now < m.end_ts {
                    return Some(m);
                }
            }
        }
        None
    }

    /// 列出当前及后续若干个市场（用于启动时展示发现能力）。
    pub async fn upcoming_markets(&self, count: i64) -> Vec<Market> {
        let now = chrono::Utc::now().timestamp();
        let iv = self.interval_seconds;
        let slot = (now / iv) * iv;
        let mut out = Vec::new();
        for i in 0..count + 1 {
            let cand = slot + i * iv;
            if let Some(m) = self.fetch_market(cand).await {
                if m.end_ts > now {
                    out.push(m);
                }
            }
        }
        out
    }

    async fn fetch_market(&self, start_ts: i64) -> Option<Market> {
        let slug = format!("{}-{}", self.slug_prefix, start_ts);
        let url = format!("{}/markets?slug={}", self.gamma_api, slug);
        let resp: serde_json::Value = self.http.get(&url).send().await.ok()?.json().await.ok()?;
        let market = resp.as_array()?.first()?;

        if market.get("acceptingOrders").and_then(|v| v.as_bool()) != Some(true) {
            return None;
        }
        if market.get("closed").and_then(|v| v.as_bool()) == Some(true) {
            return None;
        }

        let outcomes = parse_json_list(market.get("outcomes")?)?;
        let token_ids = parse_json_list(market.get("clobTokenIds")?)?;
        if outcomes.len() != token_ids.len() || token_ids.is_empty() {
            return None;
        }
        let has_up = outcomes.iter().any(|o| o.eq_ignore_ascii_case("Up"));
        let has_down = outcomes.iter().any(|o| o.eq_ignore_ascii_case("Down"));
        if !has_up || !has_down {
            return None;
        }

        let dec = |key: &str, default: Decimal| -> Decimal {
            market
                .get(key)
                .and_then(num_to_decimal)
                .unwrap_or(default)
        };

        Some(Market {
            slug: slug.clone(),
            title: market
                .get("question")
                .and_then(|v| v.as_str())
                .unwrap_or(&slug)
                .to_string(),
            condition_id: market
                .get("conditionId")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            start_ts,
            end_ts: start_ts + self.interval_seconds,
            outcomes,
            token_ids,
            order_min_size: dec("orderMinSize", Decimal::new(5, 0)),
            tick_size: dec("orderPriceMinTickSize", Decimal::new(1, 2)),
        })
    }

    /// REST 兜底拉单个 token 盘口。
    pub async fn fetch_book(&self, token_id: &str) -> Result<OrderBook> {
        let url = format!("{}/book?token_id={}", self.clob_api, token_id);
        let resp: serde_json::Value = self.http.get(&url).send().await?.json().await?;
        if resp.get("error").is_some() {
            bail!("book error for {}: {}", token_id, resp);
        }
        Ok(parse_book(&resp))
    }

    /// 结算查询：返回获胜方向（"Up"/"Down"），未结算返回 None。
    pub async fn fetch_winning_outcome(&self, slug: &str) -> Option<String> {
        let url = format!("{}/markets?slug={}", self.gamma_api, slug);
        let resp: serde_json::Value = self.http.get(&url).send().await.ok()?.json().await.ok()?;
        let market = resp.as_array()?.first()?;
        if market.get("closed").and_then(|v| v.as_bool()) != Some(true) {
            return None;
        }
        let outcomes = parse_json_list(market.get("outcomes")?)?;
        let prices = parse_json_list(market.get("outcomePrices")?)?;
        for (o, p) in outcomes.iter().zip(prices.iter()) {
            if let Ok(pd) = Decimal::from_str(p) {
                if pd >= Decimal::new(99, 2) {
                    return Some(o.clone());
                }
            }
        }
        None
    }
}

fn num_to_decimal(v: &serde_json::Value) -> Option<Decimal> {
    match v {
        serde_json::Value::String(s) => Decimal::from_str(s).ok(),
        serde_json::Value::Number(n) => Decimal::from_str(&n.to_string()).ok(),
        _ => None,
    }
}

/// Gamma 的数组字段常是 JSON 字符串（如 "[\"1\",\"2\"]"），需二次解析。
pub fn parse_json_list(v: &serde_json::Value) -> Option<Vec<String>> {
    match v {
        serde_json::Value::Array(arr) => Some(
            arr.iter()
                .filter_map(|x| x.as_str().map(|s| s.to_string()))
                .collect(),
        ),
        serde_json::Value::String(s) => serde_json::from_str::<Vec<String>>(s).ok().or_else(|| {
            let t = s.trim_matches(|c| c == '[' || c == ']');
            Some(
                t.split(',')
                    .map(|p| p.trim().trim_matches('"').to_string())
                    .filter(|s| !s.is_empty())
                    .collect(),
            )
        }),
        _ => None,
    }
}

/// 解析盘口 JSON（WS book 事件与 REST /book 同结构）。asks 升序、bids 降序。
pub fn parse_book(v: &serde_json::Value) -> OrderBook {
    let parse_levels = |arr: Option<&serde_json::Value>| -> Vec<(Decimal, Decimal)> {
        let Some(serde_json::Value::Array(levels)) = arr else {
            return vec![];
        };
        let mut out: Vec<(Decimal, Decimal)> = levels
            .iter()
            .filter_map(|l| {
                let price = Decimal::from_str(l.get("price")?.as_str()?).ok()?;
                let size = Decimal::from_str(l.get("size")?.as_str()?).ok()?;
                Some((price, size))
            })
            .collect();
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out
    };

    let mut bids = parse_levels(v.get("bids"));
    bids.sort_by(|a, b| b.0.cmp(&a.0));

    OrderBook {
        asks: parse_levels(v.get("asks")),
        bids,
        tick_size: v
            .get("tick_size")
            .and_then(|x| x.as_str())
            .and_then(|s| Decimal::from_str(s).ok())
            .unwrap_or(Decimal::new(1, 2)),
        min_order_size: v
            .get("min_order_size")
            .and_then(|x| x.as_str())
            .and_then(|s| Decimal::from_str(s).ok())
            .unwrap_or(Decimal::new(5, 0)),
    }
}
