"""
Default built-in securities for AlgoArena — 10 US equities.

The fundamental (news) process
------------------------------
A company's value does not depend on which exchange you look at, so neither
does its fundamental price. Every venue therefore computes the SAME
fundamental path with no networking at all: each tick's innovation is drawn
from a PRNG seeded by `(symbol, tick, FUNDAMENTAL_SEED)`, so

    exchange A's tick 900 innovation for NVDA
      == exchange B's tick 900 innovation for NVDA

by construction. Two venues that started from the same base price and ran the
same number of ticks hold the identical fundamental, which is what makes the
15% fundamental anchor in exchange/server.py a CONVERGING force rather than a
diverging one. (Before this, each process ran its own `random.gauss` walk and
venues drifted apart by construction — see docs/REALISM_REVIEW.md item 2.1.)

The walk itself is an ordinary driftless GBM step:

    S(t+1) = S(t) × exp((μ − ½σ²)·dt + σ·√dt·Z)     Z ~ N(0,1)

with dt = 1/(252 × 6.5 × 3600) — one tick is one second of trading — so the
per-tick innovation for a 50-100% annualized-vol name is ~0.02-0.03%, the
order of magnitude a real large cap moves in half a second. Intraday movement
beyond that is supposed to come from ORDER FLOW (the trade-impact engine in
exchange/price_engine.py), not from injected randomness.

`prev_price` — not the tick index alone — is the base of every step, so a
shock, a calendar ramp or a dividend permanently relevels the path while the
innovations stay identical across venues.

Writing your own security plugin
--------------------------------
Keep the `(prev_price, tick, params) -> float` signature and draw any
randomness from `random.Random(f"{symbol}:{tick}:{seed}")` — never from the
global `random` module. A plugin that calls `random.gauss` directly still
works, but each venue will walk its own path and the venues will drift apart.
`fundamental_step()` below does this for you.

Environment
-----------
    FUNDAMENTAL_SEED   session seed for the shared path (rotate per session;
                       EVERY venue in one session must use the same value)
    FUNDAMENTAL_VOL    multiplier on every security's annualized vol
                       (1.0 = as registered; 0 = a flat fundamental)
"""

from __future__ import annotations

import math
import os
import random

from plugins import arena

# One tick = one second of trading, expressed as a fraction of a trading year.
_DT = 1.0 / (252 * 6.5 * 3600)
_SQRT_DT = math.sqrt(_DT)

# Read at call time (not captured in the closures below) so a test or a
# teacher tool can rotate the seed or flatten the vol without re-importing.
FUNDAMENTAL_SEED = os.environ.get("FUNDAMENTAL_SEED", "algoarena-fundamental-1")
FUNDAMENTAL_VOL = float(os.environ.get("FUNDAMENTAL_VOL", "1.0"))


def fundamental_innovation(symbol: str, tick: int, sigma: float,
                           mu: float = 0.0) -> float:
    """One tick's log-return of the shared fundamental path.

    Deterministic in `(symbol, tick, FUNDAMENTAL_SEED)`: every process that
    asks for the same three values gets the same number, which is how N
    exchanges agree on the news without talking to each other.

    Args:
        symbol: Security id — different symbols get uncorrelated draws.
        tick:   Tick index within the session.
        sigma:  Annualized volatility as a decimal (0.7 = 70% per year),
                scaled by FUNDAMENTAL_VOL.
        mu:     Annualized drift as a decimal (0.1 = 10% per year).
    """
    vol = max(0.0, sigma * FUNDAMENTAL_VOL)
    # A string seed hashes to the same state in every CPython process
    # (PYTHONHASHSEED does not apply to Random's seeding), so this is stable
    # across machines and restarts — unlike hash().
    z = random.Random(f"{symbol}:{tick}:{FUNDAMENTAL_SEED}").gauss(0.0, 1.0)
    return (mu - 0.5 * vol ** 2) * _DT + vol * _SQRT_DT * z


def fundamental_step(symbol: str, prev_price: float, tick: int,
                     sigma: float, mu: float = 0.0) -> float:
    """Advance one security's shared fundamental path by one tick."""
    return prev_price * math.exp(fundamental_innovation(symbol, tick, sigma, mu))


def make_fundamental(symbol: str, sigma: float, mu: float = 0.0):
    """Return a deterministic, venue-coherent price function for `symbol`.

    Args:
        symbol: Security id, used to seed the shared path.
        sigma:  Annualized volatility as a decimal (e.g. 0.7 = 70% per year).
        mu:     Annualized drift as a decimal     (e.g. 0.1 = 10% per year).
    """

    def price_fn(prev_price: float, tick: int, params: dict) -> float:
        return fundamental_step(symbol, prev_price, tick, sigma, mu)

    return price_fn


def make_gbm(sigma: float, mu: float = 0.0):
    """Return an INDEPENDENT GBM price function (legacy).

    Every process that runs this walks its own path, so two venues listing a
    security priced this way will drift apart. Prefer `make_fundamental(id,
    sigma)` for anything that trades on more than one exchange; this is kept
    for single-venue experiments and for plugins written against the old API.

    Args:
        sigma: Annualized volatility as a decimal (e.g. 0.7 = 70% per year).
        mu:    Annualized drift as a decimal    (e.g. 0.1 = 10% per year).
    """
    drift     = (mu - 0.5 * sigma ** 2) * _DT
    diffusion = sigma * _SQRT_DT

    def price_fn(prev_price: float, tick: int, params: dict) -> float:
        Z = random.gauss(0, 1)
        return prev_price * math.exp(drift + diffusion * Z)

    return price_fn


# ------------------------------------------------------------------
# 10 US equities — broker seeds all of these
# ------------------------------------------------------------------

arena.register_security(
    id="AAPL", name="Apple Inc.", asset_type="equity",
    base_price=220.00, color="#60a5fa",
    price_fn=make_fundamental("AAPL", sigma=0.7), vol=0.7,
)

arena.register_security(
    id="MSFT", name="Microsoft Corp.", asset_type="equity",
    base_price=415.00, color="#818cf8",
    price_fn=make_fundamental("MSFT", sigma=0.5), vol=0.5,
)

arena.register_security(
    id="NVDA", name="NVIDIA Corp.", asset_type="equity",
    base_price=130.00, color="#22c55e",
    price_fn=make_fundamental("NVDA", sigma=0.9, mu=0.1), vol=0.9,
)

arena.register_security(
    id="TSLA", name="Tesla Inc.", asset_type="equity",
    base_price=280.00, color="#f472b6",
    price_fn=make_fundamental("TSLA", sigma=1.2), vol=1.2,
)

arena.register_security(
    id="AMZN", name="Amazon.com Inc.", asset_type="equity",
    base_price=195.00, color="#fb923c",
    price_fn=make_fundamental("AMZN", sigma=0.6), vol=0.6,
)

arena.register_security(
    id="GOOGL", name="Alphabet Inc.", asset_type="equity",
    base_price=175.00, color="#34d399",
    price_fn=make_fundamental("GOOGL", sigma=0.55), vol=0.55,
)

arena.register_security(
    id="META", name="Meta Platforms Inc.", asset_type="equity",
    base_price=580.00, color="#38bdf8",
    price_fn=make_fundamental("META", sigma=0.65), vol=0.65,
)

arena.register_security(
    id="NFLX", name="Netflix Inc.", asset_type="equity",
    base_price=720.00, color="#fbbf24",
    price_fn=make_fundamental("NFLX", sigma=0.8), vol=0.8,
)

arena.register_security(
    id="AMD", name="Advanced Micro Devices", asset_type="equity",
    base_price=165.00, color="#e879f9",
    price_fn=make_fundamental("AMD", sigma=1.0), vol=1.0,
)

arena.register_security(
    id="INTC", name="Intel Corp.", asset_type="equity",
    base_price=22.00, color="#94a3b8",
    price_fn=make_fundamental("INTC", sigma=0.75), vol=0.75,
)


# ------------------------------------------------------------------
# Base-price overrides — data/base_prices.json
# ------------------------------------------------------------------
# The static base prices above go stale against the real market, and a stale
# base makes every venue spend its opening minutes on a slow, clamped
# migration toward the Yahoo anchor — at venue-specific speeds, which opens
# cross-venue gaps (measured: INTC base $22 vs Yahoo $92 → an 11% gap between
# two freshly started venues). `make sync-prices` snapshots the real reference
# into data/base_prices.json; every venue that finds the file opens AT that
# level, like an IPO reference price. No file → the defaults above apply.

def _apply_base_price_overrides() -> None:
    import json
    import logging
    import os
    import pathlib

    path = os.environ.get(
        "BASE_PRICES_PATH",
        str(pathlib.Path(__file__).resolve().parents[2] / "data" / "base_prices.json"),
    )
    try:
        with open(path) as f:
            overrides = json.load(f)
    except (OSError, ValueError):
        return
    applied = 0
    for sym, px in overrides.items():
        entry = arena.securities.get(sym)
        if not entry or not isinstance(px, (int, float)) or px <= 0:
            continue
        entry["defn"].base_price = float(px)
        entry["current_price"] = float(px)
        arena.prices[sym] = float(px)
        applied += 1
    if applied:
        logging.getLogger(__name__).info(
            "Base prices: %d synced from %s", applied, path)


_apply_base_price_overrides()
