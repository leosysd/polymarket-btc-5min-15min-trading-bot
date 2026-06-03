//! BTC 涨跌概率信号（与 Python 版 jybot/signal.py 同口径）。
//!
//! 用窗口起点至今的漂移 drift + 短期动量 mom，logistic 映射成 P(涨)。
//! 数据来自常驻的 BTC 价格 WS（price_ws.rs）。

use crate::price_ws::PriceFeed;

pub struct Signal {
    pub direction: Option<&'static str>, // "UP" | "DOWN" | None
    pub p_up: f64,
    pub reason: String,
    pub spot: Option<f64>,
    pub reference: Option<f64>,
}

impl Signal {
    pub fn p_down(&self) -> f64 {
        1.0 - self.p_up
    }
    pub fn none(reason: &str) -> Self {
        Signal { direction: None, p_up: 0.5, reason: reason.to_string(), spot: None, reference: None }
    }
}

fn logistic(x: f64) -> f64 {
    1.0 / (1.0 + (-x).exp())
}

pub fn compute(feed: &PriceFeed, window_start_ts: i64, lookback_min: i64) -> Signal {
    let hist = feed.snapshot();
    if hist.is_empty() {
        return Signal::none("price feed unavailable -- no trade");
    }

    let spot = hist.last().map(|(_, p)| *p).unwrap();
    let reference = feed.price_at(window_start_ts).unwrap_or(spot);

    let drift = if reference != 0.0 {
        (spot - reference) / reference
    } else {
        0.0
    };

    // 短期动量：最近 lookback 窗口内逐点收益均值。
    let now = chrono::Utc::now().timestamp();
    let cutoff = now - lookback_min.max(1) * 60;
    let pts: Vec<f64> = hist
        .iter()
        .filter(|(ts, _)| *ts >= cutoff)
        .map(|(_, p)| *p)
        .collect();
    let mut rets = Vec::new();
    for w in pts.windows(2) {
        if w[0] != 0.0 {
            rets.push((w[1] - w[0]) / w[0]);
        }
    }
    let mom = if rets.is_empty() {
        0.0
    } else {
        rets.iter().sum::<f64>() / rets.len() as f64
    };

    let logit = 3.0 * (drift / 0.0015) + 2.0 * (mom / 0.0008);
    let p_up = logistic(logit).clamp(0.15, 0.85);

    let direction = if p_up >= 0.5 { "UP" } else { "DOWN" };
    let reason = format!(
        "spot={:.1} ref={:.1} drift={:+.3}% mom={:+.4}% -> p_up={:.3}",
        spot,
        reference,
        drift * 100.0,
        mom * 100.0,
        p_up
    );

    Signal {
        direction: Some(direction),
        p_up,
        reason,
        spot: Some(spot),
        reference: Some(reference),
    }
}
