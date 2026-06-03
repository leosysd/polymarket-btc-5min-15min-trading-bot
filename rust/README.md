# jybot-rs — Rust 核心（WebSocket + tokio）

这是 Python 版机器人的 **Rust 重写核心**，参考 [leosysd/JY_RUST](https://github.com/leosysd/JY_RUST) 的 Rust + WebSocket 架构。

行情/盘口判断全部走 **WebSocket 长连接**、事件驱动；REST 仅作兜底；真实下单复用 Python 官方 `py-clob-client`。

## 架构

```
                 ┌─────────────── tokio 异步运行时 ───────────────┐
 Polymarket 盘口 WS ─┐                                            │
 (market_ws.rs)      ├─► 事件驱动主循环 (main.rs: tokio::select!) │
 BTC 价格 WS ────────┘        │                                   │
 (price_ws.rs)                ▼                                   │
                        策略决策 (strategy.rs)                     │
                          │        │                              │
                   信号(signal.rs) 执行(executor.rs)              │
                                       │                          │
                          DRY-RUN 模拟撮合 / LIVE→Python 下单      │
                 └──────────────────────────────────────────────┘
   REST 兜底 (clob.rs)：WS 无数据或过期(WS_STALENESS_SEC)时回退
```

| 文件 | 职责 |
| --- | --- |
| `src/main.rs` | 入口、CLI、安全门、tokio 事件驱动主循环 |
| `src/config.rs` | 读 `.env`（变量名与 Python 版一致）、安全锁 |
| `src/clob.rs` | 市场动态发现、REST 盘口兜底、结算查询、盘口缓存 |
| `src/market_ws.rs` | Polymarket 盘口 WS（订阅/增量/重连/事件通知） |
| `src/price_ws.rs` | BTC 价格 WS（Binance 镜像 aggTrade）+ REST 兜底 |
| `src/signal.rs` | 漂移+动量 → P(涨) 信号 |
| `src/strategy.rs` | 入场过滤、持仓管理、TP/SL、结算 |
| `src/executor.rs` | 固定份额限价单；DRY 模拟 / LIVE 调 Python |
| `src/state.rs` | 持仓 + 纸面记录 + 胜率/PnL |

## 编译

需要 Rust 工具链（[rustup.rs](https://rustup.rs)）。`.env` 在项目根（`../.env`）。

```bash
cd rust
cargo build --release
# 产物: rust/target/release/jybot-rs
```

> Linux VPS 上编译最顺（一条 `cargo build --release` 即可）。
> Windows 用 GNU 工具链：`rustup default stable-x86_64-pc-windows-gnu`，并确保有 MinGW gcc。

## 运行

```bash
# 从项目根运行，自动读取 ../.env / .env
./rust/target/release/jybot-rs --test-mode     # 有界纸面演示
./rust/target/release/jybot-rs --simulation    # 纸面模拟（真实时钟，WS 事件驱动）
./rust/target/release/jybot-rs check           # 检查配置
./rust/target/release/jybot-rs stats           # 胜率 / PnL / 交易统计
./rust/target/release/jybot-rs --live          # 实盘（需安全锁 + 输入 YES）
# 可选: --interval 5m|15m
```

## 安全模型（与 Python 版一致）

真钱下单需 **三把锁同时满足**：
1. 启动加 `--live`
2. `.env` `DRY_RUN=false`
3. `.env` `LIVE_TRADING=true`

任一不满足即纸面模拟。`--live` 还会要求终端输入大写 `YES`；安全锁未打开时 `--live` 直接退出（码 2）。

## 实盘下单

Rust 负责全部行情/盘口/策略（WebSocket）。真实提交订单时，executor 调用
`scripts/place_order.py`（官方 `py-clob-client` 完成 EIP-712 签名与提交）。
因此实盘需要：

```bash
pip install py-clob-client     # 装在 PYTHON_BIN 指定的解释器里
```

并在 `.env` 填好 `POLYMARKET_PRIVATE_KEY` / API 三件套 / `PM_FUNDER_ADDRESS`。

## 相关 .env 变量（Rust 专用，其余与 Python 版共用）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MARKET_WS_URL` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Polymarket 盘口 WS |
| `WS_STALENESS_SEC` | `8` | WS 数据过期阈值（秒）→ 回退 REST |
| `PYTHON_BIN` | `python3` | 实盘下单调用的 Python 解释器 |
