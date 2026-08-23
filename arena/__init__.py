"""
arena — the student SDK. This is the ONLY package you need to import.

Three base classes, one per role. Derive the one(s) your team bought and
implement the hook methods; every hook has a sensible default, so an empty
subclass already runs. All the plumbing — connections, authentication,
reconnects, message parsing, order management, settlement — is handled for
you and is NOT your problem.

    from arena import Trader, Broker, Exchange, Signal

    class MyTrader(Trader):
        def on_tick(self, market, portfolio):
            ...                     # your edge
            return Signal(symbol="AAPL", side="buy", quantity=5, price=...)

    if __name__ == "__main__":
        MyTrader().run()

What each role overrides:

    Trader    on_tick()        decide a trade (or None) each tick
              on_fill()        react to your own executions
              on_event()       shocks, calendar events, dividends

    Broker    spread()         how wide you quote
              skew()           shift quotes to manage inventory
              toxic()          refuse to quote a counterparty
              on_fill()        react to your quotes being hit

    Exchange  taker_bps /      your published fee schedule
              rebate_bps
              accept_order()   veto orders before they reach the book
              on_trade()       observe every trade on your venue

The engine underneath (shared/, exchange/, broker/, trader/) is readable —
studying it is encouraged — but you never need to edit it.
"""

from shared.messages import Signal

from arena.broker import Broker
from arena.exchange import Exchange
from arena.trader import Trader

__all__ = ["Trader", "Broker", "Exchange", "Signal"]
