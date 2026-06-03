"""
scripts/place_order.py -- LIVE 下单助手（被 Rust 核心在实盘模式下调用）。

Rust 负责全部行情/盘口/策略判断（WebSocket）；真正"签名并提交订单"这一步
复用官方 py-clob-client。本脚本：
  * 从 stdin 读一行 JSON: {token_id, side, size, price, order_type}
  * 用 .env 凭证连接 CLOB，下限价单
  * 向 stdout 打印一行 JSON 结果: {status, filled_size, avg_price, order_id, reason}

所有日志走 stderr，stdout 只输出结果 JSON（Rust 解析 stdout）。

安全：再次校验 DRY_RUN=false 且 LIVE_TRADING=true，否则拒绝下单。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def out(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def main() -> int:
    try:
        raw = sys.stdin.read()
        req = json.loads(raw)
    except Exception as e:
        out({"status": "FAILED", "filled_size": 0, "avg_price": 0, "reason": f"bad input: {e}"})
        return 1

    token_id = str(req.get("token_id", ""))
    side = str(req.get("side", "")).upper()
    size = float(req.get("size", 0) or 0)
    price = float(req.get("price", 0) or 0)
    order_type = str(req.get("order_type", "FOK")).upper()

    try:
        from jybot.config import load_config
    except Exception as e:
        out({"status": "FAILED", "filled_size": 0, "avg_price": 0, "reason": f"config import: {e}"})
        return 1

    cfg = load_config()

    # 双重安全锁
    if not cfg.can_trade_live:
        out({
            "status": "REJECTED", "filled_size": 0, "avg_price": 0,
            "reason": "safety lock: DRY_RUN must be false AND LIVE_TRADING true",
        })
        return 0

    missing = cfg.credential_status()
    if missing:
        out({
            "status": "REJECTED", "filled_size": 0, "avg_price": 0,
            "reason": f"missing credentials: {','.join(missing)}",
        })
        return 0

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType, ApiCreds
        from py_clob_client.order_builder.constants import BUY, SELL
    except Exception as e:
        out({"status": "FAILED", "filled_size": 0, "avg_price": 0,
             "reason": f"py-clob-client not installed: {e}"})
        return 1

    try:
        client = ClobClient(
            host=cfg.clob_api_url,
            key=cfg.private_key,
            chain_id=cfg.chain_id,
            signature_type=cfg.signature_type,
            funder=cfg.funder_address or None,
        )
        client.set_api_creds(creds=ApiCreds(
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
            api_passphrase=cfg.api_passphrase,
        ))

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=BUY if side == "BUY" else SELL,
            fee_rate_bps=0,
        )
        signed = client.create_order(order_args)
        ot_map = {
            "FOK": OrderType.FOK,
            "FAK": getattr(OrderType, "FAK", OrderType.GTC),
            "GTC": OrderType.GTC,
        }
        resp = client.post_order(signed, orderType=ot_map.get(order_type, OrderType.FOK))
        err(f"[place_order] resp={resp}")

        order_id = (resp or {}).get("orderID") or (resp or {}).get("orderId")
        matched = float((resp or {}).get("makingAmount", 0) or 0)
        success = bool(resp and (resp.get("success") or order_id))
        status = "FILLED" if success and matched else ("RESTING" if success else "FAILED")
        out({
            "status": status,
            "filled_size": size if status == "FILLED" else 0.0,
            "avg_price": price,
            "order_id": order_id,
            "reason": str(resp)[:200],
        })
        return 0
    except Exception as e:
        out({"status": "FAILED", "filled_size": 0, "avg_price": 0, "reason": str(e)[:200]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
