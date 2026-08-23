"""
trader/flow_bots.py — background order flow: the market's extras.

Real markets are not just sharks trading against each other. Two kinds of
flow give the game its texture:

* **RetailFlow** — small, random, marketable orders: 1–3 shares, either
  side, crossing the spread. Individually meaningless, collectively the
  uninformed flow every market maker wants to capture. This is the flow
  brokers profit from — and the baseline against which toxic flow
  detection (Level 5) makes sense.

* **InstitutionalVWAP** — a large parent order worked in small child
  slices on a regular clock, always the same side of the same symbol
  until the parent is done. Individually innocuous, collectively a
  PATTERN: persistent one-sided pressure that observant traders can
  detect and trade ahead of (and TCA homework can measure the cost of).

Which personality a process gets is decided by its TEAM_ID: ids containing
"vwap" run the institutional slicer, everything else runs retail flow.

Run (ids come from the roster):
    TEAM_ID=flow_retail_1 python -m trader.flow_bots
    TEAM_ID=flow_vwap_1   python -m trader.flow_bots
"""

from __future__ import annotations

import os
import random

from arena import Signal, Trader


class RetailFlow(Trader):
    """Small random marketable orders — the uninformed flow."""

    TRADE_PROB = 0.25          # fire on ~1 tick in 4
    MAX_QTY = 3
    BUY_BIAS = 0.52            # retail leans slightly long

    def on_tick(self, market, portfolio):
        if random.random() > self.TRADE_PROB:
            return None
        symbols = [s for s in market.symbols()
                   if market.best_bid(s) is not None
                   and market.best_ask(s) is not None]
        if not symbols:
            return None
        sym = random.choice(symbols)
        qty = random.randint(1, self.MAX_QTY)
        ask, bid = market.best_ask(sym), market.best_bid(sym)
        if random.random() < self.BUY_BIAS:
            if portfolio.can_buy(sym, qty, ask):
                return Signal(symbol=sym, side="buy", quantity=qty, price=ask)
        elif portfolio.can_sell(sym, qty):
            return Signal(symbol=sym, side="sell", quantity=qty, price=bid)
        return None


class InstitutionalVWAP(Trader):
    """One large parent order, worked in child slices on a fixed clock."""

    SLICE_EVERY = 8            # ticks between child orders
    CHILD_QTY = 5
    PARENT_CHILDREN = 30       # children per parent (150 shares total)
    NEW_PARENT_EVERY = 60      # look for a new parent this often when idle

    def __init__(self) -> None:
        self._tick = 0
        self._symbol: str | None = None
        self._side: str = "buy"
        self._children_left = 0

    def on_tick(self, market, portfolio):
        self._tick += 1

        # Between parents: occasionally pick the next one.
        if self._children_left <= 0:
            if self._tick % self.NEW_PARENT_EVERY != 0:
                return None
            symbols = [s for s in market.symbols()
                       if market.best_bid(s) is not None
                       and market.best_ask(s) is not None]
            if not symbols:
                return None
            self._symbol = random.choice(symbols)
            held = portfolio.positions.get(self._symbol, 0)
            # Sell parents only against real inventory; otherwise accumulate.
            self._side = ("sell" if held >= self.CHILD_QTY * self.PARENT_CHILDREN
                          and random.random() < 0.5 else "buy")
            self._children_left = self.PARENT_CHILDREN
            return None

        # Working the parent: one child per SLICE_EVERY ticks, at the touch.
        if self._tick % self.SLICE_EVERY != 0:
            return None
        sym = self._symbol
        ask, bid = market.best_ask(sym), market.best_bid(sym)
        if ask is None or bid is None:
            return None
        if self._side == "buy":
            if not portfolio.can_buy(sym, self.CHILD_QTY, ask):
                self._children_left = 0          # out of cash: abandon parent
                return None
            self._children_left -= 1
            return Signal(symbol=sym, side="buy",
                          quantity=self.CHILD_QTY, price=ask)
        if not portfolio.can_sell(sym, self.CHILD_QTY):
            self._children_left = 0
            return None
        self._children_left -= 1
        return Signal(symbol=sym, side="sell",
                      quantity=self.CHILD_QTY, price=bid)


def pick_bot() -> Trader:
    """Personality by TEAM_ID: '*vwap*' → institutional, else retail."""
    team_id = os.environ.get("TEAM_ID", "")
    return InstitutionalVWAP() if "vwap" in team_id.lower() else RetailFlow()


if __name__ == "__main__":
    pick_bot().run()
