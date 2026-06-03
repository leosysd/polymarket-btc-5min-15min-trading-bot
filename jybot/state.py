"""
jybot.state -- position tracking and paper-trade persistence.

Trades are appended to ``paper_trades.json`` (configurable) in a schema that the
bundled ``scripts/view_trades.py`` and the ``main.py`` session dashboard can
read: each settled record carries ``outcome`` ∈ {WIN, LOSS} and ``pnl_usd``.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class Position:
    market_slug: str
    condition_id: str
    token_id: str
    outcome: str                # "Up" | "Down"
    direction: str              # "UP" | "DOWN"
    entry_price: float
    size: float
    entry_ts: float
    settle_ts: int
    p_model: float              # model probability for this side at entry
    ref_price: float = 0.0      # BTC spot at window start (settlement fallback)
    status: str = "OPEN"        # OPEN | CLOSED
    exit_price: Optional[float] = None
    exit_reason: str = ""
    outcome_result: str = ""    # WIN | LOSS | ""
    pnl_usd: float = 0.0

    @property
    def take_profit_price(self) -> float:
        # TP = entry + tp_pct * (1 - entry); set by engine using cfg
        return self.entry_price  # overridden via engine helper

    def to_record(self) -> dict:
        return {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(self.entry_ts)),
            "entry_ts": self.entry_ts,
            "market": self.market_slug,
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "side": self.direction,
            "outcome_token": self.outcome,
            "entry_price": round(self.entry_price, 4),
            "size": round(self.size, 4),
            "exit_price": round(self.exit_price, 4) if self.exit_price is not None else None,
            "status": self.status,
            "outcome": self.outcome_result or "OPEN",
            "pnl_usd": round(self.pnl_usd, 4),
            "exit_reason": self.exit_reason,
            "p_model": round(self.p_model, 4),
        }


class TradeLog:
    def __init__(self, path: str):
        self.path = path
        self._records: List[dict] = []
        self._load()

    def _load(self) -> None:
        p = Path(self.path)
        if p.exists():
            try:
                self._records = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(self._records, list):
                    self._records = []
            except Exception:
                self._records = []

    def append(self, record: dict) -> None:
        self._records.append(record)
        self._flush()

    def update_last_open(self, token_id: str, record: dict) -> bool:
        """Update the most recent OPEN record for a token (on settle/exit)."""
        for rec in reversed(self._records):
            if rec.get("token_id") == token_id and rec.get("status") == "OPEN":
                rec.update(record)
                self._flush()
                return True
        # not found -- append as a fresh record
        self.append(record)
        return False

    def _flush(self) -> None:
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._records, f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            # best-effort persistence; never crash the trading loop on disk error
            pass

    @property
    def records(self) -> List[dict]:
        return list(self._records)

    def summary(self) -> dict:
        settled = [r for r in self._records if r.get("outcome") in ("WIN", "LOSS")]
        wins = sum(1 for r in settled if r["outcome"] == "WIN")
        pnl = sum(r.get("pnl_usd", 0.0) for r in self._records)
        return {
            "trades": len(self._records),
            "settled": len(settled),
            "wins": wins,
            "losses": len(settled) - wins,
            "win_rate": (wins / len(settled) * 100.0) if settled else 0.0,
            "pnl_usd": pnl,
        }
