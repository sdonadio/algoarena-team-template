"""
exchange/scenario.py — week scenarios (the season's gate system).

A scenario file describes one week of the season: which mechanics are
unlocked, the position limit, the market-event calendar, and whether the
week counts toward the season score. The exchange loads one at startup
(SCENARIO_PATH, or GAME_WEEK to pick teacher/season/weekNN.json) and the
teacher can switch live with the `set_week` command.

Schema (see docs/SEASON_GUIDE.md for the authoritative reference):

    {
      "week": 4,
      "label": "Market making",
      "flags": {
        "shorts_allowed": true,
        "post_only_allowed": true,
        "circuit_breakers": false,
        "cancellation_fees": false,
        "latency_enabled": false,
        "multi_venue": true,
        "futures_enabled": false,
        "purchase_window": true
      },
      "position_limit": 1000,
      "scoring_counts": true,
      "events": [ {"kind": "earnings", "symbol": "NVDA", "tick": 400,
                   "magnitude_range": [0.03, 0.08]} ]
    }

Backward compatibility: with no scenario file the exchange runs
`OPEN_PLAY` — every flag permissive and no config override — so local play
(`make exchange` + `make bots`) behaves exactly as it did before the season
system existed.
"""

from __future__ import annotations

import json
import os
import pathlib
import random
from dataclasses import dataclass, field
from typing import Any

import exchange.config as config

SEASON_DIR = os.environ.get(
    "SEASON_DIR",
    str(pathlib.Path(__file__).parent.parent / "teacher" / "season"),
)

# Every gate, with the value that reproduces pre-season behaviour.
DEFAULT_FLAGS: dict[str, bool] = {
    "shorts_allowed":    True,
    "post_only_allowed": True,
    "circuit_breakers":  False,
    "cancellation_fees": False,
    "latency_enabled":   False,
    "multi_venue":       True,
    "futures_enabled":   False,
    "purchase_window":   False,
}

# Flags that map onto an exchange.config global when the scenario is applied.
_FLAG_TO_CONFIG: dict[str, str] = {
    "circuit_breakers": "CIRCUIT_BREAKERS_ENABLED",
}

# Config globals a scenario file may override via its "config" block —
# the market-structure mechanics that phase in across the season. A
# whitelist, because a scenario file is class data, not code: it may tune
# the market's physics, never arbitrary engine internals.
ALLOWED_CONFIG_OVERRIDES: dict[str, type] = {
    "OPENING_AUCTION_TICKS": int,     # pre-open length; 0 = open directly
    "CLOSING_AUCTION": bool,          # closing cross at session end
    "CLOSING_AUCTION_TICKS": int,
    "SSR_TRIGGER_PCT": float,         # short-sale rule trip point; 0 = off
    "SHORT_LOCATE_CAP": int,          # market-wide borrow pool; 0 = unlimited
    "LULD_BAND_PCT": float,           # erroneous-order collar; 0 = off
}


@dataclass
class Scenario:
    """One week's rule set."""

    week: int = 0
    label: str = "Open play"
    flags: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_FLAGS))
    position_limit: int | None = None
    # Order/cancel messages allowed per tick (see exchange/limits.py). None
    # leaves the generous config default, which never bites.
    order_quota: int | None = None
    # Maintenance margin as a fraction of starting capital. None keeps the
    # config default (0.10). Week 5 raises it so the liquidation lesson has
    # teeth: at 0.10, 30 simulated seasons produced zero liquidations.
    maintenance_fraction: float | None = None
    scoring_counts: bool = True
    events: list[dict[str, Any]] = field(default_factory=list)
    # Whitelisted exchange.config overrides (see ALLOWED_CONFIG_OVERRIDES).
    config_overrides: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    # Config values saved by apply(), restored by restore().
    _saved: dict[str, Any] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def flag(self, name: str) -> bool:
        """Value of one gate flag (permissive default if unknown)."""
        return bool(self.flags.get(name, DEFAULT_FLAGS.get(name, True)))

    def flag_summary(self) -> str:
        """One-line human summary of what is unlocked."""
        on = [k for k, v in sorted(self.flags.items()) if v]
        off = [k for k, v in sorted(self.flags.items()) if not v]
        lim = self.position_limit if self.position_limit is not None else "config"
        quota = self.order_quota if self.order_quota is not None else "config"
        return (f"week {self.week} · limit {lim} · quota {quota} · "
                f"scoring {'ON' if self.scoring_counts else 'OFF'} · "
                f"on: {', '.join(on) or '—'} · off: {', '.join(off) or '—'}")

    def to_dict(self) -> dict[str, Any]:
        """Public description, safe to broadcast to students.

        Event *directions* are deliberately excluded — see
        exchange/calendar.py: the calendar announces timing, not outcome.
        """
        return {
            "week": self.week,
            "label": self.label,
            "flags": dict(self.flags),
            "position_limit": self.position_limit,
            "order_quota": self.order_quota,
            "maintenance_fraction": self.maintenance_fraction,
            "scoring_counts": self.scoring_counts,
            "config": dict(self.config_overrides),
            "events": [
                {k: v for k, v in ev.items() if k != "direction"}
                for ev in self.events
            ],
        }

    # ------------------------------------------------------------------
    # Config overrides
    # ------------------------------------------------------------------

    def apply(self) -> None:
        """Push this week's overrides onto exchange.config module globals.

        Only keys the scenario actually specifies are touched, so anything
        the teacher set by env var stays in force.
        """
        self.restore()
        if self.position_limit is not None:
            self._saved["POSITION_LIMIT_SHARES"] = config.POSITION_LIMIT_SHARES
            config.POSITION_LIMIT_SHARES = int(self.position_limit)
        if self.order_quota is not None:
            self._saved["ORDER_QUOTA_PER_TICK"] = config.ORDER_QUOTA_PER_TICK
            config.ORDER_QUOTA_PER_TICK = float(self.order_quota)
        if self.maintenance_fraction is not None:
            self._saved["MAINTENANCE_FRACTION"] = config.MAINTENANCE_FRACTION
            config.MAINTENANCE_FRACTION = float(self.maintenance_fraction)
        for flag, attr in _FLAG_TO_CONFIG.items():
            if flag in self.flags:
                self._saved[attr] = getattr(config, attr)
                setattr(config, attr, bool(self.flags[flag]))
        for attr, value in self.config_overrides.items():
            caster = ALLOWED_CONFIG_OVERRIDES.get(attr)
            if caster is None:
                continue
            self._saved[attr] = getattr(config, attr)
            setattr(config, attr, caster(value))

    def restore(self) -> None:
        """Undo apply()."""
        for attr, value in self._saved.items():
            setattr(config, attr, value)
        self._saved = {}


# The permissive scenario used whenever no week is configured.
def open_play() -> Scenario:
    """The default rule set: everything allowed, nothing overridden."""
    return Scenario()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def from_dict(raw: dict[str, Any], source: str = "") -> Scenario:
    """Build a Scenario from parsed JSON, filling defaults for absent keys."""
    flags = dict(DEFAULT_FLAGS)
    for key, value in (raw.get("flags") or {}).items():
        flags[key] = bool(value)
    limit = raw.get("position_limit")
    maint = raw.get("maintenance_fraction")
    quota = raw.get("order_quota")
    return Scenario(
        week=int(raw.get("week", 0)),
        label=str(raw.get("label", "")) or f"Week {raw.get('week', 0)}",
        flags=flags,
        position_limit=int(limit) if limit is not None else None,
        maintenance_fraction=float(maint) if maint is not None else None,
        order_quota=int(quota) if quota is not None else None,
        scoring_counts=bool(raw.get("scoring_counts", True)),
        events=list(raw.get("events") or []),
        config_overrides={k: v for k, v in (raw.get("config") or {}).items()
                          if k in ALLOWED_CONFIG_OVERRIDES},
        source=source,
    )


def load_scenario(path: str) -> Scenario:
    """Load one scenario file. Raises FileNotFoundError / ValueError."""
    with open(path) as f:
        return from_dict(json.load(f), source=path)


def week_path(week: int) -> str:
    return os.path.join(SEASON_DIR, f"week{int(week):02d}.json")


def load_week(week: int) -> Scenario:
    """Load teacher/season/weekNN.json. Raises FileNotFoundError."""
    return load_scenario(week_path(week))


def active_scenario() -> Scenario:
    """The scenario the exchange should boot with.

    SCENARIO_PATH wins over GAME_WEEK; with neither set we return the
    permissive OPEN_PLAY scenario so local play is unchanged.
    """
    path = os.environ.get("SCENARIO_PATH")
    if path:
        try:
            return load_scenario(path)
        except (OSError, ValueError):
            return open_play()
    week = os.environ.get("GAME_WEEK")
    if week:
        try:
            return load_week(int(week))
        except (OSError, ValueError):
            return open_play()
    return open_play()


def available_weeks() -> list[int]:
    """Week numbers with a scenario file on disk, ascending."""
    try:
        names = os.listdir(SEASON_DIR)
    except OSError:
        return []
    weeks = []
    for n in names:
        if n.startswith("week") and n.endswith(".json"):
            try:
                weeks.append(int(n[4:-5]))
            except ValueError:
                continue
    return sorted(weeks)


# ---------------------------------------------------------------------------
# Simulator bridge
# ---------------------------------------------------------------------------

def scenario_shocks(scenario: Scenario, max_tick: int) -> list:
    """Resolve a scenario's price events and return them as ScheduledShocks.

    This PINS each event's DIRECTION into the scenario's own event dicts (as
    `direction`), so the exchange's calendar engine will later fire a move of
    the same sign. That is what lets the simulator hand a genuinely correct
    side to the "insider" shock predictor while the exchange remains the thing
    that actually applies the move. The exact magnitude is redrawn from the
    same `magnitude_range` at fire time, so even the insider only knows the
    side and the range — never the precise number.

    Magnitudes and directions are drawn from the global RNG, so a seeded
    season is reproducible.

    Dividends are not price shocks and are skipped — the exchange's calendar
    settles those in cash.
    """
    from sim.arena import ScheduledShock

    out = []
    for ev in scenario.events:
        kind = str(ev.get("kind", ""))
        if kind in ("dividend", "ipo"):
            continue      # cash / listing events, not price shocks
        tick = int(ev.get("tick", 0))
        if tick <= 0:
            continue
        lo, hi = (ev.get("magnitude_range") or [0.03, 0.06])[:2]
        pct = random.uniform(float(lo), float(hi))
        direction = ev.get("direction")
        if direction is None:
            up = random.choice((True, False))
            # Pin it so the exchange fires this exact direction.
            ev["direction"] = "up" if up else "down"
            if not up:
                pct = -pct
        elif str(direction).lower().startswith(("d", "-", "n")):
            pct = -pct
        if tick > max_tick:
            continue           # never reached in a run this short
        symbol = ev.get("symbol") if not ev.get("market_wide") else None
        out.append(ScheduledShock(
            tick=tick, pct=pct, symbol=symbol,
            label=f"{kind} {symbol or 'MKT'} {pct:+.0%} t{tick}",
        ))
    return sorted(out, key=lambda s: s.tick)
