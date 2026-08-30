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
        Under the earned-leverage regime you also receive "MARGIN_CALL" (your
        equity has fallen below the call ratio — de-risk) and "LIQUIDATION"
        (you were force-flattened). Default: nothing — but the best Level 5+
        traders live here.
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


# ─────────────────────────────────────────────────────────────────────────────
# Offline bridge: run an arena Trader inside the simulator
# ─────────────────────────────────────────────────────────────────────────────

class _SimMarketView:
    """Duck-typed MarketData over one signal_fn call's arguments.

    Exposes the same read surface on_tick() is documented against —
    symbols() / mid_price() / best_bid() / best_ask() / spread() /
    prices() — built from the sim's (prices, history, book) for the symbol
    currently being asked about. History is only known for that symbol;
    prices(other) returns just the current level.
    """

    def __init__(self, symbol: str, ref_prices: dict, history: list, book) -> None:
        self._symbol = symbol
        self._ref = ref_prices
        self._history = history
        self._book = book

    def symbols(self) -> list[str]:
        return list(self._ref)

    def mid_price(self, symbol: str) -> float | None:
        return self._ref.get(symbol)

    def prices(self, symbol: str) -> list[float]:
        if symbol == self._symbol:
            return list(self._history)
        px = self._ref.get(symbol)
        return [px] if px is not None else []

    def _touch(self, side: str) -> float | None:
        book = self._book
        if book is None:
            return None
        fn = getattr(book, f"best_{side}", None)
        if callable(fn):
            try:
                out = fn()
                return getattr(out, "price", out)
            except Exception:
                return None
        return None

    def best_bid(self, symbol: str) -> float | None:
        return self._touch("bid") if symbol == self._symbol else None

    def best_ask(self, symbol: str) -> float | None:
        return self._touch("ask") if symbol == self._symbol else None

    def spread(self, symbol: str) -> float | None:
        bid, ask = self.best_bid(symbol), self.best_ask(symbol)
        return (ask - bid) if bid is not None and ask is not None else None


class _SimPortfolioView:
    """Duck-typed Portfolio over the sim's portfolio dict."""

    def __init__(self, portfolio: dict) -> None:
        self.cash = portfolio.get("cash", 0.0)
        self.positions = dict(portfolio.get("positions") or {})
        self.realized_pnl = portfolio.get("realized_pnl", 0.0)
        self.total_fees_paid = portfolio.get("total_fees_paid", 0.0)
        self._net_worth = portfolio.get("net_worth", self.cash)

    def net_worth(self, _prices: dict | None = None) -> float:
        return self._net_worth


def as_signal_fn(trader: "Trader"):
    """Wrap an arena Trader so the simulator can run it.

    The simulator (make sim / tests/sim_session.py, plugin registry) speaks
        signal_fn(symbol, prices, history, book, portfolio) -> Signal | None
    while a Trader speaks on_tick(market, portfolio). This returns the former
    from the latter, so one strategy runs live AND offline:

        from arena import as_signal_fn
        from team.trader import MyTrader
        SimulatedTrader(exchange, "me", as_signal_fn(MyTrader()))

    Caveat: the sim calls signal_fn once per symbol per tick; a Trader that
    counts on_tick() invocations to measure time should count distinct
    ticks (e.g. by len(market.prices(symbol))) instead.
    """

    def signal_fn(symbol, prices, history, book, portfolio):
        market = _SimMarketView(symbol, prices, history, book)
        return trader.on_tick(market, _SimPortfolioView(portfolio))

    return signal_fn
