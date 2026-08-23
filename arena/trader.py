"""
arena.trader — derive Trader, implement on_tick(). That's the whole job.

The plumbing underneath (trader/trader.py) connects, authenticates,
tracks your portfolio, and calls your hooks at the right moments.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

import trader.config as _config
from shared.messages import Signal, TradeExecution
from trader.trader import MarketData, Portfolio, Strategy, TraderBot


class Trader(ABC):
    """Your algorithmic trader. Subclass and implement on_tick().

    Example — buy anything falling 1% below its 20-tick average:

        class MyTrader(Trader):
            def on_tick(self, market, portfolio):
                for sym in market.symbols():
                    hist = market.prices(sym)
                    if len(hist) < 20:
                        continue
                    avg = sum(hist[-20:]) / 20
                    mid = market.mid_price(sym)
                    if mid < avg * 0.99 and portfolio.can_buy(sym, 5, mid):
                        return Signal(symbol=sym, side="buy",
                                      quantity=5, price=market.best_ask(sym))
                return None

        if __name__ == "__main__":
            MyTrader().run()

    Run with:  TEAM_ID=<your_trader_id> python -m team.trader
    """

    # ── The hooks (override these) ──────────────────────────────────────

    @abstractmethod
    def on_tick(self, market: MarketData, portfolio: Portfolio) -> Signal | None:
        """Called every tick. Return a Signal to trade, or None to sit out.

        market     .symbols() .mid_price(s) .best_bid(s) .best_ask(s)
                   .spread(s) .prices(s) → recent mids, oldest first
        portfolio  .cash .positions .can_buy(s, qty, px) .can_sell(s, qty)
        """

    def on_fill(self, side: str, symbol: str, quantity: int,
                price: float) -> None:
        """Called when one of YOUR orders executes. Default: nothing."""

    def on_event(self, event: str, message: str, data: dict) -> None:
        """Market news: SHOCK, CALENDAR, CALENDAR_EVENT, DIVIDEND, …

        `data` carries the specifics (e.g. CALENDAR → data["events"] lists
        upcoming events with tick and magnitude but never direction).
        Default: nothing — but the best Level 5+ traders live here.
        """

    # ── Entry point (don't override) ────────────────────────────────────

    def run(self) -> None:
        """Connect to the exchange and trade until interrupted."""
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)-7s %(message)s",
                            datefmt="%H:%M:%S")
        asyncio.run(_AdaptedTraderBot(self).run())


# ── Adapters: bolt the hooks onto the standard plumbing ─────────────────

class _AdapterStrategy(Strategy):
    def __init__(self, owner: Trader) -> None:
        self._owner = owner

    def generate_signal(self, market, portfolio):
        return self._owner.on_tick(market, portfolio)


class _AdaptedTraderBot(TraderBot):
    def __init__(self, owner: Trader) -> None:
        super().__init__()
        self._owner = owner
        self.strategy = _AdapterStrategy(owner)

    def _on_trade(self, msg: TradeExecution) -> None:
        super()._on_trade(msg)
        if msg.buyer_id == _config.TEAM_ID:
            self._owner.on_fill("buy", msg.symbol, msg.quantity, msg.price)
        elif msg.seller_id == _config.TEAM_ID:
            self._owner.on_fill("sell", msg.symbol, msg.quantity, msg.price)

    async def _on_session_event(self, msg) -> None:
        await super()._on_session_event(msg)
        if msg.event not in ("SESSION_OPEN", "SESSION_CLOSED"):
            self._owner.on_event(msg.event, msg.message, msg.data or {})
