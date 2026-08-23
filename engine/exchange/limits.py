"""
exchange/limits.py — message quotas (token buckets) and latency tiers.

Two pieces of real market microstructure that only show up once students
requote continuously:

* **Message quotas.** Real venues meter order and cancel traffic and charge
  for excess. A team gets `refill` messages per tick with a burst allowance of
  `capacity`; over quota the exchange answers `RATE_LIMITED` instead of
  filling. This is what forces quote-lifetime management rather than
  cancel/replace every tick.

* **Latency tiers.** Everyone sees the book simultaneously today, which hides
  the single most important structural asymmetry in modern markets. When the
  week enables it, the exchange delays each team's outbound messages by its
  tier — and colocation, bought in the shop, cuts that delay by 10x.

Both default to permissive so nothing changes until a week scenario turns
them on.
"""

from __future__ import annotations

from dataclasses import dataclass

import exchange.config as config


@dataclass
class TokenBucket:
    """Classic token bucket, refilled once per game tick.

    `capacity` is the burst allowance (a market maker legitimately fires one
    order per symbol per requote); `refill` is the sustained rate.
    """

    capacity: float
    refill: float
    tokens: float = 0.0

    def __post_init__(self) -> None:
        if not self.tokens:
            self.tokens = float(self.capacity)

    def tick(self) -> None:
        """Add one tick's worth of allowance, capped at the burst size."""
        self.tokens = min(float(self.capacity), self.tokens + float(self.refill))

    def take(self, n: float = 1.0) -> bool:
        """Consume `n` tokens. False (and no consumption) if short."""
        if self.tokens < n:
            return False
        self.tokens -= n
        return True

    @property
    def available(self) -> int:
        return int(self.tokens)


def bucket_for(quota_per_tick: float) -> TokenBucket:
    """Build a bucket from a per-tick quota, using the configured burst ratio."""
    refill = max(0.0, float(quota_per_tick))
    return TokenBucket(capacity=refill * config.ORDER_QUOTA_BURST_MULTIPLE,
                       refill=refill)


def latency_seconds(latency_ms: float) -> float:
    """Convert a tier in milliseconds to seconds, clamped to sane bounds."""
    return max(0.0, min(float(latency_ms), 5_000.0)) / 1000.0
