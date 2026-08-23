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
  - Fee is fee_rate × notional, split 50/50 between buyer and seller
"""

from __future__ import annotations

import heapq
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

    def __init__(self, symbol: str, fee_rate: float = 0.001) -> None:
        self.symbol = symbol
        self.fee_rate = fee_rate

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
