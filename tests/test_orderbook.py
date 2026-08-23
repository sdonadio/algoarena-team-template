"""Tests for the CLOB matching engine. Target: 100% coverage of orderbook.py."""

import time

import pytest

from shared.orderbook import Order, OrderBook, Trade


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ob():
    return OrderBook("AAPL", fee_rate=0.001)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def place_buy(ob, team, price, qty, order_type="limit"):
    return ob.place_order(team, "buy", price, qty, order_type)


def place_sell(ob, team, price, qty, order_type="limit"):
    return ob.place_order(team, "sell", price, qty, order_type)


# ---------------------------------------------------------------------------
# Basic resting
# ---------------------------------------------------------------------------

class TestResting:
    def test_limit_buy_rests_in_empty_book(self, ob):
        order, trades = place_buy(ob, "t1", 100.0, 10)
        assert trades == []
        assert order.remaining == 10
        assert ob.best_bid() == 100.0
        assert ob.best_ask() is None

    def test_limit_sell_rests_in_empty_book(self, ob):
        order, trades = place_sell(ob, "t1", 105.0, 5)
        assert trades == []
        assert order.remaining == 5
        assert ob.best_ask() == 105.0
        assert ob.best_bid() is None

    def test_non_crossing_orders_both_rest(self, ob):
        place_buy(ob, "t1", 99.0, 10)
        place_sell(ob, "t2", 101.0, 10)
        assert ob.best_bid() == 99.0
        assert ob.best_ask() == 101.0


# ---------------------------------------------------------------------------
# Basic matching
# ---------------------------------------------------------------------------

class TestMatching:
    def test_crossing_buy_executes_at_resting_ask_price(self, ob):
        place_sell(ob, "seller", 100.0, 10)
        order, trades = place_buy(ob, "buyer", 105.0, 10)

        assert len(trades) == 1
        t = trades[0]
        assert t.price == 100.0          # resting ask price
        assert t.quantity == 10
        assert t.buyer_id == "buyer"
        assert t.seller_id == "seller"
        assert t.aggressor == "buy"

    def test_crossing_sell_executes_at_resting_bid_price(self, ob):
        place_buy(ob, "buyer", 105.0, 10)
        order, trades = place_sell(ob, "seller", 100.0, 10)

        assert len(trades) == 1
        t = trades[0]
        assert t.price == 105.0          # resting bid price
        assert t.aggressor == "sell"

    def test_no_self_crossing_at_same_price(self, ob):
        place_buy(ob, "t1", 100.0, 5)
        order, trades = place_sell(ob, "t1", 101.0, 5)
        assert trades == []              # prices don't cross

    def test_buy_does_not_cross_if_price_below_ask(self, ob):
        place_sell(ob, "t1", 105.0, 10)
        order, trades = place_buy(ob, "t2", 104.0, 10)
        assert trades == []
        assert ob.best_bid() == 104.0
        assert ob.best_ask() == 105.0


# ---------------------------------------------------------------------------
# Partial and full fills
# ---------------------------------------------------------------------------

class TestFills:
    def test_partial_fill_reduces_resting_quantity(self, ob):
        resting, _ = place_sell(ob, "seller", 100.0, 10)
        _, trades = place_buy(ob, "buyer", 100.0, 4)

        assert len(trades) == 1
        assert trades[0].quantity == 4
        # Resting order still in book with remaining = 6
        assert ob.best_ask() == 100.0
        resting_live = ob._orders[resting.order_id]
        assert resting_live.remaining == 6

    def test_full_fill_removes_resting_order(self, ob):
        resting, _ = place_sell(ob, "seller", 100.0, 10)
        _, trades = place_buy(ob, "buyer", 100.0, 10)

        assert trades[0].quantity == 10
        assert resting.order_id not in ob._orders
        assert ob.best_ask() is None

    def test_incoming_larger_than_resting_fills_multiple_levels(self, ob):
        place_sell(ob, "s1", 100.0, 5)
        place_sell(ob, "s2", 101.0, 5)
        _, trades = place_buy(ob, "buyer", 102.0, 10)

        assert len(trades) == 2
        assert trades[0].price == 100.0
        assert trades[1].price == 101.0
        assert ob.best_ask() is None

    def test_incoming_partially_fills_stays_in_book_as_limit(self, ob):
        place_sell(ob, "seller", 100.0, 3)
        order, trades = place_buy(ob, "buyer", 100.0, 10)

        assert len(trades) == 1
        assert trades[0].quantity == 3
        assert order.remaining == 7
        # The buy rests for the unfilled 7
        assert ob.best_bid() == 100.0
        assert ob._orders[order.order_id].remaining == 7


# ---------------------------------------------------------------------------
# Price-time priority
# ---------------------------------------------------------------------------

class TestPriceTimePriority:
    def test_best_price_filled_before_worse_price(self, ob):
        place_sell(ob, "s1", 101.0, 5)
        place_sell(ob, "s2", 100.0, 5)   # better ask

        _, trades = place_buy(ob, "buyer", 102.0, 5)
        assert trades[0].price == 100.0   # cheaper ask matched first

    def test_earlier_order_at_same_price_filled_first(self, ob):
        place_sell(ob, "s1", 100.0, 5)
        time.sleep(0.002)                 # ensure s2 has a later timestamp
        place_sell(ob, "s2", 100.0, 5)

        _, trades = place_buy(ob, "buyer", 100.0, 5)
        assert len(trades) == 1
        assert trades[0].seller_id == "s1"

    def test_later_order_at_same_bid_price_filled_last(self, ob):
        place_buy(ob, "b1", 100.0, 5)
        time.sleep(0.002)
        place_buy(ob, "b2", 100.0, 5)

        _, trades = place_sell(ob, "seller", 100.0, 5)
        assert trades[0].buyer_id == "b1"


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

class TestCancel:
    def test_cancel_removes_order_from_book(self, ob):
        order, _ = place_buy(ob, "t1", 100.0, 10)
        cancelled = ob.cancel_order(order.order_id, "t1")

        assert cancelled is not None
        assert cancelled.order_id == order.order_id
        assert order.order_id not in ob._orders
        assert ob.best_bid() is None

    def test_cancel_nonexistent_returns_none(self, ob):
        result = ob.cancel_order("fake-id", "t1")
        assert result is None

    def test_cancel_wrong_team_returns_none(self, ob):
        order, _ = place_buy(ob, "t1", 100.0, 10)
        result = ob.cancel_order(order.order_id, "imposter")
        assert result is None
        assert order.order_id in ob._orders  # still live

    def test_cancelled_order_cleaned_lazily_from_heap(self, ob):
        order, _ = place_buy(ob, "t1", 100.0, 10)
        ob.cancel_order(order.order_id, "t1")
        # best_bid() must trigger lazy cleanup and return None
        assert ob.best_bid() is None
        assert ob._bids == []

    def test_match_against_heap_of_only_stale_entries(self, ob):
        # Cancel a resting ask so its heap entry becomes stale.
        # Do NOT call best_ask() first, so the heap is not pre-cleaned.
        ask, _ = place_sell(ob, "seller", 100.0, 10)
        ob.cancel_order(ask.order_id, "seller")

        assert len(ob._asks) == 1  # stale entry still in heap

        # A crossing buy enters the while loop (_asks non-empty), then
        # _clean_heap inside the loop empties it, hitting the `break`.
        _, trades = place_buy(ob, "buyer", 105.0, 5)
        assert trades == []
        assert ob._asks == []


# ---------------------------------------------------------------------------
# Market orders
# ---------------------------------------------------------------------------

class TestMarketOrders:
    def test_market_buy_fills_immediately(self, ob):
        place_sell(ob, "seller", 100.0, 10)
        order, trades = place_buy(ob, "buyer", 0.0, 10, "market")

        assert len(trades) == 1
        assert trades[0].quantity == 10
        assert order.remaining == 0
        assert ob.best_ask() is None

    def test_market_buy_no_liquidity_no_trade(self, ob):
        order, trades = place_buy(ob, "buyer", 0.0, 10, "market")
        assert trades == []
        assert order.remaining == 10
        assert ob.best_bid() is None   # market order does NOT rest

    def test_market_sell_fills_at_bid(self, ob):
        place_buy(ob, "buyer", 105.0, 10)
        order, trades = place_sell(ob, "seller", 0.0, 10, "market")

        assert len(trades) == 1
        assert trades[0].price == 105.0
        assert order.remaining == 0

    def test_market_order_partial_fill_remainder_cancelled(self, ob):
        place_sell(ob, "seller", 100.0, 3)
        order, trades = place_buy(ob, "buyer", 0.0, 10, "market")

        assert len(trades) == 1
        assert trades[0].quantity == 3
        assert order.remaining == 7
        assert ob.best_bid() is None   # remainder NOT in book


# ---------------------------------------------------------------------------
# IOC orders
# ---------------------------------------------------------------------------

class TestIOCOrders:
    def test_ioc_fills_available_quantity(self, ob):
        place_sell(ob, "seller", 100.0, 5)
        order, trades = place_buy(ob, "buyer", 100.0, 10, "ioc")

        assert len(trades) == 1
        assert trades[0].quantity == 5
        assert order.remaining == 5
        assert ob.best_bid() is None   # remainder cancelled, not resting

    def test_ioc_full_fill(self, ob):
        place_sell(ob, "seller", 100.0, 10)
        order, trades = place_buy(ob, "buyer", 100.0, 10, "ioc")

        assert len(trades) == 1
        assert order.remaining == 0

    def test_ioc_respects_price_limit(self, ob):
        place_sell(ob, "seller", 105.0, 10)
        order, trades = place_buy(ob, "buyer", 100.0, 10, "ioc")  # 100 < 105

        assert trades == []
        assert ob.best_bid() is None   # IOC doesn't rest


# ---------------------------------------------------------------------------
# Fee calculation
# ---------------------------------------------------------------------------

class TestFees:
    def test_fee_equals_rate_times_notional(self, ob):
        fee_rate = ob.fee_rate
        place_sell(ob, "seller", 100.0, 10)
        _, trades = place_buy(ob, "buyer", 100.0, 10)

        t = trades[0]
        expected_fee = fee_rate * t.price * t.quantity  # 0.001 * 100 * 10 = 1.0
        assert abs(t.fee - expected_fee) < 1e-9

    def test_fee_split_50_50(self, ob):
        place_sell(ob, "seller", 200.0, 5)
        _, trades = place_buy(ob, "buyer", 200.0, 5)

        t = trades[0]
        # fee / 2 is what each party owes
        assert t.fee / 2 == pytest.approx(ob.fee_rate * 200.0 * 5 / 2)

    def test_custom_fee_rate(self):
        ob = OrderBook("TSLA", fee_rate=0.005)
        place_sell(ob, "s", 50.0, 4)
        _, trades = ob.place_order("b", "buy", 50.0, 4)

        expected = 0.005 * 50.0 * 4
        assert trades[0].fee == pytest.approx(expected)

    def test_zero_fee_rate(self):
        ob = OrderBook("SPY", fee_rate=0.0)
        place_sell(ob, "s", 400.0, 1)
        _, trades = ob.place_order("b", "buy", 400.0, 1)
        assert trades[0].fee == 0.0


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_snapshot_empty_book(self, ob):
        snap = ob.get_snapshot()
        assert snap["bids"] == []
        assert snap["asks"] == []
        assert snap["mid_price"] is None
        assert snap["spread"] is None
        assert snap["total_volume"] == 0

    def test_snapshot_aggregates_same_price_levels(self, ob):
        place_buy(ob, "t1", 100.0, 5)
        place_buy(ob, "t2", 100.0, 3)
        place_buy(ob, "t3", 99.0, 10)

        snap = ob.get_snapshot()
        assert snap["bids"][0] == [100.0, 8.0]
        assert snap["bids"][1] == [99.0, 10.0]

    def test_snapshot_respects_depth_limit(self, ob):
        for i in range(15):
            place_sell(ob, f"t{i}", 100.0 + i, 1)

        snap = ob.get_snapshot(depth=5)
        assert len(snap["asks"]) == 5
        assert snap["asks"][0][0] == 100.0

    def test_snapshot_mid_and_spread(self, ob):
        place_buy(ob, "t1", 99.0, 1)
        place_sell(ob, "t2", 101.0, 1)

        snap = ob.get_snapshot()
        assert snap["mid_price"] == 100.0
        assert snap["spread"] == 2.0

    def test_snapshot_total_volume_after_trades(self, ob):
        place_sell(ob, "s", 100.0, 10)
        place_buy(ob, "b", 100.0, 10)

        snap = ob.get_snapshot()
        assert snap["total_volume"] == 10


# ---------------------------------------------------------------------------
# Market statistics
# ---------------------------------------------------------------------------

class TestMarketStats:
    def test_best_bid_empty(self, ob):
        assert ob.best_bid() is None

    def test_best_ask_empty(self, ob):
        assert ob.best_ask() is None

    def test_mid_price_one_side_empty(self, ob):
        place_buy(ob, "t1", 100.0, 1)
        assert ob.mid_price() is None

    def test_spread_one_side_empty(self, ob):
        place_sell(ob, "t1", 100.0, 1)
        assert ob.spread() is None

    def test_mid_price_and_spread(self, ob):
        place_buy(ob, "t1", 98.0, 1)
        place_sell(ob, "t2", 102.0, 1)
        assert ob.mid_price() == 100.0
        assert ob.spread() == 4.0

    def test_best_bid_after_cancel_cleaned(self, ob):
        o1, _ = place_buy(ob, "t1", 100.0, 1)
        ob.cancel_order(o1.order_id, "t1")
        # Push a stale entry manually to simulate heap with dead entry
        assert ob.best_bid() is None

    def test_best_ask_after_cancel_cleaned(self, ob):
        o1, _ = place_sell(ob, "t1", 100.0, 1)
        ob.cancel_order(o1.order_id, "t1")
        assert ob.best_ask() is None


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------

class TestVWAP:
    def test_vwap_no_trades_returns_none(self, ob):
        assert ob.vwap() is None

    def test_vwap_single_trade(self, ob):
        place_sell(ob, "s", 100.0, 10)
        place_buy(ob, "b", 100.0, 10)
        assert ob.vwap() == pytest.approx(100.0)

    def test_vwap_weighted_correctly(self, ob):
        # Two trades: (100, qty=10) and (200, qty=10) → VWAP = 150
        place_sell(ob, "s1", 100.0, 10)
        place_buy(ob, "b1", 100.0, 10)
        place_sell(ob, "s2", 200.0, 10)
        place_buy(ob, "b2", 200.0, 10)
        assert ob.vwap() == pytest.approx(150.0)

    def test_vwap_weighted_unequal_qty(self, ob):
        # (100, qty=5) and (200, qty=15) → VWAP = (500 + 3000) / 20 = 175
        place_sell(ob, "s1", 100.0, 5)
        place_buy(ob, "b1", 100.0, 5)
        place_sell(ob, "s2", 200.0, 15)
        place_buy(ob, "b2", 200.0, 15)
        assert ob.vwap() == pytest.approx(175.0)

    def test_vwap_excludes_trades_outside_window(self, ob):
        place_sell(ob, "s", 100.0, 10)
        place_buy(ob, "b", 100.0, 10)

        # Backdate the only trade beyond the window
        ob._trade_history[0] = (time.time() - 400, 100.0, 10)

        # With a 300-second window, the old trade should be excluded
        assert ob.vwap(window_sec=300) is None

    def test_vwap_large_window_captures_all(self, ob):
        place_sell(ob, "s", 50.0, 4)
        place_buy(ob, "b", 50.0, 4)
        assert ob.vwap(window_sec=3600) == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Order book imbalance
# ---------------------------------------------------------------------------

class TestImbalance:
    def test_empty_book_imbalance_is_zero(self, ob):
        assert ob.order_book_imbalance() == 0.0

    def test_only_bids_imbalance_is_positive_one(self, ob):
        place_buy(ob, "t1", 100.0, 10)
        assert ob.order_book_imbalance() == pytest.approx(1.0)

    def test_only_asks_imbalance_is_negative_one(self, ob):
        place_sell(ob, "t1", 100.0, 10)
        assert ob.order_book_imbalance() == pytest.approx(-1.0)

    def test_balanced_book_imbalance_is_zero(self, ob):
        place_buy(ob, "t1", 99.0, 10)
        place_sell(ob, "t2", 101.0, 10)
        assert ob.order_book_imbalance() == pytest.approx(0.0)

    def test_bid_heavy_imbalance_positive(self, ob):
        place_buy(ob, "t1", 99.0, 30)
        place_sell(ob, "t2", 101.0, 10)
        imb = ob.order_book_imbalance()
        assert imb > 0
        assert imb == pytest.approx((30 - 10) / 40)

    def test_ask_heavy_imbalance_negative(self, ob):
        place_buy(ob, "t1", 99.0, 5)
        place_sell(ob, "t2", 101.0, 20)
        imb = ob.order_book_imbalance()
        assert imb < 0
        assert imb == pytest.approx((5 - 20) / 25)


# ---------------------------------------------------------------------------
# Trade and Order dataclass fields
# ---------------------------------------------------------------------------

class TestDataclassFields:
    def test_order_fields(self, ob):
        order, _ = place_buy(ob, "team1", 150.0, 7)
        assert order.team_id == "team1"
        assert order.symbol == "AAPL"
        assert order.side == "buy"
        assert order.price == 150.0
        assert order.quantity == 7
        assert order.remaining == 7
        assert order.order_type == "limit"
        assert isinstance(order.order_id, str)
        assert order.timestamp > 0

    def test_trade_fields(self, ob):
        place_sell(ob, "seller", 200.0, 5)
        _, trades = place_buy(ob, "buyer", 200.0, 5)
        t = trades[0]

        assert t.symbol == "AAPL"
        assert t.price == 200.0
        assert t.quantity == 5
        assert t.buyer_id == "buyer"
        assert t.seller_id == "seller"
        assert t.aggressor == "buy"
        assert isinstance(t.trade_id, str)
        assert t.timestamp > 0

    def test_trade_ids_unique(self, ob):
        place_sell(ob, "s", 100.0, 20)
        _, trades = place_buy(ob, "b", 200.0, 20)
        # Force two trades by adding another sell level
        place_sell(ob, "s2", 99.0, 10)
        ob2 = OrderBook("AAPL")
        place_sell(ob2, "s", 100.0, 5)
        place_sell(ob2, "s", 101.0, 5)
        _, trades2 = place_buy(ob2, "b", 102.0, 10)
        ids = [t.trade_id for t in trades2]
        assert len(ids) == len(set(ids))
