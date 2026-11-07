"""
exchange/leadlag.py — hidden lead-lag links between securities (week 8, "big data").

A week file may declare

    "lead_lag": {"pairs": 3, "lag_range": [3, 8], "beta_range": [0.5, 0.8],
                 "universe": [...optional...], "seed": optional}

and the engine then links `pairs` (leader → follower) couples of stocks: every
tick, the follower's FUNDAMENTAL picks up `beta` times the leader's
fundamental return from `lag` ticks earlier. Which stocks, which sign and
which lag are NOT in the week file: they are drawn from a seed — the spec's
"seed", else the LEADLAG_SEED environment variable (set on the hosted venue
and used by the teacher's dataset generator), else a random one (local play:
students practise the mechanics on links that are not the graded ones).

Students find the links in a large offline dataset generated with the same
seed (scripts/make_leadlag_dataset.py) and trade them live.

The link is applied to each venue's MARK (exchange/server.py advance_tick):
the follower's fair value is multiplied by (1 + beta * leader mark return
`lag` ticks ago) before the venue marks it. The house market maker quotes
around the mark and every snapshot carries it as ref_price, so the link is
visible in the book at exactly the planted lag. Every venue with the same
seed carries the same links (each on its own marks).
"""

from __future__ import annotations

import logging
import os
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_UNIVERSE = ("AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META",
                    "NFLX", "AMD", "INTC")


@dataclass(frozen=True)
class Pair:
    leader: str
    follower: str
    lag: int
    beta: float

    def as_dict(self) -> dict[str, Any]:
        return {"leader": self.leader, "follower": self.follower,
                "lag": self.lag, "beta": round(self.beta, 4)}


def _seed(spec: dict[str, Any]) -> int | None:
    raw = spec.get("seed")
    if raw in (None, ""):
        raw = os.environ.get("LEADLAG_SEED", "")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return sum(ord(c) * 31 ** i for i, c in enumerate(str(raw))) % (2 ** 32)


def draw_pairs(spec: dict[str, Any], seed: int | None = None) -> list[Pair]:
    """The links a spec + seed produce. Pure: same inputs, same pairs."""
    universe = [str(s) for s in (spec.get("universe") or DEFAULT_UNIVERSE)]
    try:
        n = max(0, int(spec.get("pairs", 3)))
        lo_lag, hi_lag = sorted(int(x) for x in (spec.get("lag_range") or (3, 8))[:2])
        lo_b, hi_b = sorted(float(x) for x in (spec.get("beta_range") or (0.5, 0.8))[:2])
    except (TypeError, ValueError):
        logger.warning("lead_lag: unusable spec %r", spec)
        return []
    lo_lag = max(1, lo_lag)
    rng = random.Random(seed if seed is not None else random.getrandbits(32))
    names = sorted(universe)
    rng.shuffle(names)
    # Leaders and followers are disjoint, so links never chain.
    n = min(n, len(names) // 2)
    leaders, followers = names[:n], names[n:2 * n]
    out = []
    for lead, fol in zip(leaders, followers):
        lag = rng.randint(lo_lag, hi_lag)
        beta = rng.uniform(lo_b, hi_b) * rng.choice((-1.0, 1.0))
        out.append(Pair(lead, fol, lag, beta))
    return out


@dataclass
class LeadLag:
    """Applies a set of links to ONE venue's marks.

    The venue mark (ref_price) is what the house market maker quotes around
    and what every snapshot carries, so a link on the mark is visible in the
    book at exactly the planted lag. Each venue process keeps its own
    instance: leader returns are that venue's own mark returns.
    """
    pairs: list[Pair]
    _last: dict[str, float] = field(default_factory=dict)
    _rets: dict[str, deque] = field(default_factory=dict)
    _tick: int | None = None

    def __post_init__(self) -> None:
        depth = max((p.lag for p in self.pairs), default=1) + 1
        for p in self.pairs:
            self._rets.setdefault(p.leader, deque(maxlen=depth))
        self._by_follower = {p.follower: p for p in self.pairs}

    def factor(self, follower: str) -> float:
        """Multiplier for `follower`'s fair value this tick (1.0 = no link)."""
        p = self._by_follower.get(follower)
        if p is None:
            return 1.0
        hist = self._rets.get(p.leader)
        if not hist or len(hist) < p.lag:
            return 1.0
        return 1.0 + p.beta * hist[-p.lag]     # the leader's move `lag` ticks ago

    def record(self, tick: int, marks: dict[str, float]) -> None:
        """Store this tick's leader mark returns (once per tick)."""
        if tick == self._tick:
            return
        self._tick = tick
        for lead, hist in self._rets.items():
            px = marks.get(lead)
            prev = self._last.get(lead)
            hist.append((px / prev - 1.0) if (px and prev) else 0.0)
            if px:
                self._last[lead] = px


def from_scenario(scen: Any) -> LeadLag | None:
    """The week's links, or None when the week declares none."""
    spec = getattr(scen, "lead_lag", None)
    if not spec:
        return None
    pairs = draw_pairs(spec, _seed(spec))
    if not pairs:
        return None
    logger.info("lead_lag: %d hidden link(s) active", len(pairs))
    return LeadLag(pairs)
