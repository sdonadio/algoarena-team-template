"""
Synthetic teaching securities for AlgoArena — SYNTH.

Why this exists
---------------
The ten defaults in plugins/securities/defaults.py are driftless GBM walks
whose per-tick innovation is ~0.02-0.03%. That is realistic, and it is also
useless for a demo: over a 500-tick run a momentum crossover or a 1.5-sigma
mean-reversion fade may never fire, so a student's first `make sim` shows a
strategy that "does nothing" and they debug their code instead of reading it.

SYNTH is the teaching instrument that fixes that: a slow, clean sine wave
around $100 with a small upward drift. It trends hard enough for the
5/20 moving-average crossover to cross, and it mean-reverts by construction,
so `make sim` produces fills, a P&L curve and an ASCII chart every time.

    price(t) = BASE x (1 + DRIFT_PER_CYCLE x t/PERIOD)  +  AMPLITUDE x sin(2 pi t/PERIOD)

    BASE = 100.00,  AMPLITUDE = 10.00,  PERIOD = 3600 ticks,  DRIFT = +1%/cycle

One tick is one second, so a full cycle is one hour of trading: prices swing
$100 -> $110 -> $90 -> $101 over 3,600 ticks.

Determinism
-----------
There is no randomness at all — no `random`, no seed, no per-process state.
`price_fn(prev, tick, params)` is a pure function of its arguments (item 5 of
labs/ROUGH_EDGES.md: the defaults ignore `seed=`, so nothing here may depend
on the global RNG either). Two venues, two processes, two fresh registries and
two replays all produce byte-identical paths.

The step is a RATIO, not an absolute level:

    next = prev x target(tick) / target(tick - 1)

which keeps two useful properties at once. Starting from `base_price` the path
IS `target()` exactly, and a shock (or a dividend, or an IPO relevel) that
moves `prev_price` permanently relevels the whole path while the shape stays
identical — the same contract `fundamental_step()` in defaults.py offers.

Registration
------------
Importing this module registers SYNTH on the global `arena` registry. It is
deliberately NOT imported by exchange/server.py: SYNTH is not a real company
and does not belong in a live competition's tape. The offline simulator
(sim/session.py, `make sim`) imports it, which is where the demo lives.

Plugin contract (CLAUDE.md): a security is a price function
(prev_price, tick, params) -> float with no side effects.
"""

from __future__ import annotations

import math

from plugins import arena

# SYNTH's shape. Tuned so the 5/20 MA crossover in
# plugins/strategies/examples.py fires inside the first few hundred ticks.
SYNTH_SYMBOL = "SYNTH"
SYNTH_BASE_PRICE = 100.00
SYNTH_AMPLITUDE = 10.00        # dollars, peak-to-mean
SYNTH_PERIOD = 3600            # ticks per full cycle (one hour of seconds)
SYNTH_DRIFT_PER_CYCLE = 0.01   # +1% of base per completed cycle

# Reported so the risk/vol displays have something sane to show. A +/-10%
# swing every 3,600 seconds is roughly this in annualised terms; SYNTH's
# actual path has no stochastic component, so this is a label, not a sigma.
SYNTH_VOL = 0.5

_PRICE_FLOOR = 0.01


def sine_level(
    tick: int,
    base_price: float = SYNTH_BASE_PRICE,
    amplitude: float = SYNTH_AMPLITUDE,
    period: int = SYNTH_PERIOD,
    drift_per_cycle: float = SYNTH_DRIFT_PER_CYCLE,
) -> float:
    """Target price level at `tick` — the closed-form sine-plus-drift curve.

    Pure and total: no state, no randomness, defined for every integer tick,
    and floored at one cent so a caller can always divide by it.

    Args:
        tick:            Tick index within the session (tick 0 == `base_price`).
        base_price:      Mean level the sine oscillates around at tick 0.
        amplitude:       Peak deviation from the mean, in dollars.
        period:          Ticks per full cycle.
        drift_per_cycle: Fraction of `base_price` added per completed cycle.
    """
    trend = base_price * (1.0 + drift_per_cycle * tick / period)
    wave = amplitude * math.sin(2.0 * math.pi * tick / period)
    return max(trend + wave, _PRICE_FLOOR)


def make_sine(
    base_price: float = SYNTH_BASE_PRICE,
    amplitude: float = SYNTH_AMPLITUDE,
    period: int = SYNTH_PERIOD,
    drift_per_cycle: float = SYNTH_DRIFT_PER_CYCLE,
):
    """Return a deterministic sine-plus-drift price function.

    The returned callable matches the security plugin signature
    `(prev_price, tick, params) -> float` and holds no mutable state, so the
    same closure can price any number of independent runs.
    """

    def price_fn(prev_price: float, tick: int, params: dict) -> float:
        prev_level = sine_level(tick - 1, base_price, amplitude, period,
                                drift_per_cycle)
        this_level = sine_level(tick, base_price, amplitude, period,
                                drift_per_cycle)
        return max(prev_price * (this_level / prev_level), _PRICE_FLOOR)

    return price_fn


def register_synthetic(registry=arena) -> None:
    """Register SYNTH on `registry` (the global `arena` by default).

    Idempotent: registering twice just overwrites the entry with an identical
    one. Takes the registry as an argument so a test can build a fresh
    ArenaRegistry and prove the path does not depend on process state.
    """
    registry.register_security(
        id=SYNTH_SYMBOL, name="Synthetic Sine Wave", asset_type="synthetic",
        base_price=SYNTH_BASE_PRICE, color="#e879f9",
        price_fn=make_sine(), vol=SYNTH_VOL,
    )


register_synthetic()
