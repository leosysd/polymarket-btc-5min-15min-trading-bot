//! 持仓 + 纸面成交记录 + 统计（胜率 / PnL）。
//!
//! 记录 schema 与 Python 版 jybot/state.py 一致，故 `scripts/view_trades.py`
//! 与 `paper_trades.json` 两边通用。

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct Position {
    pub market_slug: String,
    pub condition_id: String,
    pub token_id: String,
    pub outcome: String,   // "Up" | "Down"
    pub direction: String, // "UP" | "DOWN"
    pub entry_price: f64,
    pub size: f64,
    pub entry_ts: i64,
    pub settle_ts: i64,
    pub p_model: f64,
    pub ref_price: f64,
    pub status: String, // OPEN | CLOSED
    pub exit_price: Option<f64>,
    pub exit_reason: String,
    pub outcome_result: String, // WIN | LOSS | ""
    pub pnl_usd: f64,
}

impl Position {
    pub fn to_record(&self) -> TradeRecord {
        TradeRecord {
            timestamp: chrono::DateTime::from_timestamp(self.entry_ts, 0)
                .map(|dt| dt.format("%Y-%m-%d %H:%M:%S").to_string())
                .unwrap_or_default(),
            entry_ts: self.entry_ts,
            market: self.market_slug.clone(),
            condition_id: self.condition_id.clone(),
            token_id: self.token_id.clone(),
            side: self.direction.clone(),
            outcome_token: self.outcome.clone(),
            entry_price: round4(self.entry_price),
            size: round4(self.size),
            exit_price: self.exit_price.map(round4),
            status: self.status.clone(),
            outcome: if self.outcome_result.is_empty() {
                "OPEN".to_string()
            } else {
                self.outcome_result.clone()
            },
            pnl_usd: round4(self.pnl_usd),
            exit_reason: self.exit_reason.clone(),
            p_model: round4(self.p_model),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TradeRecord {
    pub timestamp: String,
    pub entry_ts: i64,
    pub market: String,
    pub condition_id: String,
    pub token_id: String,
    pub side: String,
    pub outcome_token: String,
    pub entry_price: f64,
    pub size: f64,
    pub exit_price: Option<f64>,
    pub status: String,
    pub outcome: String,
    pub pnl_usd: f64,
    pub exit_reason: String,
    pub p_model: f64,
}

fn round4(x: f64) -> f64 {
    (x * 10_000.0).round() / 10_000.0
}

pub struct TradeLog {
    path: PathBuf,
    records: Vec<TradeRecord>,
}

impl TradeLog {
    pub fn load(path: &Path) -> Self {
        let records = std::fs::read_to_string(path)
            .ok()
            .and_then(|s| serde_json::from_str::<Vec<TradeRecord>>(&s).ok())
            .unwrap_or_default();
        Self { path: path.to_path_buf(), records }
    }

    pub fn append(&mut self, rec: TradeRecord) {
        self.records.push(rec);
        self.flush();
    }

    /// 更新最近一条同 token 的 OPEN 记录（结算/离场时）；找不到则追加。
    pub fn update_last_open(&mut self, token_id: &str, rec: TradeRecord) {
        if let Some(slot) = self
            .records
            .iter_mut()
            .rev()
            .find(|r| r.token_id == token_id && r.status == "OPEN")
        {
            *slot = rec;
            self.flush();
        } else {
            self.append(rec);
        }
    }

    fn flush(&self) {
        if let Some(parent) = self.path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        if let Ok(s) = serde_json::to_string_pretty(&self.records) {
            let tmp = self.path.with_extension("json.tmp");
            if std::fs::write(&tmp, s).is_ok() {
                let _ = std::fs::rename(&tmp, &self.path);
            }
        }
    }

    pub fn summary(&self) -> Summary {
        let settled: Vec<&TradeRecord> = self
            .records
            .iter()
            .filter(|r| r.outcome == "WIN" || r.outcome == "LOSS")
            .collect();
        let wins = settled.iter().filter(|r| r.outcome == "WIN").count();
        let pnl: f64 = self.records.iter().map(|r| r.pnl_usd).sum();
        Summary {
            trades: self.records.len(),
            settled: settled.len(),
            wins,
            losses: settled.len() - wins,
            win_rate: if settled.is_empty() {
                0.0
            } else {
                wins as f64 / settled.len() as f64 * 100.0
            },
            pnl_usd: pnl,
        }
    }
}

pub struct Summary {
    pub trades: usize,
    pub settled: usize,
    pub wins: usize,
    pub losses: usize,
    pub win_rate: f64,
    pub pnl_usd: f64,
}
