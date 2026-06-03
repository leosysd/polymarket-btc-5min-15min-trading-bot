"""
scripts/health_check.py -- verify the bot can reach everything it needs.

Checks (in order):
    * Gamma API     -- market discovery
    * CLOB API      -- order books / order placement
    * BTC price feed -- the trading signal
    * Redis         -- optional live mode switching
    * RPC_URL       -- optional on-chain settlement
    * py-clob-client -- required only for live trading

Exit codes:
    0  all REQUIRED services reachable (Gamma + CLOB + price feed)
    1  a required service is unreachable
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jybot.config import load_config  # noqa: E402
from jybot.http import get_json_safe, get_json  # noqa: E402
from jybot.markets import discover_upcoming_markets, fetch_orderbook  # noqa: E402
from jybot.signal import compute_signal  # noqa: E402


def _row(ok, name, detail=""):
    tag = "OK  " if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if detail else ""))
    return ok


def main() -> int:
    cfg = load_config()
    required_ok = True

    print("=" * 72)
    print("  HEALTH CHECK")
    print("=" * 72)

    # ── Gamma ─────────────────────────────────────────────────────────────
    try:
        markets = discover_upcoming_markets(cfg, time.time(), count=3)
        ok = len(markets) > 0
        detail = (f"found {len(markets)} upcoming BTC {cfg.market_interval} market(s); "
                  f"next: {markets[0].slug}" if markets else "no markets found")
        required_ok &= _row(ok, f"Gamma API ({cfg.gamma_api_url})", detail)
    except Exception as exc:
        required_ok &= _row(False, "Gamma API", str(exc)[:80])
        markets = []

    # ── CLOB ──────────────────────────────────────────────────────────────
    try:
        import urllib.request
        req = urllib.request.Request(f"{cfg.clob_api_url}/ok",
                                     headers={"User-Agent": "jybot-health"})
        with urllib.request.urlopen(req, timeout=10) as r:
            clob_ok = r.status == 200
        detail = f"{cfg.clob_api_url}/ok -> {r.status}"
        # bonus: prove we can read a real book
        if markets and markets[0].up:
            book = fetch_orderbook(cfg, markets[0].up.token_id)
            if book and book.best_ask:
                detail += f"; book ask={book.best_ask.price:.3f}"
        required_ok &= _row(clob_ok, "CLOB API", detail)
    except Exception as exc:
        required_ok &= _row(False, "CLOB API", str(exc)[:80])

    # ── price feed ────────────────────────────────────────────────────────
    try:
        sig = compute_signal(cfg, int(time.time() // cfg.interval_seconds * cfg.interval_seconds))
        ok = sig.spot is not None
        detail = (f"{cfg.price_feed} spot={sig.spot:.1f} p_up={sig.p_up:.3f}"
                  if sig.spot else "no price data")
        required_ok &= _row(ok, "BTC price feed", detail)
    except Exception as exc:
        required_ok &= _row(False, "BTC price feed", str(exc)[:80])

    print("-" * 72)

    # ── Redis (optional) ──────────────────────────────────────────────────
    try:
        import redis  # type: ignore
        client = redis.Redis(host=cfg.redis_host, port=cfg.redis_port,
                             db=cfg.redis_db, socket_connect_timeout=3)
        client.ping()
        _row(True, "Redis (optional)", f"{cfg.redis_host}:{cfg.redis_port} db={cfg.redis_db}")
    except ImportError:
        _row(True, "Redis (optional)", "redis lib not installed -- live mode switch disabled")
    except Exception as exc:
        _row(True, "Redis (optional)", f"not reachable: {str(exc)[:50]}")

    # ── RPC (optional) ────────────────────────────────────────────────────
    if cfg.rpc_url:
        try:
            import json as _json
            import urllib.request
            payload = _json.dumps({"jsonrpc": "2.0", "method": "eth_blockNumber",
                                   "params": [], "id": 1}).encode()
            req = urllib.request.Request(cfg.rpc_url, data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = _json.loads(r.read())
            block = int(data.get("result", "0x0"), 16)
            _row(True, "RPC_URL (optional)", f"block={block}")
        except Exception as exc:
            _row(True, "RPC_URL (optional)", f"not reachable: {str(exc)[:50]}")
    else:
        _row(True, "RPC_URL (optional)", "not set")

    # ── py-clob-client (needed for live) ──────────────────────────────────
    try:
        import py_clob_client  # type: ignore  # noqa: F401
        _row(True, "py-clob-client", "installed (live trading ready)")
    except Exception:
        _row(True, "py-clob-client", "not installed -- needed only for --live "
             "(pip install py-clob-client)")

    print("=" * 72)
    if required_ok:
        print("  RESULT: OK -- required services reachable")
        print("=" * 72)
        return 0
    print("  RESULT: FAIL -- a required service is unreachable (see above)")
    print("=" * 72)
    return 1


if __name__ == "__main__":
    sys.exit(main())
