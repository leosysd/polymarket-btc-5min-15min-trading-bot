# Polymarket BTC 5 分钟 UP/DOWN 自动交易机器人

一个**开箱即用、可直接部署**的 Polymarket 比特币 5 分钟「涨/跌」（UP/DOWN）市场自动交易机器人。

核心设计目标：**你只需要填 `.env`，不需要改任何代码**。

- ✅ 默认 **dry-run（纸面模拟）**，绝不会拿真钱下单
- ✅ 自动从 Polymarket Gamma / CLOB API **动态发现** BTC 5 分钟市场（不写死任何 market_id / token_id）
- ✅ 固定份额（`FIXED_SHARES`）限价下单，支持 **FOK / FAK / GTC**
- ✅ 只有在 `.env` 里把 `LIVE_TRADING=true` 且 `DRY_RUN=false`、并用 `--live` 启动后，才会真实下单
- ✅ 模拟路径**只依赖 Python 标准库**，干净机器上也能跑 `python main.py --test-mode`
- ✅ 自带配置检查、健康检查、一键安装、systemd 服务文件

---

## 1. 快速开始（3 步）

```bash
# 1) 复制配置文件（你之后只改这个文件）
cp .env.example .env

# 2) 校验配置（默认 dry-run 配置即可通过）
python scripts/check_config.py

# 3) 跑一个有界的纸面演示（离线也能跑，约 3 轮后自动退出）
python main.py --test-mode
```

想长期模拟（真实行情、真实时钟、纸面成交）：

```bash
python main.py --simulation
```

### 推荐：交互式菜单（不想敲命令就用它）

```bash
python menu.py
```

一个纯标准库的终端菜单，集成了**启动 / 配置 / 工具**三大功能，新手强烈推荐：

- **启动机器人**：一键跑 test-mode / simulation / supervisor；实盘 `--live` 入口带二次确认（需输入大写 `YES`，且安全锁未打开时直接拦截）。
- **配置 `.env`**：按分组（市场周期 / 安全开关 / 钱包API / 网络 / 下单 / 入场 / 离场 / 运行 / Redis）读写所有参数，**带类型校验**（bool/int/float/枚举），改完可立即跑 `check_config`。
- **工具**：一键运行 `check_config` / `health_check` / `view_trades` / `regen_polymarket_keys`，以及查看最近日志。

安全设计：没有 `.env` 会自动从 `.env.example` 复制；每次保存前自动备份为 `.env.backup-时间戳`；私钥 / API_KEY / API_SECRET / PASSPHRASE **显示时一律打码**（如 `0x12****beef`），绝不明文打印；把 `LIVE_TRADING=true` 或 `DRY_RUN=false` 这类危险开关打开时，必须输入大写 `YES` 才会保存。菜单**只写入 `.env`，绝不改动代码**。

---

## 2. 启动命令

| 命令 | 说明 | 是否真钱 |
| --- | --- | --- |
| `python main.py --test-mode` | 有界纸面演示，离线友好（默认 3 轮） | 否（强制纸面） |
| `python main.py --simulation` | 纸面交易，真实 5 分钟市场时钟 | 否（强制纸面） |
| `python main.py --live` | **真实下单**，需通过安全锁 | 是 |
| `python supervisor.py --simulation` | 带自动重启的模拟运行 | 否 |
| `python supervisor.py --live` | 带自动重启的实盘运行 | 是 |

可选参数：

- `--interval 5m` / `--interval 15m`：临时覆盖 `MARKET_INTERVAL`
- `--verbose`：DEBUG 级别日志

---

## 3. 实盘安全锁（三重保险）

只有**同时满足**以下三点，机器人才会签名并提交真实订单：

1. 用 `python main.py --live` 启动
2. `.env` 中 `DRY_RUN=false`
3. `.env` 中 `LIVE_TRADING=true`

任何一个不满足，都会保持纸面模式。`--test-mode` 和 `--simulation` 永远强制纸面，**无视** `.env` 里的开关。

如果用 `--live` 启动但安全锁没打开，机器人会**拒绝启动**并提示你去改 `.env`（退出码 2，supervisor 不会无限重启）。

> ⚠️ 实盘风险提示：5 分钟二元市场到期后，输的一方 token 结算为 0，即亏损约 100% 本金。请从最小 `FIXED_SHARES`、最小 `MAX_POSITION_USDC` 开始，自行承担风险。

---

## 4. 配置说明（`.env`）

所有参数都在 `.env`，改完即生效，无需改代码。

### 市场周期
| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MARKET_INTERVAL` | `5m` | `5m` 或 `15m` |

### 安全开关
| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DRY_RUN` | `true` | true = 纸面模拟 |
| `LIVE_TRADING` | `false` | true = 允许实盘（仍需 `DRY_RUN=false` + `--live`） |

### 钱包 / API 凭证（仅实盘需要）
| 变量 | 说明 |
| --- | --- |
| `POLYMARKET_PRIVATE_KEY` | 签名订单的 EOA 私钥 |
| `POLYMARKET_API_KEY` / `POLYMARKET_API_SECRET` / `POLYMARKET_API_PASSPHRASE` | CLOB API 凭证，用 `python scripts/regen_polymarket_keys.py` 生成 |
| `PM_FUNDER_ADDRESS` | 持有 USDC 的地址（Polymarket 充值地址 / 代理 Safe） |
| `POLYMARKET_SIG_TYPE` | `0`=EOA `1`=POLY_PROXY `2`=POLY_GNOSIS_SAFE（默认 2） |
| `CHAIN_ID` | 默认 137（Polygon） |

### 网络
| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RPC_URL` | `https://polygon-rpc.com` | 链上结算校验用 |
| `WSS_URL` | 空 | 可选 websocket |
| `GAMMA_API_URL` / `CLOB_API_URL` | 公共端点 | 一般不用改 |

### 下单
| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FIXED_SHARES` | `5` | 每单固定份额（市场最小为 5） |
| `ORDER_TYPE` | `FOK` | `FOK` 全成或撤 / `FAK` 部分成交后撤 / `GTC` 挂单 |
| `SLIPPAGE_BPS` | `100` | 滑点，100bps = 1%（BUY 加在最优卖价上，SELL 减在最优买价上） |
| `MAX_POSITION_USDC` | `5` | 单仓名义上限 |
| `MAX_TRADES_PER_MARKET` | `1` | 每个市场最多下单次数 |

### 入场过滤
| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MIN_ENTRY_PRICE` / `MAX_ENTRY_PRICE` | `0.25` / `0.75` | 价格不在区间内不进场 |
| `MAX_SPREAD_PCT` | `0.05` | 点差过大不进场 |
| `LATE_ENTRY_CUTOFF_SEC` | `45` | 距结算不足 45 秒不进场 |
| `EARLY_ENTRY_CUTOFF_SEC` | `0` | 开盘后多少秒才允许进场 |
| `MIN_ML_EDGE` | `0.10` | 模型概率 − 市场卖价 的最小优势 |

### 离场
| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TAKE_PROFIT_PCT` | `0.40` | TP = entry + 0.40 ×(1 − entry) |
| `ENABLE_STOP_LOSS` | `false` | 是否启用止损 |
| `STOP_LOSS_PCT` | `0.50` | SL = entry − 0.50 × entry |

### 运行 / 信号
| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `POLL_INTERVAL_SEC` | `3` | 主循环轮询间隔 |
| `SIGNAL_LOOKBACK_MIN` | `3` | 信号回看分钟数 |
| `PRICE_FEED` | `coinbase` | `coinbase` 或 `binance`（binance 部分地区被封） |
| `REDIS_HOST/PORT/DB` | localhost/6379/2 | 可选 |

---

## 5. 下单逻辑（与需求一致）

- **固定份额**：每个 BUY 订单 `size = FIXED_SHARES`；绝不把市价单的 USD 金额当份额。
- **限价单**：BUY 与 SELL 都是限价单。
  - BUY 价 = 最优卖价 ×(1 + `SLIPPAGE_BPS`)，并按 tick 取整。
  - SELL 价 = 最优买价 ×(1 − `SLIPPAGE_BPS`)。
- **SELL 不超持仓**：卖出份额自动 `min(size, 当前持仓)`。
- **订单类型**：`FOK` 必须全部成交否则取消；`FAK` 允许部分成交、剩余取消；`GTC` 剩余挂在盘上。
- **5 分钟专用**：每个市场最多 `MAX_TRADES_PER_MARKET` 次；距结算 < `LATE_ENTRY_CUTOFF_SEC` 不进场；市场结算后自动切到下一个 5 分钟市场。
- **异常处理**：市场未开放 / token_id 缺失 / 流动性不足 / 盘口为空 / API 限流（429 自动退避重试）都会被安全跳过并记录日志。

每一次决策日志都会显示：当前市场 slug、UP/DOWN token、价格、信号、是否下单、下单/不下单的原因。

---

## 6. 部署到服务器（systemd）

```bash
# 1) 放到 /opt/jy-bot 并安装
sudo mkdir -p /opt/jy-bot && sudo chown $USER /opt/jy-bot
cp -r . /opt/jy-bot && cd /opt/jy-bot
bash scripts/install.sh          # 建 venv、装依赖、生成 .env、检查 Redis、校验配置

# 2) 编辑配置
nano /opt/jy-bot/.env

# 3) 安装服务（先按需修改 jy-bot.service 里的 User / WorkingDirectory / ExecStart）
sudo cp jy-bot.service /etc/systemd/system/jy-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now jy-bot

# 4) 看日志
journalctl -u jy-bot -f
```

`jy-bot.service` 默认以 `--simulation` 启动。确认无误后，把 `.env` 改成
`DRY_RUN=false` + `LIVE_TRADING=true`，并把 `ExecStart` 改成 `--live`，再
`systemctl restart jy-bot`。

---

## 7. 实用脚本

| 脚本 | 作用 |
| --- | --- |
| `python menu.py` | 交互式菜单：启动 / 配置 `.env` / 工具（推荐） |
| `python scripts/check_config.py` | 检查 `.env` 是否完整、合法 |
| `python scripts/health_check.py` | 检查能否连上 Gamma / CLOB / 行情源 / Redis / RPC |
| `python scripts/regen_polymarket_keys.py` | 生成 Polymarket CLOB API 凭证 |
| `python scripts/view_trades.py` | 查看纸面交易记录（`paper_trades.json`） |
| `bash scripts/install.sh` | 一键安装 |

---

## 8. 工作原理

```
发现当前活跃 5m 市场  ->  计算信号 + 过滤  ->  固定份额限价下单（一单）
        ^                                              |
        +------------ 结算后切换下一个市场 <-- 管理 TP/SL/结算
```

- **市场发现**：根据当前时间对齐到 300 秒，计算 slug `btc-updown-5m-{unix时间戳}`，从 Gamma `/markets` 动态拉取当前及临近窗口的市场，全部动态、无硬编码。
- **盘口**：从 CLOB `/book?token_id=...` 读取，best bid = 买盘最高价，best ask = 卖盘最低价。
- **信号**：用 Coinbase/Binance 1 分钟 K 线计算窗口起点至今的漂移 + 短期动量，logistic 映射成 P(涨)；与市场隐含价比较得到 edge，`edge >= MIN_ML_EDGE` 才下注。
- **结算**：到期后优先用 Gamma 的 `outcomePrices` 判定输赢，拿不到则用行情源对比（现价 vs 窗口起点价）兜底。

---

## 9. 目录结构（新引擎）

```
main.py               # 入口：--test-mode / --simulation / --live 分发 + 安全锁
supervisor.py         # 自动重启包装器
jybot/                # 自包含 5 分钟引擎（纸面路径仅依赖标准库）
  config.py           # 所有参数的唯一来源（读 .env）
  envload.py          # 极简 .env 加载器（无需 python-dotenv）
  markets.py          # 动态市场发现 + 盘口
  signal.py           # BTC 涨跌概率信号
  executor.py         # 固定份额限价下单（纸面 + 实盘 / FOK/FAK/GTC）
  engine.py           # 主循环 + 市场轮换
  state.py            # 持仓与纸面成交记录
scripts/
  check_config.py     # 配置校验
  health_check.py     # 连通性检查
  install.sh          # 一键安装
.env.example          # 配置模板
jy-bot.service        # systemd 服务
requirements.txt      # 依赖
```

> 仓库中原有的 Nautilus / 15 分钟相关模块（`core/`、`bot/strategy.py`、`patches/` 等）作为遗留代码保留，**不在新引擎的默认路径上**。新引擎完全自包含。

---

## 10. 常见问题

- **没装依赖能跑吗？** 能。`--test-mode` / `--simulation` 仅用标准库。实盘 `--live` 才需要 `py-clob-client`。
- **Binance 行情拉不到？** 部分地区封禁 Binance，已默认用 Coinbase，并会自动回退。
- **改了份额 / 滑点 / 模式要重启吗？** 改 `.env` 后重启进程即可（systemd 用 `systemctl restart jy-bot`）。不需要改代码。

---

## 11. Rust 核心（高性能 WebSocket 版，可选）

仓库内 `rust/` 目录是用 **Rust + tokio** 重写的核心机器人（参考 [leosysd/JY_RUST](https://github.com/leosysd/JY_RUST) 架构）。与 Python 版相比，它把行情/盘口判断从 HTTP 轮询升级为 **WebSocket 长连接 + 事件驱动**：

- **Polymarket 盘口 WS**：`wss://ws-subscriptions-clob.polymarket.com/ws/market`，订阅当前盘 Up/Down token，`book`/`price_change` 一到即更新缓存。
- **BTC 价格 WS**：Binance 公共镜像 aggTrade 流，每笔成交即推送。
- **事件驱动**：盘口或价格一更新立刻触发策略判断（`tokio::select!`），看到机会延迟≈0。
- **REST 兜底**：WS 断线或数据过期（`WS_STALENESS_SEC`）时自动回退 REST。
- **安全逻辑、`.env`、胜率/PnL 统计**：与 Python 版完全一致。
- **实盘下单**：Rust 负责全部行情/策略；真正提交订单复用 Python 官方 `py-clob-client`（`scripts/place_order.py`）。

```bash
cd rust && cargo build --release          # 编译（Linux VPS 上最顺）
./target/release/jybot-rs --test-mode     # 有界纸面演示
./target/release/jybot-rs --simulation    # WS 事件驱动纸面模拟
./target/release/jybot-rs check           # 检查配置
./target/release/jybot-rs stats           # 胜率 / PnL
./target/release/jybot-rs --live          # 实盘（需安全锁 + 输入 YES）
```

详见 [`rust/README.md`](rust/README.md)。Python 版与 Rust 版共用同一个 `.env` 和 `paper_trades.json`。

---

## 免责声明

加密货币与预测市场交易存在**重大亏损风险**。本软件仅用于学习与研究，过往表现不代表未来收益，作者不对任何资金损失负责。请先用模拟模式、小额资金，仅使用你能承受全部损失的资金进行交易。
