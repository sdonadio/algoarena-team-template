"""
exchange/price_engine.py — Supply-demand driven price discovery for one symbol.

Three forces act simultaneously:
  1. Fair value  — the blend of this venue's own book (a depth-weighted
     microprice, see `microprice` below) and the shared fundamental path;
     computed in ExchangeServer.advance_tick and pushed in here.
  2. Temporary impact — each trade pushes price in the aggressor's direction;
     70% decays back toward fair value over ~20-30 seconds (liquidity rebounds).
  3. Permanent impact — 30% shifts fair value itself (informational content).

(2) and (3) are what makes intraday movement ENDOGENOUS: imbalanced demand
eats through the book and moves the price, exactly as it does in a real
market. Nothing here injects randomness of its own.

Usage:
    engine = PriceEngine("AAPL", 220.00)
    engine.update_fair_value(new_yahoo_price)   # called by price tick loop
    engine.apply_trade_impact(trade)            # called after every fill
    engine.tick()                               # called every second
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import TYPE_CHECKING

import exchange.config as config

if TYPE_CHECKING:
    from shared.orderbook import Trade


def microprice(bids: list, asks: list) -> float | None:
    """Depth-weighted price at the touch, or None if the book is not two-sided.

        microprice = (bid × ask_size + ask × bid_size) / (bid_size + ask_size)

    This is the reference real microstructure uses, and it is the venue's
    supply/demand reading: the weight on each price is the size on the OTHER
    side, so a heavy bid (lots of buyers) pulls the reference UP toward the
    ask, and a heavy offer pulls it down. With equal sizes it is the plain mid.

    A crossed or locked book (bid >= ask) is rejected rather than averaged:
    the matching engine does not leave one behind, so seeing one means the
    snapshot is torn and the previous reference should stand.

    Args:
        bids: [[price, size], …] best first, as OrderBook.get_snapshot returns.
        asks: [[price, size], …] best first.
    """
    if not bids or not asks:
        return None
    bid, bid_size = float(bids[0][0]), float(bids[0][1])
    ask, ask_size = float(asks[0][0]), float(asks[0][1])
    if bid <= 0 or ask <= 0 or bid >= ask:
        return None
    total = bid_size + ask_size
    if total <= 0:
        return (bid + ask) / 2.0
    return (bid * ask_size + ask * bid_size) / total


class PriceEngine:
    """Manages price dynamics for one symbol via a two-component impact model."""

    def __init__(self, symbol: str, initial_price: float) -> None:
        self.symbol         = symbol
        self.fair_value     = initial_price
        self.market_price   = initial_price
        self.impact_buffer  = 0.0           # accumulated temporary impact
        self.session_open   = initial_price
        self.price_history: deque[float] = deque(maxlen=300)   # 5 min at 1/s
        self.trade_history: deque[dict]  = deque(maxlen=100)   # recent fills
        self.last_tick_time = time.time()

        # Tunable — sourced from config so teachers can tweak without code changes
        self.IMPACT_COEFFICIENT   = config.IMPACT_COEFFICIENT
        self.PERMANENT_FRACTION   = config.PERMANENT_FRACTION
        self.MEAN_REVERSION_SPEED = config.MEAN_REVERSION_SPEED
        self.IMPACT_MODEL         = config.IMPACT_MODEL

    # ------------------------------------------------------------------
    # Core dynamics
    # ------------------------------------------------------------------

    def apply_trade_impact(self, trade: "Trade") -> float:
        """Apply market impact after a completed fill and return new market_price.

        Impact model:
          linear: impact = IMPACT_COEFFICIENT × notional
          sqrt:   impact = IMPACT_COEFFICIENT × sqrt(notional)  ← default, more realistic

        The total impact is split:
          permanent (PERMANENT_FRACTION)  — shifts fair_value permanently
          temporary (1 - PERMANENT_FRACTION) — goes into impact_buffer, decays over time

        Direction: "buy" aggressor pushes price up; "sell" pushes down.
        """
        notional = trade.price * trade.quantity
        if self.IMPACT_MODEL == "sqrt":
            raw = self.IMPACT_COEFFICIENT * math.sqrt(notional)
        else:
            # TODO Level 4: Implement Kyle's Lambda (volume-weighted permanent impact)
            raw = self.IMPACT_COEFFICIENT * notional

        direction     = 1.0 if trade.aggressor == "buy" else -1.0
        total_impact  = direction * raw
        permanent     = total_impact * self.PERMANENT_FRACTION
        temporary     = total_impact * (1.0 - self.PERMANENT_FRACTION)

        self.fair_value    = max(0.01, self.fair_value + permanent)
        self.impact_buffer += temporary
        self.market_price  = max(0.01, self.fair_value + self.impact_buffer)

        self.trade_history.append({
            "price":     trade.price,
            "quantity":  trade.quantity,
            "aggressor": trade.aggressor,
            "notional":  notional,
            "impact":    total_impact,
        })
        return self.market_price

    def tick(self) -> float:
        """Advance one second: decay temporary impact, recompute market price.

        Temporary impact decays exponentially:
            impact_buffer *= (1 - MEAN_REVERSION_SPEED)
        Market price is then:
            market_price = fair_value + impact_buffer

        Records market_price into price_history for vol and circuit-breaker checks.
        Returns new market_price.
        """
        self.impact_buffer *= (1.0 - self.MEAN_REVERSION_SPEED)
        self.market_price   = max(0.01, self.fair_value + self.impact_buffer)
        self.price_history.append(self.market_price)
        self.last_tick_time = time.time()
        return self.market_price

    def update_fair_value(self, new_price: float) -> None:
        """Update fundamental fair value from the Yahoo feed or a shock event.

        Does NOT reset impact_buffer — temporary impact continues to decay
        independently.  Market price walks toward the new fair value as the
        buffer decays.
        """
        self.fair_value   = max(0.01, new_price)
        self.market_price = max(0.01, self.fair_value + self.impact_buffer)

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def rolling_volatility(self, window: int = 60) -> float:
        """Annualized volatility from recent price_history.

        Returns 0.0 when fewer than 2 data points are available.
        Formula: std(log_returns) * sqrt(252 * 6.5 * 3600)
        """
        prices = list(self.price_history)[-window:]
        if len(prices) < 2:
            return 0.0
        log_returns = [
            math.log(prices[i] / prices[i - 1])
            for i in range(1, len(prices))
            if prices[i - 1] > 0
        ]
        if not log_returns:
            return 0.0
        n    = len(log_returns)
        mean = sum(log_returns) / n
        var  = sum((r - mean) ** 2 for r in log_returns) / max(n - 1, 1)
        return math.sqrt(var) * math.sqrt(252 * 6.5 * 3600)

    def session_return(self) -> float:
        """Current market_price vs session open as a fraction. e.g. 0.03 = +3%"""
        if self.session_open <= 0:
            return 0.0
        return (self.market_price - self.session_open) / self.session_open

    def to_dict(self) -> dict:
        """Snapshot for broadcasting and logging."""
        return {
            "symbol":         self.symbol,
            "fair_value":     round(self.fair_value, 4),
            "market_price":   round(self.market_price, 4),
            "impact_buffer":  round(self.impact_buffer, 6),
            "session_return": round(self.session_return(), 6),
            "rolling_vol":    round(self.rolling_volatility(), 6),
        }
