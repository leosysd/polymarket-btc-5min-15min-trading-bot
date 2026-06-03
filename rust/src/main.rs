//! jybot-rs — Polymarket BTC 5 分钟 UP/DOWN 交易机器人（Rust 核心）。
//!
//! 行情/盘口走 WebSocket 长连接，事件驱动决策；REST 仅作兜底。
//! 安全模型：默认纸面模拟；真钱下单需 --live + DRY_RUN=false + LIVE_TRADING=true。
//!
//! 用法：
//!   jybot-rs --test-mode        有界纸面演示
//!   jybot-rs --simulation       纸面模拟（真实时钟，默认）
//!   jybot-rs --live             实盘（需安全锁 + 手动确认）
//!   jybot-rs stats              查看胜率 / PnL / 交易记录
//!   jybot-rs check              检查配置
//!   可选: --interval 5m|15m

mod clob;
mod config;
mod executor;
mod market_ws;
mod price_ws;
mod signal;
mod state;
mod strategy;

use std::sync::Arc;

use anyhow::Result;
use clob::{new_book_cache, ClobClient};
use executor::Executor;
use market_ws::MarketWs;
use price_ws::PriceFeed;
use strategy::Strategy;
use tracing::{info, warn};

#[derive(PartialEq)]
enum Mode {
    Test,
    Simulation,
    Live,
}

#[tokio::main]
async fn main() -> Result<()> {
    let _ = rustls::crypto::ring::default_provider().install_default();
    tracing_subscriber::fmt().with_target(false).with_level(true).init();

    let args: Vec<String> = std::env::args().skip(1).collect();

    // 子命令（非交易）
    if let Some(first) = args.first() {
        match first.as_str() {
            "stats" => return cmd_stats(),
            "check" => return cmd_check(),
            "-h" | "--help" | "help" => {
                print_help();
                return Ok(());
            }
            _ => {}
        }
    }

    // 周期覆盖
    let interval_override = arg_value(&args, "--interval");

    // 模式
    let mode = if args.iter().any(|a| a == "--live") {
        Mode::Live
    } else if args.iter().any(|a| a == "--test-mode" || a == "--test") {
        Mode::Test
    } else {
        Mode::Simulation
    };

    let mut cfg = config::load(None)?;
    if let Some(iv) = interval_override {
        let (label, secs) = match iv.as_str() {
            "15m" => ("15m".to_string(), 900),
            _ => ("5m".to_string(), 300),
        };
        cfg.market_interval = label;
        cfg.interval_seconds = secs;
    }

    // 模式安全门
    let mode_label = match mode {
        Mode::Live => {
            if !cfg.can_trade_live() {
                eprintln!("==============================================================");
                eprintln!("拒绝启动 LIVE。--live 需要 .env 同时满足：");
                eprintln!("    DRY_RUN=false        (当前 DRY_RUN={})", cfg.dry_run);
                eprintln!("    LIVE_TRADING=true    (当前 LIVE_TRADING={})", cfg.live_trading);
                eprintln!("这是安全锁。编辑 .env 后重试。");
                eprintln!("==============================================================");
                std::process::exit(2);
            }
            let missing = cfg.missing_credentials();
            if !missing.is_empty() {
                eprintln!("无法实盘——.env 缺少凭证: {}", missing.join(", "));
                std::process::exit(2);
            }
            "LIVE (真钱)"
        }
        Mode::Test => {
            cfg.dry_run = true;
            cfg.live_trading = false;
            "TEST (纸面, 有界)"
        }
        Mode::Simulation => {
            cfg.dry_run = true;
            cfg.live_trading = false;
            "SIMULATION (纸面)"
        }
    };

    println!("==============================================================");
    println!("  POLYMARKET BTC {} UP/DOWN BOT (Rust)", cfg.market_interval.to_uppercase());
    println!("  Mode: {mode_label}");
    println!("==============================================================");
    for line in config::describe(&cfg).lines() {
        info!("{line}");
    }

    if mode == Mode::Live {
        println!("\n!!! LIVE 实盘 —— 将向 Polymarket 提交真实订单 !!!");
        println!("输入大写 YES 继续，其它任意键取消：");
        let mut input = String::new();
        std::io::stdin().read_line(&mut input).ok();
        if input.trim() != "YES" {
            println!("已取消。");
            return Ok(());
        }
    }

    let cfg = Arc::new(cfg);
    let test_mode = mode == Mode::Test;
    run(cfg, test_mode).await
}

async fn run(cfg: Arc<config::Config>, test_mode: bool) -> Result<()> {
    let cache = new_book_cache();
    let clob = Arc::new(ClobClient::new(
        &cfg.clob_api_url,
        &cfg.gamma_api_url,
        &cfg.slug_prefix(),
        cfg.interval_seconds,
    ));

    // BTC 价格 WS（事件驱动）
    let price = PriceFeed::new(&cfg.price_feed);
    let price_evt = price.updated_handle();
    let _price_task = price.clone().run();

    // Polymarket 盘口 WS（事件驱动）
    let ws = MarketWs::new(&cfg.market_ws_url, cache.clone());
    let _ws_task = ws.run();

    // 启动时展示发现能力（证明动态发现工作）
    let upcoming = clob.upcoming_markets(4).await;
    if upcoming.is_empty() {
        warn!("[discovery] 暂未发现市场（API/网络？）");
    } else {
        info!("[discovery] 即将到来的市场:");
        for m in &upcoming {
            info!("  {}  settle_in={}s", m.slug, m.seconds_left());
        }
    }

    let exec = Arc::new(Executor::new(cfg.clone()));
    let mut strat = Strategy::new(cfg.clone(), clob.clone(), cache.clone(), ws.clone(), price.clone(), exec);

    if !cfg.can_trade_live() {
        info!("安全模式：纸面交易（真钱需 DRY_RUN=false + LIVE_TRADING=true + --live）");
    } else {
        warn!("LIVE 实盘已启用 —— 真钱风险");
    }

    let poll = tokio::time::Duration::from_secs_f64(cfg.poll_interval_sec.max(0.5));
    let min_gap = tokio::time::Duration::from_millis(50);
    let max_iters: Option<u64> = if test_mode {
        Some(std::env::var("TEST_MODE_ITERS").ok().and_then(|s| s.parse().ok()).unwrap_or(3))
    } else {
        None
    };

    let mut iters: u64 = 0;
    loop {
        let t0 = tokio::time::Instant::now();

        // BTC 价格 WS 过期 → REST 兜底回填
        if price.age_ms() > cfg.ws_staleness_sec * 1000 {
            price.rest_backfill().await;
        }

        if let Err(e) = strat.run_once().await {
            warn!("[loop] {e}");
        }

        iters += 1;
        if let Some(max) = max_iters {
            if iters >= max {
                info!("[test-mode] 达到 {max} 轮，停止");
                break;
            }
        }

        // 事件驱动：盘口更新 / 价格更新 / 兜底超时 / Ctrl+C，谁先到
        tokio::select! {
            _ = ws.wait_book_update() => {}
            _ = price_evt.notified() => {}
            _ = tokio::time::sleep(poll) => {}
            _ = tokio::signal::ctrl_c() => {
                info!("收到 Ctrl+C，退出");
                break;
            }
        }

        let elapsed = t0.elapsed();
        if elapsed < min_gap {
            tokio::time::sleep(min_gap - elapsed).await;
        }
    }

    strat.print_summary();
    Ok(())
}

// ── 子命令 ──────────────────────────────────────────────────────────────────

fn cmd_check() -> Result<()> {
    let cfg = config::load(None)?;
    println!("==============================================================");
    println!("  配置检查 (.env)");
    println!("==============================================================");
    println!("{}", config::describe(&cfg));
    println!("--------------------------------------------------------------");
    let missing = cfg.missing_credentials();
    if missing.is_empty() {
        println!("  [OK] 实盘凭证齐全");
    } else {
        for m in &missing {
            let tag = if cfg.can_trade_live() { "FAIL" } else { "WARN" };
            println!("  [{tag}] 缺少凭证 {m}（实盘需要）");
        }
    }
    let mode = if cfg.can_trade_live() { "LIVE (真钱)" } else { "DRY-RUN (纸面)" };
    println!("  结果: 模式 = {mode}");
    Ok(())
}

fn cmd_stats() -> Result<()> {
    let cfg = config::load(None)?;
    let log = state::TradeLog::load(&cfg.paper_trades_path);
    let s = log.summary();
    println!("==============================================================");
    println!("  交易统计 ({})", cfg.paper_trades_path.display());
    println!("==============================================================");
    println!("  总交易:   {}", s.trades);
    println!("  已结算:   {}", s.settled);
    println!("  胜 / 负:  {} / {}", s.wins, s.losses);
    println!("  胜率:     {:.1}%", s.win_rate);
    println!("  累计 PnL: ${:+.4}", s.pnl_usd);
    println!("==============================================================");
    Ok(())
}

fn arg_value(args: &[String], key: &str) -> Option<String> {
    args.iter().position(|a| a == key).and_then(|i| args.get(i + 1)).cloned()
}

fn print_help() {
    println!(
        "jybot-rs — Polymarket BTC UP/DOWN 交易机器人 (Rust)\n\n\
        用法:\n\
        \x20 jybot-rs --test-mode      有界纸面演示\n\
        \x20 jybot-rs --simulation     纸面模拟（默认）\n\
        \x20 jybot-rs --live           实盘（需安全锁 + 确认）\n\
        \x20 jybot-rs stats            查看胜率/PnL\n\
        \x20 jybot-rs check            检查配置\n\
        \x20 选项: --interval 5m|15m"
    );
}
