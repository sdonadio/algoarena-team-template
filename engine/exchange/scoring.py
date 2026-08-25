"""
exchange/scoring.py — season scoring from equity snapshots.

Season rank is risk-adjusted, not raw final net worth, and it is announced
up front (docs/SEASON_GUIDE.md). Ranking on net worth alone rewards one
lucky levered bet; this formula rewards a steady book.

Given a team's equity snapshots e[0..n] (oldest first, taken only in weeks
where scoring_counts is true):

    r[i]              = e[i] / e[i-1] - 1                  for i = 1..n
    cumulative_return = e[n] / e[0] - 1
    mean_return       = mean(r)
    vol               = population standard deviation of r
    peak[i]           = max(e[0..i])
    max_drawdown      = max over i of (peak[i] - e[i]) / peak[i]

    excess[i]         = r[i] - RISK_FREE_PER_SNAPSHOT      # return above idle cash
    risk_adjusted     = (mean(excess) / max(vol, VOL_FLOOR))
                        * max(0, 1 - PENALTY * max_drawdown)
                        clamped to +/- RISK_ADJUSTED_CAP

with PENALTY = SEASON_DRAWDOWN_PENALTY (default 2.0).

Why excess return and a vol floor (audit C1): idle cash earns
CASH_INTEREST_PER_TICK every tick, so a do-nothing all-cash book has a
tiny-but-nonzero return and a volatility of pure floating-point noise
(~1e-16). The old formula divided the drift by that noise and scored idle
cash ~1e9x a real trader — "standing still" was the dominant strategy, the
exact opposite of the intent. Scoring the return ABOVE the risk-free drift
makes idle cash score ~0; flooring the denominator stops a near-constant
book from exploding; the cap bounds any residual. A catastrophic drawdown
still cannot flip a negative score positive (the multiplier floors at 0).
"""

from __future__ import annotations

import os
import statistics
from typing import Any, Iterable, Sequence

# Weight on max drawdown. 2.0 means a 25% drawdown halves your score.
SEASON_DRAWDOWN_PENALTY = float(os.environ.get("SEASON_DRAWDOWN_PENALTY", "2.0"))

# Minimum snapshots before a score is meaningful.
MIN_SNAPSHOTS = 3

# Risk-free drift per equity snapshot: idle cash compounds at
# CASH_INTEREST_PER_TICK for SEASON_SNAPSHOT_TICKS ticks between snapshots.
# Returns are scored net of this, so parking cash is worth ~0, not a fortune.
_CASH_INTEREST_PER_TICK = float(os.environ.get("CASH_INTEREST_PER_TICK", "0.000002"))
_SNAPSHOT_TICKS = int(os.environ.get("SEASON_SNAPSHOT_TICKS", "60"))
RISK_FREE_PER_SNAPSHOT = (1.0 + _CASH_INTEREST_PER_TICK) ** _SNAPSHOT_TICKS - 1.0

# Denominator floor: one snapshot of the risk-free rate. Prevents a
# near-constant equity curve (vol ~ float noise) from producing an
# astronomical ratio.
VOL_FLOOR = float(os.environ.get("SCORE_VOL_FLOOR", str(max(RISK_FREE_PER_SNAPSHOT, 1e-4))))

# Hard bound on the risk-adjusted score, so no residual edge case explodes.
RISK_ADJUSTED_CAP = float(os.environ.get("SCORE_RISK_ADJUSTED_CAP", "10.0"))


def returns(equity: Sequence[float]) -> list[float]:
    """Simple period returns between consecutive snapshots."""
    out = []
    for prev, cur in zip(equity, equity[1:]):
        if prev:
            out.append(cur / prev - 1.0)
    return out


def max_drawdown(equity: Sequence[float]) -> float:
    """Largest peak-to-trough fraction over the equity path (0.0 – 1.0)."""
    peak = None
    worst = 0.0
    for value in equity:
        if peak is None or value > peak:
            peak = value
        if peak and peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def score_equity(equity: Sequence[float],
                 penalty: float | None = None) -> dict[str, float]:
    """Season metrics for one equity path. See the module docstring."""
    pen = SEASON_DRAWDOWN_PENALTY if penalty is None else penalty
    eq = [float(v) for v in equity]
    n = len(eq)
    if n < 2 or not eq[0]:
        return {
            "snapshots": n, "cumulative_return": 0.0, "mean_return": 0.0,
            "vol": 0.0, "max_drawdown": 0.0, "risk_adjusted": 0.0,
        }

    r = returns(eq)
    vol = statistics.pstdev(r) if len(r) > 1 else 0.0
    mean_r = (sum(r) / len(r)) if r else 0.0
    mdd = max_drawdown(eq)

    # Score the return ABOVE the risk-free drift, over a floored denominator,
    # then clamp. This is what stops "park cash and do nothing" from winning
    # the season (audit C1).
    if n >= MIN_SNAPSHOTS and r:
        excess = [x - RISK_FREE_PER_SNAPSHOT for x in r]
        mean_excess = sum(excess) / len(excess)
        ratio = mean_excess / max(vol, VOL_FLOOR)
        ratio = max(-RISK_ADJUSTED_CAP, min(RISK_ADJUSTED_CAP, ratio))
        risk_adjusted = ratio * max(0.0, 1.0 - pen * mdd)
    else:
        risk_adjusted = 0.0

    return {
        "snapshots": n,
        "cumulative_return": eq[-1] / eq[0] - 1.0,
        "mean_return": mean_r,
        "vol": vol,
        "max_drawdown": mdd,
        "risk_adjusted": risk_adjusted,
    }


def downsample(points: Sequence[Any], limit: int = 120) -> list[Any]:
    """Evenly thin a series to at most `limit` points, keeping the last one.

    The dashboard draws curves a few hundred pixels wide; sending a
    multi-thousand-point path per team per leaderboard is pure waste.
    """
    n = len(points)
    if n <= limit or limit < 2:
        return list(points)
    step = n / float(limit)
    out = [points[min(n - 1, int(i * step))] for i in range(limit)]
    if out[-1] != points[-1]:
        out[-1] = points[-1]
    return out


def build_season_block(
    equity_history: dict[str, list[Any]],
    portfolios: dict[str, Any],
    scenario: Any,
    curve_points: int = 120,
) -> dict[str, Any]:
    """Assemble the optional `season` section of a Leaderboard message.

    equity_history: team_id → [(tick, net_worth), ...]
    """
    standings = []
    curves: dict[str, list[list[float]]] = {}

    for team_id, hist in equity_history.items():
        if not hist:
            continue
        equity = [float(v) for _, v in hist]
        metrics = score_equity(equity)
        p = portfolios.get(team_id)
        standings.append({
            "team_id": team_id,
            "role": getattr(p, "role", "unknown"),
            "net_worth": round(equity[-1], 2),
            "cumulative_return": round(metrics["cumulative_return"], 6),
            "vol": round(metrics["vol"], 8),
            "max_drawdown": round(metrics["max_drawdown"], 6),
            "risk_adjusted": round(metrics["risk_adjusted"], 6),
            "snapshots": metrics["snapshots"],
            "liquidated": bool(getattr(p, "liquidated", False)),
        })
        curves[team_id] = [
            [int(t), round(float(v), 2)]
            for t, v in downsample(hist, curve_points)
        ]

    # The season is ranked PER ROLE: traders against traders, brokers against
    # brokers. Market making and directional trading have structurally
    # different return profiles (a liquidity provider earns the spread on
    # every fill; a trader earns only when a view is right), so a mixed table
    # is decided at seat-selection time, not at the keyboard — 30 simulated
    # seasons had a market maker winning every single one. Real markets never
    # benchmark a desk against a prop trader on raw return either.
    # `rank` is the within-role rank; `overall_rank` keeps the mixed view.
    standings.sort(key=lambda r: r["risk_adjusted"], reverse=True)
    for i, row in enumerate(standings, 1):
        row["overall_rank"] = i
    by_role: dict[str, int] = {}
    for row in standings:
        by_role[row["role"]] = by_role.get(row["role"], 0) + 1
        row["rank"] = by_role[row["role"]]

    return {
        "week": getattr(scenario, "week", 0),
        "label": getattr(scenario, "label", ""),
        "scoring_counts": bool(getattr(scenario, "scoring_counts", True)),
        "drawdown_penalty": SEASON_DRAWDOWN_PENALTY,
        "standings": standings,
        "curves": curves,
    }
