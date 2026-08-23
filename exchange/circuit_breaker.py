"""
exchange/circuit_breaker.py — Three-layer circuit breaker mirroring SEC LULD rules.

Layer 1 — Velocity halt:   symbol moved > VELOCITY_HALT_PCT in 60 seconds
Layer 2 — Session halt:    symbol moved > SESSION_HALT_PCT from session open
Layer 3 — Market-wide:     average session return crosses -7% / -13% / -20%

After 3 halts on the same symbol the per-symbol thresholds double to prevent
infinite halt loops (same logic as NYSE repeated-halt widening rules).
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import TYPE_CHECKING

import exchange.config as config

if TYPE_CHECKING:
    from exchange.price_engine import PriceEngine


class CircuitBreaker:
    """Monitors PriceEngines and enforces trading halts per SEC LULD rules."""

    def __init__(self, symbols: list[str]) -> None:
        self.halted:        set[str]          = set()
        self.halt_count:    dict[str, int]    = defaultdict(int)
        self.halt_times:    dict[str, float]  = {}   # symbol → UTC resume timestamp
        self.halt_history:  list[dict]        = []
        self.market_halted: bool              = False
        self._market_resume_time: float       = 0.0

        self.VELOCITY_WINDOW_SEC  = config.VELOCITY_WINDOW_SEC
        self.VELOCITY_HALT_PCT    = config.VELOCITY_HALT_PCT
        self.SESSION_HALT_PCT     = config.SESSION_HALT_PCT
        self.MARKET_WIDE_L1_PCT   = config.MARKET_WIDE_L1_PCT
        self.MARKET_WIDE_L2_PCT   = config.MARKET_WIDE_L2_PCT
        self.HALT_DURATION_SEC    = config.HALT_DURATION_SEC
        self.REPEATED_HALT_FACTOR = 2.0

    # ------------------------------------------------------------------
    # Per-symbol checks
    # ------------------------------------------------------------------

    def _effective_thresholds(self, symbol: str) -> tuple[float, float]:
        """Return (velocity_pct, session_pct) widened for repeat halts."""
        factor = self.REPEATED_HALT_FACTOR if self.halt_count[symbol] >= 3 else 1.0
        return (
            self.VELOCITY_HALT_PCT * factor,
            self.SESSION_HALT_PCT  * factor,
        )

    def check_symbol(
        self, symbol: str, engine: "PriceEngine"
    ) -> tuple[bool, str]:
        """Check whether a symbol should be halted. Returns (should_halt, reason).

        Three checks in order:
          1. Already halted — skip (avoid double-halt).
          2. Velocity: price moved > VELOCITY_HALT_PCT in the last
             VELOCITY_WINDOW_SEC seconds.
          3. Session band: |session_return| > SESSION_HALT_PCT.

        After 3 halts on the same symbol the thresholds widen by
        REPEATED_HALT_FACTOR to prevent infinite halt loops.
        """
        if symbol in self.halted or self.market_halted:
            return False, ""

        vel_pct, sess_pct = self._effective_thresholds(symbol)

        # ── Velocity check ────────────────────────────────────────────
        history = list(engine.price_history)
        if len(history) >= self.VELOCITY_WINDOW_SEC:
            old_price = history[-self.VELOCITY_WINDOW_SEC]
            if old_price > 0:
                velocity = abs(engine.market_price - old_price) / old_price
                if velocity >= vel_pct:
                    return True, (
                        f"{symbol} velocity halt: {velocity * 100:.1f}% move "
                        f"in {self.VELOCITY_WINDOW_SEC}s "
                        f"(threshold {vel_pct * 100:.1f}%)"
                    )

        # ── Session band check ────────────────────────────────────────
        ret = abs(engine.session_return())
        if ret >= sess_pct:
            return True, (
                f"{symbol} session-band halt: {ret * 100:.1f}% from open "
                f"(threshold {sess_pct * 100:.1f}%)"
            )

        return False, ""

    # ------------------------------------------------------------------
    # Market-wide check
    # ------------------------------------------------------------------

    def check_market_wide(
        self, engines: dict[str, "PriceEngine"]
    ) -> tuple[bool, str, int]:
        """Check for a market-wide halt. Returns (should_halt, reason, level).

        Computes average session_return across all symbols:
          Level 1: avg < MARKET_WIDE_L1_PCT (-7%)  → 15-minute halt
          Level 2: avg < MARKET_WIDE_L2_PCT (-13%) → 60-minute halt
          Level 3: avg < -20%                       → close session
        """
        if self.market_halted or not engines:
            return False, "", 0

        avg_ret = sum(e.session_return() for e in engines.values()) / len(engines)

        if avg_ret < -0.20:
            return (
                True,
                f"Market-wide L3 halt: avg session return {avg_ret * 100:.1f}%",
                3,
            )
        if avg_ret < self.MARKET_WIDE_L2_PCT:
            return (
                True,
                f"Market-wide L2 halt: avg session return {avg_ret * 100:.1f}%",
                2,
            )
        if avg_ret < self.MARKET_WIDE_L1_PCT:
            return (
                True,
                f"Market-wide L1 halt: avg session return {avg_ret * 100:.1f}%",
                1,
            )
        return False, "", 0

    # ------------------------------------------------------------------
    # Halt / resume
    # ------------------------------------------------------------------

    def halt_symbol(
        self, symbol: str, reason: str, duration: float | None = None
    ) -> dict:
        """Record a symbol halt and return the halt record dict.

        Does NOT send WebSocket messages — that is the exchange's responsibility.
        """
        dur       = duration if duration is not None else self.HALT_DURATION_SEC
        resume_at = time.time() + dur
        self.halted.add(symbol)
        self.halt_count[symbol] += 1
        self.halt_times[symbol]  = resume_at

        record = {
            "symbol":    symbol,
            "reason":    reason,
            "halted_at": time.time(),
            "resume_at": resume_at,
            "duration":  dur,
            "halt_num":  self.halt_count[symbol],
        }
        self.halt_history.append(record)
        return record

    def resume_symbol(self, symbol: str) -> bool:
        """Mark a symbol as no longer halted. Returns True if it was halted."""
        if symbol not in self.halted:
            return False
        self.halted.discard(symbol)
        self.halt_times.pop(symbol, None)
        return True

    def is_halted(self, symbol: str) -> bool:
        """True if this specific symbol OR the entire market is halted."""
        return self.market_halted or symbol in self.halted

    def get_resume_time(self, symbol: str) -> float:
        """Seconds remaining until symbol can resume (0 if not halted)."""
        if symbol not in self.halt_times:
            return 0.0
        return max(0.0, self.halt_times[symbol] - time.time())

    def get_halt_summary(self) -> list[dict]:
        """Return all halt records for this session."""
        return list(self.halt_history)
