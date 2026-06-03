"""
scripts/check_config.py -- validate that .env is complete and self-consistent.

Exit codes:
    0  configuration is valid for the current mode
    1  hard error (invalid values, or live-gate ON but credentials missing)

In the default safe configuration (DRY_RUN=true / LIVE_TRADING=false) this
passes even with placeholder credentials -- it only WARNS that live trading
would need them. Flip the live gate and the credential checks become hard
errors.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jybot.config import load_config, describe  # noqa: E402


GREEN = "OK   "
WARN = "WARN "
FAIL = "FAIL "


def main() -> int:
    cfg = load_config()
    errors = 0
    warnings = 0

    print("=" * 72)
    print("  CONFIGURATION CHECK  (.env)")
    print("=" * 72)
    print(describe(cfg))
    print("-" * 72)

    def check(ok: bool, name: str, detail: str = "", hard: bool = True) -> None:
        nonlocal errors, warnings
        if ok:
            tag = GREEN
        elif hard:
            tag = FAIL
            errors += 1
        else:
            tag = WARN
            warnings += 1
        line = f"  [{tag}] {name}"
        if detail:
            line += f" -- {detail}"
        print(line)

    # ── value sanity ──────────────────────────────────────────────────────
    check(cfg.market_interval in ("5m", "15m"), "MARKET_INTERVAL",
          f"{cfg.market_interval}")
    check(cfg.fixed_shares > 0, "FIXED_SHARES > 0", f"{cfg.fixed_shares}")
    check(cfg.order_type in ("FOK", "FAK", "GTC"), "ORDER_TYPE valid",
          f"{cfg.order_type}")
    check(0 <= cfg.min_entry_price < cfg.max_entry_price <= 1,
          "ENTRY price band 0 <= MIN < MAX <= 1",
          f"[{cfg.min_entry_price}, {cfg.max_entry_price}]")
    check(cfg.slippage_bps >= 0, "SLIPPAGE_BPS >= 0", f"{cfg.slippage_bps}")
    check(cfg.max_position_usdc > 0, "MAX_POSITION_USDC > 0",
          f"{cfg.max_position_usdc}")
    check(cfg.max_trades_per_market >= 1, "MAX_TRADES_PER_MARKET >= 1",
          f"{cfg.max_trades_per_market}")
    check(cfg.late_entry_cutoff_sec >= 0, "LATE_ENTRY_CUTOFF_SEC >= 0",
          f"{cfg.late_entry_cutoff_sec}")
    check(0 <= cfg.take_profit_pct <= 1, "TAKE_PROFIT_PCT in [0,1]",
          f"{cfg.take_profit_pct}")
    check(0 <= cfg.min_ml_edge <= 1, "MIN_ML_EDGE in [0,1]", f"{cfg.min_ml_edge}")
    if cfg.enable_stop_loss:
        check(0 < cfg.stop_loss_pct <= 1, "STOP_LOSS_PCT in (0,1]",
              f"{cfg.stop_loss_pct}")

    # notional vs sizing sanity (informational)
    est_notional = cfg.fixed_shares * cfg.max_entry_price
    check(est_notional <= cfg.max_position_usdc,
          "FIXED_SHARES * MAX_ENTRY_PRICE <= MAX_POSITION_USDC",
          f"~${est_notional:.2f} vs ${cfg.max_position_usdc:.2f} "
          f"(else top-priced entries are skipped)", hard=False)

    print("-" * 72)

    # ── credentials ───────────────────────────────────────────────────────
    missing = cfg.credential_status()
    live_gate = cfg.live_trading and not cfg.dry_run
    if missing:
        for name in missing:
            check(False, f"credential {name}",
                  "required for live trading", hard=live_gate)
    else:
        check(True, "live credentials present")

    check(bool(cfg.rpc_url), "RPC_URL set",
          "needed for on-chain settlement checks", hard=False)
    check(bool(cfg.wss_url), "WSS_URL set",
          "optional websocket stream", hard=False)

    print("=" * 72)
    if errors:
        print(f"  RESULT: FAIL  ({errors} error(s), {warnings} warning(s))")
        print("  Edit .env and re-run.  (Copy .env.example to .env to start.)")
        print("=" * 72)
        return 1
    mode = "LIVE (real money)" if live_gate else "DRY-RUN (paper)"
    print(f"  RESULT: OK  ({warnings} warning(s))  ->  mode = {mode}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
