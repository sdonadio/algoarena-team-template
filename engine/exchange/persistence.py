"""
exchange/persistence.py — season state on disk.

The season is the meta-game: portfolios, cumulative P&L, and the equity
history that season scoring is computed from all survive across sessions and
across exchange restarts. Everything lives in one JSON file (SEASON_PATH,
default data/season.json), written:

  * on `end_session` / `close_session`
  * every SEASON_SAVE_INTERVAL_SEC while a session is open
  * and read once at startup

Design notes
------------
* Plain dicts, one version field. A save from an older build must never
  crash a newer exchange, so every field is read defensively.
* Positions/avg_cost keys are symbols, which JSON keeps as strings — fine.
* `data/` is gitignored: a season file contains live student capital and is
  environment-specific, never source.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import tempfile
import time
from typing import Any

logger = logging.getLogger(__name__)

SEASON_VERSION = 1

SEASON_PATH = os.environ.get(
    "SEASON_PATH",
    str(pathlib.Path(__file__).parent.parent / "data" / "season.json"),
)

# How often to checkpoint while a session is open (seconds).
SEASON_SAVE_INTERVAL_SEC = float(os.environ.get("SEASON_SAVE_INTERVAL_SEC", "60"))

# Cap on stored equity snapshots per team (oldest dropped first).
EQUITY_HISTORY_MAX = int(os.environ.get("EQUITY_HISTORY_MAX", "2000"))


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def portfolio_to_dict(p: Any) -> dict[str, Any]:
    """Serialise one Portfolio, including every accounting field."""
    return {
        "team_id": p.team_id,
        "role": p.role,
        "level": p.level,
        "cash": p.cash,
        "positions": {k: int(v) for k, v in p.positions.items()},
        "avg_cost": {k: float(v) for k, v in p.avg_cost.items()},
        "realized_pnl": p.realized_pnl,
        "total_fees_paid": p.total_fees_paid,
        "total_rebates_earned": p.total_rebates_earned,
        "total_carry_paid": p.total_carry_paid,
        "starting_cash": p.starting_cash,
        "liquidated": bool(p.liquidated),
    }


def portfolio_from_dict(cls: type, raw: dict[str, Any]) -> Any:
    """Rebuild a Portfolio from a saved dict, tolerating missing fields."""
    p = cls(
        team_id=str(raw.get("team_id", "")),
        role=str(raw.get("role", "trader")),
        level=int(raw.get("level", 1)),
        cash=float(raw.get("cash", 0.0)),
    )
    p.positions = {str(k): int(v) for k, v in (raw.get("positions") or {}).items()}
    p.avg_cost = {str(k): float(v) for k, v in (raw.get("avg_cost") or {}).items()}
    p.realized_pnl = float(raw.get("realized_pnl", 0.0))
    p.total_fees_paid = float(raw.get("total_fees_paid", 0.0))
    p.total_rebates_earned = float(raw.get("total_rebates_earned", 0.0))
    p.total_carry_paid = float(raw.get("total_carry_paid", 0.0))
    p.starting_cash = float(raw.get("starting_cash", p.cash) or p.cash)
    p.liquidated = bool(raw.get("liquidated", False))
    return p


def build_state(server: Any) -> dict[str, Any]:
    """Snapshot everything about the season that must outlive the process."""
    return {
        "version": SEASON_VERSION,
        "saved_at": time.time(),
        "week": server.scenario.week,
        "tick": server.tick,
        "exchange_revenue": server.exchange_revenue,
        "sessions_played": server.sessions_played,
        "portfolios": {
            tid: portfolio_to_dict(p) for tid, p in server.portfolios.items()
        },
        "equity_history": {
            tid: [[int(t), float(v)] for t, v in hist]
            for tid, hist in server.equity_history.items()
        },
        # Primary-market state: which deals already happened (the durable
        # done-once guard) and what each listed security needs to be
        # re-registered on restart — without this, a restart strands every
        # IPO position in a symbol the venue no longer knows.
        "ipo": {
            "listed": {
                sym: dict(rec, last_price=float(
                    server.ref_prices.get(sym) or rec.get("offer_price", 0.0)))
                for sym, rec in server.listed_ipos.items()
            },
            "issued": {k: int(v) for k, v in server.ipo_issued.items()},
            "proceeds": float(server.ipo_proceeds),
            "allocations": {k: int(v)
                            for k, v in server.ipo_allocations.items()},
        },
    }


# ---------------------------------------------------------------------------
# Disk I/O
# ---------------------------------------------------------------------------

def save(state: dict[str, Any], path: str | None = None) -> bool:
    """Write the season file atomically. Returns True on success.

    Atomic because the checkpoint runs while students are trading: a torn
    write would lose a whole season.
    """
    target = path or SEASON_PATH
    try:
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(os.path.abspath(target)), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
        os.replace(tmp, target)
        return True
    except OSError as exc:
        logger.warning("Season save failed (%s)", exc)
        return False


def load(path: str | None = None) -> dict[str, Any] | None:
    """Read the season file, or None when absent/corrupt."""
    target = path or SEASON_PATH
    try:
        with open(target) as f:
            state = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Season file unreadable (%s) — starting fresh", exc)
        return None
    if not isinstance(state, dict):
        return None
    return state


def wipe(path: str | None = None) -> bool:
    """Delete the season file (new_season). True if a file was removed."""
    target = path or SEASON_PATH
    try:
        os.remove(target)
        return True
    except OSError:
        return False
