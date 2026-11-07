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

* **ToxicTaker** — informed, aggressive flow. It does not post, ever: it
  crosses the spread, in size, in the direction the market is about to
  move, and it is gone before the move is finished. Its edge is speed of
  reaction, not foresight: `CALENDAR_EVENT` publishes the realised `pct`
  the instant the ramp starts (see exchange/server.py), so a bot that
  reads the event hook and fires on the same tick is trading against
  every quote that has not been pulled yet. Between events it probes
  short-horizon momentum. This is the counterparty a market maker must
  learn to detect and price — the "toxic flow" of Level 5 and of the
  market-making session — and the reason a naive two-sided quote bleeds.

All three are MEAN-REVERTING IN INVENTORY over long horizons: retail's buy
lean fades and flips as a position builds, the VWAP desk alternates
accumulation and distribution parents, and the toxic taker unwinds to flat
once its burst is spent. This matters in a session that stays
open for days — flow that only ever buys pins the market maker into a
permanently growing short, whose financing costs then bankrupt the only
two-sided quote in the book.

Which personality a process gets is decided by its TEAM_ID: ids containing
"toxic" run the informed taker, ids containing "vwap" run the institutional
slicer, everything else runs retail flow.

Run (ids come from the roster):
    TEAM_ID=flow_retail_1 python -m trader.flow_bots
    TEAM_ID=flow_vwap_1   python -m trader.flow_bots
    TEAM_ID=flow_toxic_1  python -m trader.flow_bots
"""

from __future__ import annotations

import os
import random

from arena import Signal, Trader


class RetailFlow(Trader):
    """Small random marketable orders — the uninformed flow.

    Two-sided over any long horizon. Retail leans slightly long when flat
    (BUY_BIAS), but the lean FADES as the account fills up and flips to
    selling once a name is at INVENTORY_TARGET: households take profits and
    raise cash, they do not buy the same stock forever. Without this the
    population is a pure net buyer — it buys until it is out of cash — and
    the only counterparty (the market maker) is forced into an ever-growing
    short that financing costs eventually kill.
    """

    TRADE_PROB = 0.25          # fire on ~1 tick in 4
    MAX_QTY = 3
    BUY_BIAS = 0.52            # retail leans slightly long when flat
    INVENTORY_TARGET = 50      # shares per symbol the account drifts toward
    INVENTORY_PULL = 0.40      # how hard inventory bends the side choice

    def buy_probability(self, held: int) -> float:
        """Chance the next order is a buy, given `held` shares of that name.

        Flat → BUY_BIAS. At (or beyond) INVENTORY_TARGET → BUY_BIAS −
        INVENTORY_PULL, i.e. mostly sells. Clamped to [0, 1].
        """
        target = max(1, self.INVENTORY_TARGET)
        lean = max(-1.0, min(1.0, held / target))
        return max(0.0, min(1.0, self.BUY_BIAS - self.INVENTORY_PULL * lean))

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
        held = portfolio.positions.get(sym, 0)
        if random.random() < self.buy_probability(held):
            if portfolio.can_buy(sym, qty, ask):
                return Signal(symbol=sym, side="buy", quantity=qty, price=ask)
        elif portfolio.can_sell(sym, qty):
            return Signal(symbol=sym, side="sell", quantity=qty, price=bid)
        return None


class InstitutionalVWAP(Trader):
    """One large parent order, worked in child slices on a fixed clock.

    Programs ALTERNATE: an accumulation is followed by a distribution of
    what it just bought. A real execution desk works both directions, and
    the alternative — buying every parent until the account is out of cash
    — is a permanent one-way pressure that forces the market maker short
    forever. Each parent is still strictly one-sided in one symbol on a
    fixed clock, which is the pattern students are meant to detect.
    """

    SLICE_EVERY = 8            # ticks between child orders
    CHILD_QTY = 5
    PARENT_CHILDREN = 30       # children per parent (150 shares total)
    NEW_PARENT_EVERY = 60      # look for a new parent this often when idle

    def __init__(self) -> None:
        self._tick = 0
        self._symbol: str | None = None
        self._side: str = "buy"
        self._children_left = 0
        self._accumulated = False     # last parent was a buy → unwind next

    def _next_parent(self, market, portfolio) -> None:
        """Choose the next parent order: distribute, else accumulate."""
        symbols = [s for s in market.symbols()
                   if market.best_bid(s) is not None
                   and market.best_ask(s) is not None]
        if not symbols:
            return
        heaviest = max(symbols, key=lambda s: portfolio.positions.get(s, 0))
        held = portfolio.positions.get(heaviest, 0)
        if self._accumulated and held >= self.CHILD_QTY:
            # Work the inventory back out — never more than we actually hold.
            self._symbol = heaviest
            self._side = "sell"
            self._children_left = min(self.PARENT_CHILDREN,
                                      held // self.CHILD_QTY)
        else:
            self._symbol = random.choice(symbols)
            self._side = "buy"
            self._children_left = self.PARENT_CHILDREN
        self._accumulated = self._side == "buy"

    def on_tick(self, market, portfolio):
        self._tick += 1

        # Between parents: occasionally pick the next one.
        if self._children_left <= 0:
            if self._tick % self.NEW_PARENT_EVERY != 0:
                return None
            self._next_parent(market, portfolio)
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


class ToxicTaker(Trader):
    """Informed, aggressive flow — the market maker's natural predator.

    Never posts. Three modes, in priority order:

    1. **Burst.** `CALENDAR_EVENT` (and `SHOCK`) carry the realised `pct` of
       the move that is starting, and the exchange then ramps the price over
       `ramp_ticks`. The taker fires on the same tick, crossing the spread in
       the direction of the ramp for `BURST_TICKS` ticks. Every fill it gets
       is a quote somebody left up one tick too long — that is adverse
       selection, measurable as a markout in `live_readiness.py`.
    2. **Probe.** Between events it reads short-horizon momentum from
       `market.prices()` and takes the touch when the drift exceeds
       `PROBE_TRIGGER`. Small, occasional, and the reason a market maker
       cannot simply widen for thirty seconds after each print.
    3. **Unwind.** `UNWIND_AFTER` ticks past the last burst it works back to
       flat, crossing. Informed flow does not sit on inventory, and a bot
       that only ever bought would pin the house market maker into a
       permanently growing short (see the module docstring).

    Uses only `symbols` / `best_bid` / `best_ask` / `prices`, so it runs
    unchanged against the live `MarketData` and the offline sim view.
    """

    BURST_TICKS = 20           # how long one event burst lasts
    BURST_QTY = 20             # shares per aggressive child
    MIN_PCT = 0.004            # ignore moves smaller than this
    PROBE_LOOKBACK = 12        # ticks of mid history for the momentum probe
    PROBE_TRIGGER = 0.0025     # drift over the lookback that arms a probe
    PROBE_QTY = 6
    PROBE_EVERY = 5            # at most one probe this often
    UNWIND_AFTER = 90          # ticks of quiet before working back to flat
    UNWIND_QTY = 15
    MAX_POSITION = 400         # per symbol, either side

    def __init__(self) -> None:
        self._tick = 0
        self._burst_left = 0
        self._burst_symbol: str | None = None
        self._burst_side = "buy"
        self._last_burst_tick = -10 ** 9

    # ── the event hook is the whole edge ──────────────────────────────
    def on_event(self, event: str, message: str, data: dict) -> None:
        if event not in ("CALENDAR_EVENT", "SHOCK"):
            return
        try:
            pct = float(data.get("pct") or 0.0)
        except (TypeError, ValueError):
            pct = 0.0
        if abs(pct) < self.MIN_PCT:
            return
        affected = [s for s in (data.get("affected") or []) if s]
        symbol = data.get("symbol") or (affected[0] if affected else None)
        if not symbol:
            return
        self._burst_symbol = str(symbol)
        self._burst_side = "buy" if pct > 0 else "sell"
        self._burst_left = self.BURST_TICKS
        self._last_burst_tick = self._tick

    # ── helpers ───────────────────────────────────────────────────────
    @staticmethod
    def _touch(market, symbol: str, side: str) -> float | None:
        return (market.best_ask(symbol) if side == "buy"
                else market.best_bid(symbol))

    def _take(self, market, portfolio, symbol: str, side: str,
              qty: int) -> Signal | None:
        """One marketable order, or None if it is not fundable/allowed."""
        price = self._touch(market, symbol, side)
        if price is None or qty <= 0:
            return None
        held = portfolio.positions.get(symbol, 0)
        room = self.MAX_POSITION - (held if side == "buy" else -held)
        qty = min(qty, max(0, room))
        if qty <= 0:
            return None
        if side == "buy":
            if not portfolio.can_buy(symbol, qty, price):
                return None
        elif not portfolio.can_sell(symbol, qty):
            return None
        return Signal(symbol=symbol, side=side, quantity=qty, price=price)

    def _drift(self, market, symbol: str) -> float:
        prices = market.prices(symbol)
        if len(prices) < self.PROBE_LOOKBACK:
            return 0.0
        first, last = prices[-self.PROBE_LOOKBACK], prices[-1]
        return (last - first) / first if first else 0.0

    # ── the loop ──────────────────────────────────────────────────────
    def on_tick(self, market, portfolio):
        self._tick += 1

        # 1. burst on the event
        if self._burst_left > 0 and self._burst_symbol:
            self._burst_left -= 1
            sig = self._take(market, portfolio, self._burst_symbol,
                             self._burst_side, self.BURST_QTY)
            if sig is not None:
                return sig

        # 2. unwind once the dust has settled
        if self._tick - self._last_burst_tick > self.UNWIND_AFTER:
            for symbol, held in sorted(portfolio.positions.items()):
                if abs(held) < self.UNWIND_QTY:
                    continue
                side = "sell" if held > 0 else "buy"
                sig = self._take(market, portfolio, symbol, side,
                                 self.UNWIND_QTY)
                if sig is not None:
                    return sig

        # 3. probe short-horizon momentum
        if self._tick % self.PROBE_EVERY:
            return None
        best_sym, best_drift = None, 0.0
        for symbol in market.symbols():
            if (market.best_bid(symbol) is None
                    or market.best_ask(symbol) is None):
                continue
            drift = self._drift(market, symbol)
            if abs(drift) > abs(best_drift):
                best_sym, best_drift = symbol, drift
        if best_sym is None or abs(best_drift) < self.PROBE_TRIGGER:
            return None
        side = "buy" if best_drift > 0 else "sell"
        return self._take(market, portfolio, best_sym, side, self.PROBE_QTY)


def pick_bot() -> Trader:
    """Personality by TEAM_ID: '*toxic*' → informed, '*vwap*' → institutional,
    else retail."""
    team_id = os.environ.get("TEAM_ID", "").lower()
    if "toxic" in team_id:
        return ToxicTaker()
    return InstitutionalVWAP() if "vwap" in team_id else RetailFlow()


if __name__ == "__main__":
    pick_bot().run()
