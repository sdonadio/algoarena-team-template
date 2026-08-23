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
from shared.messages import IPOSubscribe, Signal, TradeExecution
from trader.trader import MarketData, Portfolio, Strategy, TraderBot

logger = logging.getLogger(__name__)


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
        """Market news: SHOCK, CALENDAR, CALENDAR_EVENT, DIVIDEND, IPO_*, …

        `data` carries the specifics (e.g. CALENDAR → data["events"] lists
        upcoming events with tick and magnitude but never direction).
        Default: nothing — but the best Level 5+ traders live here.
        """

    def on_ipo(self, symbol: str, lo: float, hi: float, shares: int,
               data: dict) -> int | tuple[int, float] | None:
        """An IPO book just opened. Return your indication, or None to pass.

        Return a quantity (bids the TOP of the range — the sure allocation)
        or (quantity, max_price) to bid tighter and risk missing the deal.
        Cash is only debited if you are allocated at pricing. The listing
        may pop above the offer — or break below it. Choose.
        """
        return None

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
        if msg.event == "IPO_OPEN":
            d = msg.data or {}
            lo, hi = (d.get("offer_range") or [0, 0])[:2]
            try:
                wish = self._owner.on_ipo(d.get("symbol", ""), float(lo),
                                          float(hi), int(d.get("shares", 0)),
                                          d)
            except Exception:
                logger.exception("on_ipo raised — passing on the deal")
                wish = None
            if wish:
                qty, px = (wish if isinstance(wish, tuple) else (wish, hi))
                if int(qty) > 0:
                    await self._send(IPOSubscribe(
                        team_id=_config.TEAM_ID, symbol=d.get("symbol", ""),
                        quantity=int(qty), max_price=float(px)))
        if msg.event not in ("SESSION_OPEN", "SESSION_CLOSED"):
            self._owner.on_event(msg.event, msg.message, msg.data or {})
