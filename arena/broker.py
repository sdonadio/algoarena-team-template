"""
arena.broker — derive Broker, override the three pricing decisions.

A market maker's whole job is three questions, and each is one method:
how wide do I quote (spread), where do I centre it (skew), and who do I
refuse to trade with (toxic). The plumbing underneath (broker/broker.py)
handles the reference price feed, quote placement, cancel/replace, and
multi-venue fan-out.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque

import broker.config as _config
from broker.broker import BrokerBot, BrokerState
from shared.messages import TradeExecution


class Broker:
    """Your market maker. Subclass and override any of the hooks —
    an empty subclass already quotes a fixed, symmetric market.

    Example — widen in volatile markets, skew against inventory:

        class MyBroker(Broker):
            def spread(self, symbol, price, history):
                if len(history) < 20:
                    return None                  # None → sensible default
                moves = [abs(b - a) for a, b in zip(history, history[1:])]
                vol = sum(moves[-20:]) / 20
                return max(0.05, vol * 4)

            def skew(self, symbol, inventory):
                return -inventory * 0.002        # long → quote lower

        if __name__ == "__main__":
            MyBroker().run()

    Run with:  TEAM_ID=<your_broker_id> python -m team.broker
    Multi-venue:  EXCHANGE_URLS=ws://host:8765,ws://host:8766 (one pricing
    brain, one execution gateway per venue — handled for you).
    """

    # ── The hooks (override these) ──────────────────────────────────────

    def spread(self, symbol: str, price: float | None,
               history: list[float]) -> float | None:
        """Dollar width of your quote for this symbol.

        price    the current reference price (None before the first tick)
        history  recent reference prices, oldest first (up to 100)

        Return None to use the default (a fixed width, capped in bps).
        Wider = safer but less flow; tighter = more fills, more risk.
        """
        return None

    def skew(self, symbol: str, inventory: int) -> float | None:
        """Dollar shift applied to BOTH your bid and ask.

        inventory  your net position in this symbol (negative = short)

        Negative skew lowers your quotes (attracts sellers → flattens a
        long); positive raises them. Return None (or 0) for symmetric
        quotes. This is the Level 4 survival skill.
        """
        return None

    def toxic(self, trader_id: str) -> bool:
        """Return True to stop quoting to a counterparty that keeps
        picking you off the moment the price moves. Default: trust all."""
        return False

    def on_fill(self, side: str, symbol: str, quantity: int,
                price: float) -> None:
        """Called when one of your quotes is hit or lifted. Default: nothing."""

    # ── Entry point (don't override) ────────────────────────────────────

    def run(self) -> None:
        """Quote every configured venue until interrupted."""
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)-7s %(message)s",
                            datefmt="%H:%M:%S")
        asyncio.run(self._run_venues())

    async def _run_venues(self) -> None:
        urls = [u.strip() for u in _config.EXCHANGE_URLS if u.strip()]
        if len(urls) <= 1:
            await _AdaptedBrokerBot(self, urls[0] if urls else None).run()
            return
        bots = [_AdaptedBrokerBot(self, url) for url in urls]
        # One shared external reference (there is only one Apple); per-venue
        # execution state stays local. Only the first instance polls Yahoo.
        for b in bots[1:]:
            b.state.yahoo_prices = bots[0].state.yahoo_prices
            b._own_yahoo_feed = False
        await asyncio.gather(*(b.run() for b in bots))


# ── Adapters: bolt the hooks onto the standard plumbing ─────────────────

class _AdaptedState(BrokerState):
    def __init__(self, owner: Broker) -> None:
        super().__init__()
        self._owner = owner

    def _price_and_history(self, symbol: str):
        price = self.exchange_prices.get(symbol) or self.yahoo_prices.get(symbol)
        return price, list(self.price_history.get(symbol, deque()))

    def compute_spread(self, symbol: str) -> float:
        price, hist = self._price_and_history(symbol)
        value = self._owner.spread(symbol, price, hist)
        return super().compute_spread(symbol) if value is None else float(value)

    def compute_skew(self, symbol: str) -> float:
        value = self._owner.skew(symbol, self.positions.get(symbol, 0))
        return 0.0 if value is None else float(value)

    def is_toxic(self, trader_id: str) -> bool:
        return bool(self._owner.toxic(trader_id))


class _AdaptedBrokerBot(BrokerBot):
    def __init__(self, owner: Broker, exchange_url: str | None = None) -> None:
        super().__init__(exchange_url)
        self._owner = owner
        self.state = _AdaptedState(owner)

    def _on_trade(self, msg: TradeExecution) -> None:
        super()._on_trade(msg)
        if msg.buyer_id == _config.TEAM_ID:
            self._owner.on_fill("buy", msg.symbol, msg.quantity, msg.price)
        elif msg.seller_id == _config.TEAM_ID:
            self._owner.on_fill("sell", msg.symbol, msg.quantity, msg.price)
