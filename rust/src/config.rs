//! 配置加载：从当前项目的 `.env` 读取（变量名与 Python 版保持一致）。
//!
//! 安全模型：真钱下单需要三把锁同时满足——
//!   1) 启动时传 `--live`
//!   2) `.env` 中 `DRY_RUN=false`
//!   3) `.env` 中 `LIVE_TRADING=true`
//! 任一不满足即为纸面模拟。`can_trade_live()` 表达后两把锁。

use anyhow::{bail, Result};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct Config {
    // 市场周期
    pub market_interval: String, // "5m" | "15m"
    pub interval_seconds: i64,

    // 安全开关
    pub dry_run: bool,
    pub live_trading: bool,

    // 凭证
    pub private_key: Option<String>,
    pub api_key: Option<String>,
    pub api_secret: Option<String>,
    pub api_passphrase: Option<String>,
    pub funder_address: Option<String>,
    pub signature_type: u8,
    pub chain_id: u64,

    // 网络
    pub rpc_url: Option<String>,
    pub wss_url: Option<String>,
    pub gamma_api_url: String,
    pub clob_api_url: String,
    pub market_ws_url: String,

    // 下单
    pub fixed_shares: f64,
    pub order_type: String, // FOK | FAK | GTC
    pub slippage_bps: f64,
    pub max_position_usdc: f64,
    pub max_trades_per_market: u32,

    // 入场过滤
    pub min_entry_price: f64,
    pub max_entry_price: f64,
    pub max_spread_pct: f64,
    pub late_entry_cutoff_sec: i64,
    pub early_entry_cutoff_sec: i64,
    pub min_ml_edge: f64,

    // 离场
    pub take_profit_pct: f64,
    pub enable_stop_loss: bool,
    pub stop_loss_pct: f64,

    // 运行 / 信号
    pub poll_interval_sec: f64,
    pub signal_lookback_min: i64,
    pub price_feed: String, // coinbase | binance

    // WS 数据新鲜度阈值（秒）：超过则回退 REST
    pub ws_staleness_sec: i64,

    // 文件
    pub paper_trades_path: PathBuf,
    pub place_order_script: PathBuf, // live 下单助手（Python py-clob-client）
    pub python_bin: String,
}

impl Config {
    /// 真钱下单仅当两把软件锁都满足（仍需 --live）。
    pub fn can_trade_live(&self) -> bool {
        self.live_trading && !self.dry_run
    }

    pub fn slippage_frac(&self) -> f64 {
        self.slippage_bps / 10_000.0
    }

    pub fn slug_prefix(&self) -> String {
        format!("btc-updown-{}", self.market_interval)
    }

    /// 缺失/占位的实盘凭证清单。
    pub fn missing_credentials(&self) -> Vec<&'static str> {
        let mut m = Vec::new();
        let check = |v: &Option<String>| -> bool {
            match v {
                None => true,
                Some(s) => {
                    let t = s.trim().to_lowercase();
                    t.is_empty() || t == "0x..." || t == "..." || t.ends_with("your_key")
                }
            }
        };
        if check(&self.private_key) { m.push("POLYMARKET_PRIVATE_KEY"); }
        if check(&self.api_key) { m.push("POLYMARKET_API_KEY"); }
        if check(&self.api_secret) { m.push("POLYMARKET_API_SECRET"); }
        if check(&self.api_passphrase) { m.push("POLYMARKET_API_PASSPHRASE"); }
        if check(&self.funder_address) { m.push("PM_FUNDER_ADDRESS"); }
        m
    }
}

// ── env 读取辅助 ────────────────────────────────────────────────────────────

fn get(key: &str) -> Option<String> {
    match std::env::var(key) {
        Ok(v) if !v.trim().is_empty() => Some(v.trim().to_string()),
        _ => None,
    }
}

fn get_or(key: &str, default: &str) -> String {
    get(key).unwrap_or_else(|| default.to_string())
}

fn get_bool(key: &str, default: bool) -> bool {
    match get(key).as_deref() {
        Some(v) => matches!(v.to_lowercase().as_str(), "1" | "true" | "yes" | "y" | "on"),
        None => default,
    }
}

fn get_f64(key: &str, default: f64) -> f64 {
    get(key).and_then(|v| v.parse().ok()).unwrap_or(default)
}

fn get_i64(key: &str, default: i64) -> i64 {
    get(key).and_then(|v| v.parse().ok()).unwrap_or(default)
}

fn get_u64(key: &str, default: u64) -> u64 {
    get(key).and_then(|v| v.parse().ok()).unwrap_or(default)
}

fn get_u32(key: &str, default: u32) -> u32 {
    get(key).and_then(|v| v.parse().ok()).unwrap_or(default)
}

fn get_u8(key: &str, default: u8) -> u8 {
    get(key).and_then(|v| v.parse().ok()).unwrap_or(default)
}

/// 解析 .env 路径：JY_ENV_PATH → ../.env（rust/ 上一级=项目根）→ ./.env
pub fn default_env_path() -> String {
    if let Some(p) = get("JY_ENV_PATH") {
        return p;
    }
    for cand in ["../.env", ".env", "../../.env"] {
        if Path::new(cand).exists() {
            return cand.to_string();
        }
    }
    "../.env".to_string()
}

fn normalize_interval(raw: &str) -> (String, i64) {
    match raw.trim().to_lowercase().as_str() {
        "15m" | "15min" | "15" | "900" => ("15m".to_string(), 900),
        _ => ("5m".to_string(), 300),
    }
}

pub fn load(env_path: Option<&str>) -> Result<Config> {
    let path = env_path.map(|s| s.to_string()).unwrap_or_else(default_env_path);
    // 找不到 .env 不报错（环境变量也可注入）。
    dotenvy::from_path(&path).ok();

    let (market_interval, interval_seconds) =
        normalize_interval(&get_or("MARKET_INTERVAL", "5m"));

    let order_type = get_or("ORDER_TYPE", "FOK").to_uppercase();
    let order_type = if matches!(order_type.as_str(), "FOK" | "FAK" | "GTC") {
        order_type
    } else {
        "FOK".to_string()
    };

    let price_feed = get_or("PRICE_FEED", "coinbase").to_lowercase();
    let price_feed = if matches!(price_feed.as_str(), "coinbase" | "binance") {
        price_feed
    } else {
        "coinbase".to_string()
    };

    let base_dir: PathBuf = Path::new(&path)
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."));

    let cfg = Config {
        market_interval,
        interval_seconds,
        dry_run: get_bool("DRY_RUN", true),
        live_trading: get_bool("LIVE_TRADING", false),

        private_key: get("POLYMARKET_PRIVATE_KEY").or_else(|| get("POLYMARKET_PK")),
        api_key: get("POLYMARKET_API_KEY"),
        api_secret: get("POLYMARKET_API_SECRET"),
        api_passphrase: get("POLYMARKET_API_PASSPHRASE").or_else(|| get("POLYMARKET_PASSPHRASE")),
        funder_address: get("PM_FUNDER_ADDRESS").or_else(|| get("POLYMARKET_FUNDER")),
        signature_type: get_u8("POLYMARKET_SIG_TYPE", 2),
        chain_id: get_u64("CHAIN_ID", 137),

        rpc_url: get("RPC_URL").or_else(|| get("ETH_RPC_URL")),
        wss_url: get("WSS_URL"),
        gamma_api_url: get_or("GAMMA_API_URL", "https://gamma-api.polymarket.com")
            .trim_end_matches('/')
            .to_string(),
        clob_api_url: get_or("CLOB_API_URL", "https://clob.polymarket.com")
            .trim_end_matches('/')
            .to_string(),
        market_ws_url: get_or(
            "MARKET_WS_URL",
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        ),

        fixed_shares: get_f64("FIXED_SHARES", 5.0),
        order_type,
        slippage_bps: get_f64("SLIPPAGE_BPS", 100.0),
        max_position_usdc: get_f64("MAX_POSITION_USDC", 5.0),
        max_trades_per_market: get_u32("MAX_TRADES_PER_MARKET", 1),

        min_entry_price: get_f64("MIN_ENTRY_PRICE", 0.25),
        max_entry_price: get_f64("MAX_ENTRY_PRICE", 0.75),
        max_spread_pct: get_f64("MAX_SPREAD_PCT", 0.05),
        late_entry_cutoff_sec: get_i64("LATE_ENTRY_CUTOFF_SEC", 45),
        early_entry_cutoff_sec: get_i64("EARLY_ENTRY_CUTOFF_SEC", 0),
        min_ml_edge: get_f64("MIN_ML_EDGE", 0.10),

        take_profit_pct: get_f64("TAKE_PROFIT_PCT", 0.40),
        enable_stop_loss: get_bool("ENABLE_STOP_LOSS", false),
        stop_loss_pct: get_f64("STOP_LOSS_PCT", 0.50),

        poll_interval_sec: get_f64("POLL_INTERVAL_SEC", 3.0),
        signal_lookback_min: get_i64("SIGNAL_LOOKBACK_MIN", 3),
        price_feed,

        ws_staleness_sec: get_i64("WS_STALENESS_SEC", 8),

        paper_trades_path: base_dir.join(get_or("PAPER_TRADES_FILE", "paper_trades.json")),
        place_order_script: base_dir.join("scripts/place_order.py"),
        python_bin: get_or("PYTHON_BIN", "python3"),
    };

    // 实盘前置校验
    if cfg.can_trade_live() {
        if cfg.private_key.is_none() {
            bail!("DRY_RUN=false 且 LIVE_TRADING=true 时必须设置 POLYMARKET_PRIVATE_KEY");
        }
    }

    Ok(cfg)
}

/// 启动横幅用的简要描述。
pub fn describe(c: &Config) -> String {
    let mode = if c.can_trade_live() {
        "LIVE (真钱)"
    } else {
        "DRY-RUN (纸面)"
    };
    format!(
        "  MARKET_INTERVAL      = {} ({}s)\n\
         \x20 MODE                 = {}\n\
         \x20   DRY_RUN            = {}\n\
         \x20   LIVE_TRADING       = {}\n\
         \x20 FIXED_SHARES         = {}\n\
         \x20 ORDER_TYPE           = {}\n\
         \x20 SLIPPAGE_BPS         = {} ({:.4}%)\n\
         \x20 MAX_POSITION_USDC    = {}\n\
         \x20 MAX_TRADES_PER_MARKET= {}\n\
         \x20 ENTRY_PRICE_BAND     = [{}, {}]\n\
         \x20 MAX_SPREAD_PCT       = {}\n\
         \x20 LATE_ENTRY_CUTOFF_SEC= {}\n\
         \x20 MIN_ML_EDGE          = {}\n\
         \x20 TAKE_PROFIT_PCT      = {}\n\
         \x20 ENABLE_STOP_LOSS     = {} (pct={})\n\
         \x20 PRICE_FEED           = {}\n\
         \x20 MARKET_WS_URL        = {}",
        c.market_interval, c.interval_seconds, mode, c.dry_run, c.live_trading,
        c.fixed_shares, c.order_type, c.slippage_bps, c.slippage_frac() * 100.0,
        c.max_position_usdc, c.max_trades_per_market, c.min_entry_price, c.max_entry_price,
        c.max_spread_pct, c.late_entry_cutoff_sec, c.min_ml_edge, c.take_profit_pct,
        c.enable_stop_loss, c.stop_loss_pct, c.price_feed, c.market_ws_url,
    )
}
