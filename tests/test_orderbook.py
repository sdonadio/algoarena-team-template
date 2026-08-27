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


# ── Self-trade prevention (cancel-resting) ────────────────────────────────────

class TestSelfTradePrevention:
    def test_own_orders_never_match(self):
        book = OrderBook("AAPL")
        resting, _ = book.place_order("me", "sell", 100.0, 5)
        order, trades = book.place_order("me", "buy", 101.0, 5)
        assert trades == []
        assert [o.order_id for o in book.stp_cancels] == [resting.order_id]
        # The resting ask is gone; the incoming buy now rests alone.
        assert book.best_ask() is None
        assert book.best_bid() == 101.0

    def test_matching_continues_past_own_order(self):
        book = OrderBook("AAPL")
        book.place_order("me", "sell", 100.0, 5)      # own — will be cancelled
        book.place_order("them", "sell", 100.5, 5)    # next level — real fill
        order, trades = book.place_order("me", "buy", 101.0, 5)
        assert len(trades) == 1
        assert trades[0].seller_id == "them"
        assert trades[0].price == pytest.approx(100.5)
        assert len(book.stp_cancels) == 1

    def test_partial_own_shield_in_the_middle(self):
        book = OrderBook("AAPL")
        book.place_order("them", "sell", 100.0, 3)
        book.place_order("me", "sell", 100.5, 3)
        book.place_order("them2", "sell", 101.0, 3)
        _, trades = book.place_order("me", "buy", 101.0, 9)
        assert [t.seller_id for t in trades] == ["them", "them2"]
        assert sum(t.quantity for t in trades) == 6
        assert len(book.stp_cancels) == 1

    def test_firm_level_scope_via_stp_key(self):
        team = {"a_trader": "A", "a_broker": "A", "b_trader": "B"}
        book = OrderBook("AAPL", stp_key=lambda t: team.get(t, t))
        book.place_order("a_broker", "sell", 100.0, 5)
        _, trades = book.place_order("a_trader", "buy", 101.0, 5)
        assert trades == [] and len(book.stp_cancels) == 1
        _, trades = book.place_order("b_trader", "buy", 101.0, 5)
        assert trades == []   # book emptied by the STP cancel above

    def test_stp_cancels_reset_each_call(self):
        book = OrderBook("AAPL")
        book.place_order("me", "sell", 100.0, 5)
        book.place_order("me", "buy", 101.0, 5)
        assert len(book.stp_cancels) == 1
        book.place_order("other", "buy", 99.0, 1)
        assert book.stp_cancels == []


# ── Tick size (Reg NMS Rule 612) ──────────────────────────────────────────────

class TestTickSize:
    def test_snap_toward_the_passive_side(self):
        book = OrderBook("AAPL", tick_size=0.01)
        assert book.snap_to_tick(100.234, "buy") == pytest.approx(100.23)
        assert book.snap_to_tick(100.234, "sell") == pytest.approx(100.24)
        assert book.snap_to_tick(100.23, "buy") == pytest.approx(100.23)
        assert book.snap_to_tick(100.23, "sell") == pytest.approx(100.23)

    def test_orders_rest_and_trade_on_the_grid(self):
        book = OrderBook("AAPL", tick_size=0.01)
        book.place_order("m", "sell", 100.0049, 5)      # rests at 100.01
        assert book.best_ask() == pytest.approx(100.01)
        _, trades = book.place_order("t", "buy", 100.0151, 5)  # snapped to 100.01
        assert len(trades) == 1
        assert trades[0].price == pytest.approx(100.01)

    def test_sub_tick_price_becomes_one_tick(self):
        book = OrderBook("PENNY", tick_size=0.01)
        order, _ = book.place_order("t", "buy", 0.003, 1)
        assert order.price == pytest.approx(0.01)

    def test_zero_tick_disables_snapping(self):
        book = OrderBook("AAPL")                         # pure-logic default
        order, _ = book.place_order("t", "buy", 100.1234, 1)
        assert order.price == pytest.approx(100.1234)

    def test_snapping_cannot_make_an_order_more_aggressive(self):
        book = OrderBook("AAPL", tick_size=0.01)
        book.place_order("m", "sell", 100.24, 5)
        # Buyer asked 100.238 — below the ask; snapping down must not cross.
        _, trades = book.place_order("t", "buy", 100.238, 5)
        assert trades == []
        assert book.best_bid() == pytest.approx(100.23)


# ---------------------------------------------------------------------------
# Queue position & arrival sequence (M1)
# ---------------------------------------------------------------------------

class TestQueuePosition:
    def test_seq_is_monotonic_and_orders_true_arrival(self):
        ob = OrderBook("AAPL")
        o1, _ = place_buy(ob, "a", 100.0, 5)
        o2, _ = place_buy(ob, "b", 100.0, 3)
        o3, _ = place_buy(ob, "c", 100.0, 2)
        assert o1.seq < o2.seq < o3.seq
        # Front of the level (earliest seq) has nothing ahead.
        assert ob.queue_position(o1.order_id) == (0, 10)
        # Second in queue: 5 ahead, level total 10.
        assert ob.queue_position(o2.order_id) == (5, 10)
        # Back of the queue: 8 ahead.
        assert ob.queue_position(o3.order_id) == (8, 10)

    def test_level_qty_is_per_price_and_side(self):
        ob = OrderBook("AAPL")
        a, _ = place_buy(ob, "a", 100.0, 5)
        place_buy(ob, "b", 99.0, 7)        # different price — not counted
        place_sell(ob, "c", 101.0, 4)      # other side — not counted
        assert ob.queue_position(a.order_id) == (0, 5)

    def test_unknown_or_filled_order_is_zero(self):
        ob = OrderBook("AAPL")
        assert ob.queue_position("nope") == (0, 0)
        # A marketable order that fully fills never rests → not tracked.
        place_sell(ob, "m", 100.0, 5)
        taker, trades = place_buy(ob, "t", 100.0, 5)
        assert taker.remaining == 0
        assert ob.queue_position(taker.order_id) == (0, 0)

    def test_cancel_ahead_advances_those_behind(self):
        ob = OrderBook("AAPL")
        front, _ = place_buy(ob, "a", 100.0, 5)
        mid, _ = place_buy(ob, "b", 100.0, 3)
        back, _ = place_buy(ob, "c", 100.0, 2)
        assert ob.queue_position(back.order_id) == (8, 10)
        ob.cancel_order(front.order_id, "a")
        # Front gone: back now has only mid (3) ahead, level shrank to 5.
        assert ob.queue_position(back.order_id) == (3, 5)
        assert ob.queue_position(mid.order_id) == (0, 5)

    def test_partial_fill_ahead_advances_the_queue(self):
        ob = OrderBook("AAPL")
        front, _ = place_buy(ob, "a", 100.0, 5)
        back, _ = place_buy(ob, "b", 100.0, 4)
        assert ob.queue_position(back.order_id) == (5, 9)
        # A sell hits 3 of the front order.
        place_sell(ob, "x", 100.0, 3)
        assert ob.queue_position(back.order_id) == (2, 6)

    def test_reprice_goes_to_back_of_queue(self):
        ob = OrderBook("AAPL")
        a, _ = place_buy(ob, "a", 100.0, 5)   # front
        b, _ = place_buy(ob, "b", 100.0, 3)
        c, _ = place_buy(ob, "c", 100.0, 2)
        assert ob.queue_position(a.order_id) == (0, 10)
        # "a" reprices (cancel + new at same price) — loses time priority.
        ob.cancel_order(a.order_id, "a")
        a2, _ = place_buy(ob, "a", 100.0, 5)
        # a2 is now behind b and c: 3 + 2 = 5 ahead, level back to 10.
        assert ob.queue_position(a2.order_id) == (5, 10)

    def test_orders_at_is_front_to_back(self):
        ob = OrderBook("AAPL")
        a, _ = place_buy(ob, "a", 100.0, 5)
        b, _ = place_buy(ob, "b", 100.0, 3)
        ids = [o.order_id for o in ob.orders_at("buy", 100.0)]
        assert ids == [a.order_id, b.order_id]
        assert ob.orders_at("sell", 100.0) == []
