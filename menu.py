"""
menu.py -- 终端交互式配置 / 启动菜单（纯 Python 标准库实现）。

运行：  python menu.py

功能：
  1. 启动机器人（test-mode / simulation / supervisor / live 带二次确认）
  2. 读取并修改 .env（分组、类型校验、密钥打码、危险开关需输入 YES）
  3. 工具（check_config / health_check / view_trades / regen_keys / 查看日志）

安全：
  * 没有 .env 时自动从 .env.example 复制
  * 每次写入前自动备份为 .env.backup-时间戳
  * 私钥 / API_KEY / API_SECRET / PASSPHRASE 显示时打码，绝不明文打印
  * LIVE_TRADING=true 或 DRY_RUN=false 需明确输入 YES 才保存
  * 只写入 .env，绝不修改 jybot/config.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from collections import namedtuple
from pathlib import Path

# 防御性：重定向到管道/文件时，避免不可编码字符触发 UnicodeEncodeError。
# 真实控制台仍走宽字符输出，中文正常显示。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # Python 3.7+
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = ROOT / ".env.example"
LOG_DIR = ROOT / "logs"
PY = sys.executable

# ── 字段定义 ────────────────────────────────────────────────────────────────
# Field(key, type, choices, secret, desc)
#   type   : 'bool' | 'int' | 'float' | 'enum' | 'str'
#   choices: enum 的合法取值（字符串列表）
#   secret : 显示时是否打码
Field = namedtuple("Field", "key type choices secret desc")


def F(key, type="str", choices=None, secret=False, desc=""):
    return Field(key, type, choices, secret, desc)


GROUPS = [
    ("市场周期", [
        F("MARKET_INTERVAL", "enum", ["5m", "15m"], desc="交易市场周期"),
    ]),
    ("安全开关", [
        F("DRY_RUN", "bool", desc="true=纸面模拟(安全)"),
        F("LIVE_TRADING", "bool", desc="true=允许实盘(危险)"),
    ]),
    ("钱包 / API", [
        F("POLYMARKET_PRIVATE_KEY", "str", secret=True, desc="签名订单的 EOA 私钥"),
        F("POLYMARKET_API_KEY", "str", secret=True, desc="CLOB API key"),
        F("POLYMARKET_API_SECRET", "str", secret=True, desc="CLOB API secret"),
        F("POLYMARKET_API_PASSPHRASE", "str", secret=True, desc="CLOB API passphrase"),
        F("PM_FUNDER_ADDRESS", "str", desc="持有 USDC 的地址"),
        F("POLYMARKET_SIG_TYPE", "enum", ["0", "1", "2"], desc="0=EOA 1=PROXY 2=SAFE"),
        F("CHAIN_ID", "int", desc="链 ID(Polygon=137)"),
    ]),
    ("网络", [
        F("RPC_URL", "str", desc="Polygon RPC 节点"),
        F("WSS_URL", "str", desc="可选 websocket"),
        F("GAMMA_API_URL", "str", desc="Gamma API(留空用默认)"),
        F("CLOB_API_URL", "str", desc="CLOB API(留空用默认)"),
    ]),
    ("下单设置", [
        F("FIXED_SHARES", "float", desc="每单固定份额(最小5)"),
        F("ORDER_TYPE", "enum", ["FOK", "FAK", "GTC"], desc="订单类型"),
        F("SLIPPAGE_BPS", "float", desc="滑点(100=1%)"),
        F("MAX_POSITION_USDC", "float", desc="单仓名义上限"),
        F("MAX_TRADES_PER_MARKET", "int", desc="每市场最多下单次数"),
    ]),
    ("入场过滤", [
        F("MIN_ENTRY_PRICE", "float", desc="最低入场价"),
        F("MAX_ENTRY_PRICE", "float", desc="最高入场价"),
        F("MAX_SPREAD_PCT", "float", desc="最大点差占比"),
        F("LATE_ENTRY_CUTOFF_SEC", "int", desc="距结算<此值不进场"),
        F("EARLY_ENTRY_CUTOFF_SEC", "int", desc="开盘后多少秒才进场"),
        F("MIN_ML_EDGE", "float", desc="最小模型优势(0.10=10pp)"),
    ]),
    ("离场设置", [
        F("TAKE_PROFIT_PCT", "float", desc="止盈比例"),
        F("ENABLE_STOP_LOSS", "bool", desc="是否启用止损"),
        F("STOP_LOSS_PCT", "float", desc="止损比例"),
    ]),
    ("运行 / 信号", [
        F("POLL_INTERVAL_SEC", "float", desc="主循环轮询间隔(秒)"),
        F("SIGNAL_LOOKBACK_MIN", "int", desc="信号回看分钟数"),
        F("PRICE_FEED", "enum", ["coinbase", "binance"], desc="行情源"),
    ]),
    ("Redis", [
        F("REDIS_HOST", "str", desc="Redis 主机"),
        F("REDIS_PORT", "int", desc="Redis 端口"),
        F("REDIS_DB", "int", desc="Redis DB 编号"),
    ]),
]

_PLACEHOLDERS = {"", "0x...", "...", "your_key"}


# ── 通用 IO ─────────────────────────────────────────────────────────────────

def clear():
    os.system("cls" if os.name == "nt" else "clear")


def hr(char="=", width=64):
    print(char * width)


def pause():
    try:
        input("\n按回车返回菜单...")
    except (EOFError, KeyboardInterrupt):
        print()


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return ""
    except KeyboardInterrupt:
        print()
        return ""


# ── .env 读写 ───────────────────────────────────────────────────────────────

def ensure_env():
    """没有 .env 时从 .env.example 复制生成。"""
    if ENV_PATH.exists():
        return
    if EXAMPLE_PATH.exists():
        shutil.copy2(EXAMPLE_PATH, ENV_PATH)
        print(f"[i] 未找到 .env，已从 .env.example 复制生成: {ENV_PATH.name}")
    else:
        ENV_PATH.write_text("", encoding="utf-8")
        print("[i] 未找到 .env / .env.example，已创建空 .env")
    time.sleep(1)


def read_env_values():
    """解析 .env 为 {KEY: VALUE}（忽略注释/空行）。"""
    values = {}
    if not ENV_PATH.exists():
        return values
    for raw in ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # 去掉行内注释（未加引号时）
        if val[:1] not in ("'", '"'):
            h = val.find(" #")
            if h != -1:
                val = val[:h].strip()
        else:
            q = val[0]
            end = val.find(q, 1)
            if end != -1:
                val = val[1:end]
        values[key] = val
    return values


def backup_env():
    if not ENV_PATH.exists():
        return None
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = ROOT / f".env.backup-{ts}"
    shutil.copy2(ENV_PATH, dst)
    return dst.name


def write_env_value(key, value):
    """写入单个 key（备份后，保留注释/顺序原地替换；不存在则追加）。"""
    backup_name = backup_env()
    lines = ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines() \
        if ENV_PATH.exists() else []
    pattern = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=")
    replaced = False
    for i, raw in enumerate(lines):
        if pattern.match(raw):
            lines[i] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return backup_name


# ── 显示工具 ────────────────────────────────────────────────────────────────

def mask(value):
    """密钥打码显示。"""
    if value is None or value.strip() in _PLACEHOLDERS:
        return "(未设置)"
    v = value.strip()
    if len(v) <= 8:
        return "****"
    return f"{v[:4]}****{v[-4:]}"


def display_value(field, value):
    if value is None or value == "":
        shown = "(未设置)"
    elif field.secret:
        shown = mask(value)
    else:
        shown = value
    return shown


# ── 类型校验 ────────────────────────────────────────────────────────────────

def validate(field, raw):
    """返回 (ok, normalized_value_or_errmsg)。"""
    raw = raw.strip()
    t = field.type
    if t == "bool":
        low = raw.lower()
        if low in ("1", "true", "yes", "y", "on"):
            return True, "true"
        if low in ("0", "false", "no", "n", "off"):
            return True, "false"
        return False, "请输入 true/false（或 1/0、y/n）"
    if t == "int":
        try:
            return True, str(int(raw))
        except ValueError:
            return False, "请输入整数"
    if t == "float":
        try:
            return True, str(float(raw))
        except ValueError:
            return False, "请输入数字"
    if t == "enum":
        if raw in field.choices:
            return True, raw
        return False, f"只能是: {', '.join(field.choices)}"
    # str —— 允许任意（含空）
    return True, raw


# ── 启动菜单 ────────────────────────────────────────────────────────────────

def run_cmd(args, long_running=False):
    """运行子进程；long_running 时允许 Ctrl+C 结束并返回菜单。"""
    print()
    hr("-")
    print(f"运行: {' '.join(args)}")
    if long_running:
        print("（这是长期运行任务，按 Ctrl+C 可停止并返回菜单）")
    hr("-")
    try:
        subprocess.run(args, cwd=str(ROOT))
    except KeyboardInterrupt:
        print("\n[i] 已停止，返回菜单。")
    except FileNotFoundError as e:
        print(f"[!] 无法运行: {e}")


def find_rust_bin():
    """定位已编译的 Rust 二进制 jybot-rs（找不到返回 None）。"""
    exe = "jybot-rs.exe" if os.name == "nt" else "jybot-rs"
    cands = [
        ROOT / "rust" / "target" / "release" / exe,
        ROOT / "rust" / "target" / "debug" / exe,
    ]
    tgt = os.environ.get("CARGO_TARGET_DIR")
    if tgt:
        cands += [Path(tgt) / "release" / exe, Path(tgt) / "debug" / exe]
    for c in cands:
        if c.exists():
            return c
    return None


def run_rust(extra_args, long_running=False):
    rb = find_rust_bin()
    if rb is None:
        print("\n  未找到 Rust 二进制 jybot-rs。请先编译：")
        print("    cd rust && cargo build --release")
        print("  （Linux VPS 上最顺；详见 rust/README.md）")
        return
    run_cmd([str(rb), *extra_args], long_running=long_running)


def launch_menu():
    while True:
        clear()
        rust_state = "已编译" if find_rust_bin() else "未编译"
        hr()
        print("  启动机器人")
        hr()
        print("  -- Python 引擎 --")
        print("  1) 测试模式     python main.py --test-mode   (有界纸面演示)")
        print("  2) 模拟交易     python main.py --simulation  (纸面, 真实时钟)")
        print("  3) 模拟+守护    python supervisor.py --simulation (自动重启)")
        print("  4) 实盘 LIVE    python main.py --live        [危险, 需确认]")
        print(f"  -- Rust 引擎 (WebSocket 事件驱动, {rust_state}) --")
        print("  5) Rust 测试    jybot-rs --test-mode")
        print("  6) Rust 模拟    jybot-rs --simulation")
        print("  7) Rust 统计    jybot-rs stats")
        print("  8) Rust 实盘    jybot-rs --live              [危险, 需确认]")
        print("  0) 返回上级菜单")
        hr("-")
        c = ask("请选择: ")
        if c == "1":
            run_cmd([PY, "main.py", "--test-mode"])
            pause()
        elif c == "2":
            run_cmd([PY, "main.py", "--simulation"], long_running=True)
            pause()
        elif c == "3":
            run_cmd([PY, "supervisor.py", "--simulation"], long_running=True)
            pause()
        elif c == "4":
            launch_live()
            pause()
        elif c == "5":
            run_rust(["--test-mode"])
            pause()
        elif c == "6":
            run_rust(["--simulation"], long_running=True)
            pause()
        elif c == "7":
            run_rust(["stats"])
            pause()
        elif c == "8":
            launch_live_rust()
            pause()
        elif c in ("0", "q", ""):
            return


def launch_live_rust():
    vals = read_env_values()
    dry = vals.get("DRY_RUN", "true").lower()
    live = vals.get("LIVE_TRADING", "false").lower()
    clear()
    hr("!")
    print("  Rust 实盘 LIVE —— 真实资金风险")
    hr("!")
    print(f"  当前 .env:  DRY_RUN={dry}   LIVE_TRADING={live}")
    if dry != "false" or live != "true":
        print("\n  [安全锁未打开] 需先设置 DRY_RUN=false 且 LIVE_TRADING=true。")
        print("  （即便强行运行，jybot-rs 也会拒绝启动）")
        return
    print("\n  风险提示: 将向 Polymarket 提交真实订单。")
    if ask('\n  确认请输入大写 YES: ') != "YES":
        print("  已取消。")
        return
    run_rust(["--live"], long_running=True)


def launch_live():
    vals = read_env_values()
    dry = vals.get("DRY_RUN", "true").lower()
    live = vals.get("LIVE_TRADING", "false").lower()

    clear()
    hr("!")
    print("  实盘 LIVE 交易 —— 真实资金风险")
    hr("!")
    print(f"  当前 .env:  DRY_RUN={dry}   LIVE_TRADING={live}")
    print()
    if dry != "false" or live != "true":
        print("  [安全锁未打开] 当前配置不允许实盘。")
        print("  需要先在『配置菜单』里设置:  DRY_RUN=false  且  LIVE_TRADING=true")
        print("  （即便强行运行，main.py 也会拒绝启动）")
        return
    print("  风险提示: 5 分钟二元市场结算后，输的一方亏损约 100% 本金。")
    print("  这将向 Polymarket 提交真实订单。")
    print()
    confirm = ask('  确认实盘请输入大写 YES（其它任意键取消）: ')
    if confirm != "YES":
        print("  已取消。")
        return
    run_cmd([PY, "main.py", "--live"], long_running=True)


# ── 配置菜单 ────────────────────────────────────────────────────────────────

def config_menu():
    while True:
        clear()
        hr()
        print("  配置 .env   （文件: %s）" % ENV_PATH.name)
        hr()
        for i, (group, _fields) in enumerate(GROUPS, 1):
            print(f"  {i}) {group}")
        print("  0) 返回上级菜单")
        hr("-")
        c = ask("选择分组: ")
        if c in ("0", "q", ""):
            return
        if c.isdigit() and 1 <= int(c) <= len(GROUPS):
            group_menu(GROUPS[int(c) - 1])


def group_menu(group_def):
    group_name, fields = group_def
    while True:
        clear()
        vals = read_env_values()
        hr()
        print(f"  分组: {group_name}")
        hr()
        for i, f in enumerate(fields, 1):
            cur = display_value(f, vals.get(f.key, ""))
            print(f"  {i:>2}) {f.key:<26} = {cur}")
            if f.desc:
                print(f"       {f.desc}")
        print("   0) 返回")
        hr("-")
        c = ask("选择要修改的项: ")
        if c in ("0", "q", ""):
            return
        if c.isdigit() and 1 <= int(c) <= len(fields):
            edit_field(fields[int(c) - 1])


def edit_field(field):
    vals = read_env_values()
    cur = vals.get(field.key, "")
    clear()
    hr()
    print(f"  修改: {field.key}")
    if field.desc:
        print(f"  说明: {field.desc}")
    print(f"  当前: {display_value(field, cur)}")
    if field.type == "enum":
        print(f"  可选: {', '.join(field.choices)}")
    elif field.type == "bool":
        print("  可选: true / false")
    hr("-")
    raw = ask("输入新值（直接回车=取消）: ")
    if raw == "":
        print("  未修改。")
        return

    ok, result = validate(field, raw)
    if not ok:
        print(f"  [!] 校验失败: {result}")
        pause()
        return

    # 危险开关确认
    if not danger_confirm(field, result):
        print("  已取消（未保存）。")
        pause()
        return

    backup = write_env_value(field.key, result)
    shown = mask(result) if field.secret else result
    print(f"  [OK] 已保存 {field.key} = {shown}")
    if backup:
        print(f"       备份: {backup}")

    # 修改后提示运行 check_config
    if ask("\n  立即运行 check_config.py 校验? (y/N): ").lower() == "y":
        run_cmd([PY, "scripts/check_config.py"])
    pause()


def danger_confirm(field, new_value):
    """对危险开关要求输入大写 YES。返回是否允许保存。"""
    risky = (
        (field.key == "LIVE_TRADING" and new_value == "true")
        or (field.key == "DRY_RUN" and new_value == "false")
    )
    if not risky:
        return True
    print()
    hr("!")
    print("  危险变更 —— 这会让机器人更接近『真实下单』")
    print(f"    {field.key} = {new_value}")
    print("  只有 DRY_RUN=false 且 LIVE_TRADING=true 时，--live 才会用真钱下单。")
    hr("!")
    return ask("  确认请输入大写 YES: ") == "YES"


# ── 工具菜单 ────────────────────────────────────────────────────────────────

def tools_menu():
    while True:
        clear()
        hr()
        print("  工具")
        hr()
        print("  1) 检查配置     python scripts/check_config.py")
        print("  2) 健康检查     python scripts/health_check.py")
        print("  3) 查看交易     python scripts/view_trades.py")
        print("  4) 生成API密钥  python scripts/regen_polymarket_keys.py")
        print("  5) 查看最近日志")
        print("  0) 返回上级菜单")
        hr("-")
        c = ask("请选择: ")
        if c == "1":
            run_cmd([PY, "scripts/check_config.py"]); pause()
        elif c == "2":
            run_cmd([PY, "scripts/health_check.py"]); pause()
        elif c == "3":
            run_cmd([PY, "scripts/view_trades.py"]); pause()
        elif c == "4":
            run_cmd([PY, "scripts/regen_polymarket_keys.py"]); pause()
        elif c == "5":
            view_logs(); pause()
        elif c in ("0", "q", ""):
            return


def view_logs(tail=40):
    print()
    if not LOG_DIR.exists():
        print("  暂无 logs/ 目录（运行一次机器人后会生成）。")
        return
    logs = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        print("  logs/ 下暂无日志文件。")
        return
    latest = logs[0]
    print(f"  最近日志: {latest.name}  （末尾 {tail} 行）")
    hr("-")
    try:
        lines = latest.read_text(encoding="utf-8", errors="ignore").splitlines()
        for line in lines[-tail:]:
            print("  " + line)
    except Exception as e:
        print(f"  [!] 读取失败: {e}")


# ── 主菜单 ──────────────────────────────────────────────────────────────────

def main_menu():
    while True:
        clear()
        vals = read_env_values()
        mode = "DRY-RUN (纸面)"
        if vals.get("LIVE_TRADING", "false").lower() == "true" and \
           vals.get("DRY_RUN", "true").lower() == "false":
            mode = "LIVE (实盘已解锁!)"
        hr()
        print("  Polymarket BTC 5 分钟 UP/DOWN 机器人 —— 配置 / 启动菜单")
        hr()
        print(f"  当前模式: {mode}   |   周期: {vals.get('MARKET_INTERVAL', '5m')}"
              f"   |   .env: {'存在' if ENV_PATH.exists() else '缺失'}")
        hr("-")
        print("  1) 启动机器人")
        print("  2) 配置 .env")
        print("  3) 工具")
        print("  0) 退出")
        hr("-")
        c = ask("请选择: ")
        if c == "1":
            launch_menu()
        elif c == "2":
            config_menu()
        elif c == "3":
            tools_menu()
        elif c in ("0", "q"):
            print("再见。")
            return


def main():
    ensure_env()
    try:
        main_menu()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
