"""
Central Limit Order Book (CLOB) matching engine for AlgoArena.

Pure logic — no I/O, no async, no WebSockets. Thread-safety is the
caller's responsibility.

Matching rules:
  - Price-time priority (best price first, then earliest timestamp)
  - Trades always execute at the RESTING order's price
  - Limit orders rest in the book if not immediately matched
  - Market orders fill at any available price; remainder is cancelled
  - IOC orders fill at limit price or better; remainder is cancelled
  - Post-only orders rest without ever crossing; rejected if they would
    (guarantees the maker rebate under the maker/taker fee model)
  - Self-trade prevention (cancel-resting): an order never matches a resting
    order with the same STP key (the bot id by default). The resting order is
    cancelled and matching continues — exactly how real venues implement STP,
    and what makes wash trades impossible.
  - Fee is fee_rate × notional, split 50/50 between buyer and seller
"""

from __future__ import annotations

import heapq
import math
import time
import uuid
from dataclasses import dataclass
from typing import Literal


@dataclass
class Order:
    order_id: str
    team_id: str
    symbol: str
    side: Literal["buy", "sell"]
    price: float
    quantity: int
    remaining: int
    timestamp: float
    order_type: Literal["limit", "market", "ioc", "post_only"]
    rejected: bool = False   # post-only order that would have crossed


@dataclass
class Trade:
    trade_id: str
    symbol: str
    price: float
    quantity: int
    buyer_id: str
    seller_id: str
    aggressor: Literal["buy", "sell"]
    fee: float   # total fee (both sides); each party pays fee / 2
    timestamp: float


class OrderBook:
    """Per-symbol CLOB with price-time priority matching and lazy heap deletion."""

    def __init__(self, symbol: str, fee_rate: float = 0.001,
                 stp_key=None, tick_size: float = 0.0) -> None:
        self.symbol = symbol
        self.fee_rate = fee_rate

        # Minimum price increment (Reg NMS Rule 612: a penny for equities).
        # Incoming prices are snapped toward the passive side — buys round
        # DOWN, sells round UP — so an order is never more aggressive than
        # the sender intended. 0.0 disables snapping (pure-logic default;
        # the exchange passes its configured tick).
        self.tick_size = tick_size

        # Self-trade prevention scope: two orders whose team_ids map to the
        # same key never trade with each other. Identity (bot-level) by
        # default; pass e.g. a roster team-of-bot resolver for firm-level.
        self._stp_key = stp_key or (lambda team_id: team_id)
        # Resting orders cancelled by STP during the LAST place_order call —
        # the caller reads this to notify the cancelled order's owner.
        self.stp_cancels: list[Order] = []

        # Auction mode (pre-open): orders REST without matching; the book
        # crosses once at a single volume-maximizing price via
        # auction_execute(). Continuous matching resumes when cleared.
        self.auction_mode = False

        # Min-heaps for fast best-price access (entries are deleted lazily).
        #   _bids: (-price, timestamp, order_id)  →  highest price pops first
        #   _asks: ( price, timestamp, order_id)  →  lowest  price pops first
        self._bids: list[tuple] = []
        self._asks: list[tuple] = []

        # order_id → Order for every live resting order.
        # Removing an entry here is the canonical delete; heaps clean up lazily.
        self._orders: dict[str, Order] = {}

        # (timestamp, price, quantity) for VWAP and analytics.
        self._trade_history: list[tuple[float, float, int]] = []
        self._total_volume: int = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def place_order(
        self,
        team_id: str,
        side: Literal["buy", "sell"],
        price: float,
        quantity: int,
        order_type: Literal["limit", "market", "ioc", "post_only"] = "limit",
    ) -> tuple[Order, list[Trade]]:
        """Place an order. Returns (order, trades_generated).

        Post-only orders never take liquidity: if the price would cross the
        opposite side, the order is returned with rejected=True and no fills.
        """
        # TODO Level 5: Order-to-trade ratio enforcement
        self.stp_cancels = []
        if order_type != "market":            # market orders carry no price
            price = self.snap_to_tick(price, side)
        now = time.time()
        order = Order(
            order_id=str(uuid.uuid4()),
            team_id=team_id,
            symbol=self.symbol,
            side=side,
            price=price,
            quantity=quantity,
            remaining=quantity,
            timestamp=now,
            order_type=order_type,
        )

        if self.auction_mode:
            # Pre-open: everything rests, nothing matches. The caller
            # rejects non-limit types before they get here.
            self._orders[order.order_id] = order
            if side == "buy":
                heapq.heappush(self._bids, (-order.price, now, order.order_id))
            else:
                heapq.heappush(self._asks, (order.price, now, order.order_id))
            return order, []

        if order_type == "post_only":
            opp = self.best_ask() if side == "buy" else self.best_bid()
            crosses = opp is not None and (
                price >= opp if side == "buy" else price <= opp
            )
            if crosses:
                order.rejected = True
                return order, []
            trades: list[Trade] = []
        else:
            trades = self._match(order)

        # Limit and post-only orders rest; market/IOC cancel any remainder.
        if order_type in ("limit", "post_only") and order.remaining > 0:
            self._orders[order.order_id] = order
            if side == "buy":
                heapq.heappush(self._bids, (-price, now, order.order_id))
            else:
                heapq.heappush(self._asks, (price, now, order.order_id))

        return order, trades

    def cancel_order(self, order_id: str, team_id: str) -> Order | None:
        """Cancel a resting order. Returns the Order, or None if not found / wrong team."""
        order = self._orders.get(order_id)
        if order is None or order.team_id != team_id:
            return None
        del self._orders[order_id]
        # Stale heap entries for this order are removed lazily on next access.
        return order

    def cancel_team_orders(self, team_id: str) -> int:
        """Cancel every resting order belonging to a team (e.g. on liquidation).
        Returns the number of orders cancelled."""
        ids = [oid for oid, o in self._orders.items() if o.team_id == team_id]
        for oid in ids:
            del self._orders[oid]
        return len(ids)

    def get_snapshot(self, depth: int = 10) -> dict:
        """Return a depth-limited book view plus market statistics."""
        bids_by_price: dict[float, int] = {}
        asks_by_price: dict[float, int] = {}

        for order in self._orders.values():
            if order.side == "buy":
                bids_by_price[order.price] = bids_by_price.get(order.price, 0) + order.remaining
            else:
                asks_by_price[order.price] = asks_by_price.get(order.price, 0) + order.remaining

        bids = [[p, float(q)] for p, q in sorted(bids_by_price.items(), reverse=True)[:depth]]
        asks = [[p, float(q)] for p, q in sorted(asks_by_price.items())[:depth]]

        return {
            "bids": bids,
            "asks": asks,
            "mid_price": self.mid_price(),
            "spread": self.spread(),
            "total_volume": self._total_volume,
        }

    def best_bid(self) -> float | None:
        """Highest resting bid price, or None if no bids."""
        self._clean_heap(self._bids)
        return -self._bids[0][0] if self._bids else None

    def best_ask(self) -> float | None:
        """Lowest resting ask price, or None if no asks."""
        self._clean_heap(self._asks)
        return self._asks[0][0] if self._asks else None

    def mid_price(self) -> float | None:
        """(best_bid + best_ask) / 2, or None if either side is empty."""
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0

    def spread(self) -> float | None:
        """best_ask - best_bid, or None if either side is empty."""
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is None or ask is None:
            return None
        return ask - bid

    def vwap(self, window_sec: float = 300) -> float | None:
        """Volume-weighted average price over the last window_sec seconds."""
        if not self._trade_history:
            return None
        cutoff = time.time() - window_sec
        total_value = 0.0
        total_qty = 0
        for ts, price, qty in self._trade_history:
            if ts >= cutoff:
                total_value += price * qty
                total_qty += qty
        if total_qty == 0:
            return None
        return total_value / total_qty

    def order_book_imbalance(self) -> float:
        """
        Signed imbalance of resting volume.
        +1.0 = entirely bids, -1.0 = entirely asks, 0.0 = balanced or empty.
        """
        bid_vol = sum(o.remaining for o in self._orders.values() if o.side == "buy")
        ask_vol = sum(o.remaining for o in self._orders.values() if o.side == "sell")
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    # ------------------------------------------------------------------
    # Auctions — single-price cross (opening / closing)
    # ------------------------------------------------------------------

    def auction_preview(self) -> dict | None:
        """The indicative auction outcome for the current resting book.

        Returns {"price", "volume", "imbalance"} — the volume-maximizing
        clearing price, the shares that would cross there, and the signed
        surplus (buy demand − sell supply, positive = buy pressure) — or
        None if nothing would cross. Non-mutating: this is the indicative
        feed venues publish during the pre-open.
        """
        bids = sorted((o for o in self._orders.values() if o.side == "buy"),
                      key=lambda o: (-o.price, o.timestamp))
        asks = sorted((o for o in self._orders.values() if o.side == "sell"),
                      key=lambda o: (o.price, o.timestamp))
        if not bids or not asks or bids[0].price < asks[0].price:
            return None

        candidates = sorted({o.price for o in bids} | {o.price for o in asks})
        best = None
        for p in candidates:
            demand = sum(o.remaining for o in bids if o.price >= p)
            supply = sum(o.remaining for o in asks if o.price <= p)
            volume = min(demand, supply)
            if volume <= 0:
                continue
            surplus = demand - supply
            key = (volume, -abs(surplus))     # max volume, then min imbalance
            if best is None or key > best[0]:
                best = (key, p, volume, surplus)
        if best is None:
            return None
        _, price, volume, surplus = best
        return {"price": self.snap_to_tick(price, "sell" if surplus >= 0
                                           else "buy"),
                "volume": volume, "imbalance": surplus}

    def auction_execute(self) -> list[Trade]:
        """Cross the resting book once at the single clearing price.

        Eligible bids (price ≥ clearing) meet eligible asks (price ≤
        clearing) in price-time priority; every trade prints AT the
        clearing price. Unexecuted remainder keeps resting for continuous
        trading. Self-matches are skipped (STP), never printed.
        """
        preview = self.auction_preview()
        if preview is None:
            return []
        clearing = preview["price"]
        now = time.time()
        trades: list[Trade] = []
        aggressor: Literal["buy", "sell"] = (
            "buy" if preview["imbalance"] >= 0 else "sell")

        bids = [o for o in sorted(self._orders.values(),
                                  key=lambda o: (-o.price, o.timestamp))
                if o.side == "buy" and o.price >= clearing]
        asks = [o for o in sorted(self._orders.values(),
                                  key=lambda o: (o.price, o.timestamp))
                if o.side == "sell" and o.price <= clearing]

        bi = 0
        for ask in asks:
            while ask.remaining > 0 and bi < len(bids):
                bid = bids[bi]
                if bid.remaining == 0:
                    bi += 1
                    continue
                if self._stp_key(bid.team_id) == self._stp_key(ask.team_id):
                    # Never print a self-match; try the next bid for this ask.
                    swapped = next(
                        (j for j in range(bi + 1, len(bids))
                         if bids[j].remaining > 0
                         and self._stp_key(bids[j].team_id)
                         != self._stp_key(ask.team_id)), None)
                    if swapped is None:
                        break
                    bid = bids[swapped]
                fill = min(bid.remaining, ask.remaining)
                bid.remaining -= fill
                ask.remaining -= fill
                notional = clearing * fill
                trades.append(Trade(
                    trade_id=str(uuid.uuid4()), symbol=self.symbol,
                    price=clearing, quantity=fill,
                    buyer_id=bid.team_id, seller_id=ask.team_id,
                    aggressor=aggressor,
                    fee=round(self.fee_rate * notional, 8), timestamp=now,
                ))
                self._trade_history.append((now, clearing, fill))
                self._total_volume += fill

        for oid in [oid for oid, o in self._orders.items() if o.remaining == 0]:
            del self._orders[oid]
        return trades

    def snap_to_tick(self, price: float, side: Literal["buy", "sell"]) -> float:
        """Snap a price onto the tick grid, toward the passive side.

        Buys round down, sells round up: the snapped order is never MORE
        aggressive than the price the sender asked for. A price below one
        tick becomes one tick (there is no zero or negative price level).
        """
        t = self.tick_size
        if t <= 0:
            return price
        steps = price / t
        if side == "buy":
            snapped = math.floor(steps + 1e-9) * t
        else:
            snapped = math.ceil(steps - 1e-9) * t
        return round(max(snapped, t), 10)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _clean_heap(self, heap: list) -> None:
        """Pop stale (dead) entries from the heap front until a live entry is found."""
        while heap and heap[0][2] not in self._orders:
            heapq.heappop(heap)

    def _match(self, incoming: Order) -> list[Trade]:
        """Match an incoming order against the opposite side using price-time priority."""
        trades: list[Trade] = []
        now = time.time()

        if incoming.side == "buy":
            opposite = self._asks

            def resting_price(entry: tuple) -> float:
                return entry[0]

            def can_cross(rest_price: float) -> bool:
                return incoming.order_type == "market" or incoming.price >= rest_price

        else:
            opposite = self._bids

            def resting_price(entry: tuple) -> float:
                return -entry[0]

            def can_cross(rest_price: float) -> bool:
                return incoming.order_type == "market" or incoming.price <= rest_price

        while opposite and incoming.remaining > 0:
            self._clean_heap(opposite)
            if not opposite:
                break

            entry = opposite[0]
            rest_price = resting_price(entry)
            rest_oid = entry[2]

            if not can_cross(rest_price):
                break

            resting = self._orders[rest_oid]

            # Self-trade prevention (cancel-resting): never match yourself.
            # The resting order is cancelled and matching continues to the
            # next price level, so a bot that crosses its own quote pays by
            # losing queue position — not by printing a wash trade.
            if self._stp_key(resting.team_id) == self._stp_key(incoming.team_id):
                heapq.heappop(opposite)
                del self._orders[rest_oid]
                self.stp_cancels.append(resting)
                continue

            fill_qty = min(incoming.remaining, resting.remaining)
            exec_price = resting.price

            incoming.remaining -= fill_qty
            resting.remaining -= fill_qty

            notional = exec_price * fill_qty
            # TODO Level 3: Maker/taker fee model (replace flat split with rebate/charge)
            total_fee = round(self.fee_rate * notional, 8)

            if incoming.side == "buy":
                buyer_id, seller_id = incoming.team_id, resting.team_id
            else:
                buyer_id, seller_id = resting.team_id, incoming.team_id

            trade = Trade(
                trade_id=str(uuid.uuid4()),
                symbol=self.symbol,
                price=exec_price,
                quantity=fill_qty,
                buyer_id=buyer_id,
                seller_id=seller_id,
                aggressor=incoming.side,
                fee=total_fee,
                timestamp=now,
            )
            trades.append(trade)
            self._trade_history.append((now, exec_price, fill_qty))
            self._total_volume += fill_qty

            # TODO Level 4: Circuit breaker check after each trade

            if resting.remaining == 0:
                heapq.heappop(opposite)
                del self._orders[rest_oid]

        return trades
