"""
Tests for margin facility, carry costs, position limits, forced liquidation,
conservative marking, and post-only orders.

Run with:  pytest tests/test_risk_controls.py -v
"""

import asyncio

import pytest

import exchange.config as config
from exchange.server import ExchangeServer, Portfolio
from shared.messages import ErrorMsg, PlaceOrder
from shared.orderbook import OrderBook


class FakeWS:
    """Collects messages the server sends to one client."""
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(config, "MARGIN_ENABLED", True)
    monkeypatch.setattr(config, "MARGIN_HAIRCUT", 0.5)
    monkeypatch.setattr(config, "MARGIN_RATE_PER_TICK", 0.0001)
    monkeypatch.setattr(config, "BORROW_FEE_PER_TICK", 0.0001)
    monkeypatch.setattr(config, "LIQUIDATION_ENABLED", True)
    monkeypatch.setattr(config, "MAINTENANCE_FRACTION", 0.10)
    monkeypatch.setattr(config, "POSITION_LIMIT_SHARES", 100)
    srv = ExchangeServer()
    srv.session_open = True
    return srv


def add_team(srv, team_id, role="trader", cash=100_000.0):
    p = Portfolio(team_id=team_id, role=role, level=1, cash=cash)
    srv.portfolios[team_id] = p
    ws = FakeWS()
    srv.clients[team_id] = ws
    return p, ws


def last_error(ws) -> str | None:
    for raw in reversed(ws.sent):
        if '"error"' in raw:
            return ErrorMsg.model_validate_json(raw).code
    return None


def place(srv, ws, team, symbol="AAPL", side="buy", qty=10, price=100.0,
          order_type="limit"):
    msg = PlaceOrder(team_id=team, symbol=symbol, side=side,
                     order_type=order_type, price=price, quantity=qty)
    run(srv._handle_place_order(ws, msg, team))


# ── Position limits ───────────────────────────────────────────────────────────

def test_position_limit_rejects_oversized_buy(server):
    p, ws = add_team(server, "t1")
    place(server, ws, "t1", qty=101)
    assert last_error(ws) == "POSITION_LIMIT"


def test_position_limit_counts_existing_position(server):
    p, ws = add_team(server, "t1")
    p.positions["AAPL"] = 95
    place(server, ws, "t1", qty=10)
    assert last_error(ws) == "POSITION_LIMIT"


def test_position_limit_applies_to_shorts(server):
    p, ws = add_team(server, "t1")
    place(server, ws, "t1", side="sell", qty=101)
    assert last_error(ws) == "POSITION_LIMIT"


def test_within_limit_is_accepted(server):
    p, ws = add_team(server, "t1")
    place(server, ws, "t1", qty=50)
    assert last_error(ws) is None


# ── Margin buying power ───────────────────────────────────────────────────────

def test_trader_cannot_borrow(server):
    p, ws = add_team(server, "t1", role="trader", cash=100.0)
    place(server, ws, "t1", qty=10, price=100.0)
    assert last_error(ws) == "INSUFFICIENT_CASH"


def test_broker_borrows_against_inventory(server):
    p, ws = add_team(server, "b1", role="broker", cash=100.0)
    p.positions["AAPL"] = 50                       # inventory worth 50 × ref
    server.ref_prices["AAPL"] = 100.0
    # buying power = 100 + 0.5 × 5000 = 2600 → a $1000 order passes
    place(server, ws, "b1", qty=10, price=100.0)
    assert last_error(ws) is None


def test_broker_borrowing_is_capped(server):
    p, ws = add_team(server, "b1", role="broker", cash=100.0)
    p.positions["AAPL"] = 50
    server.ref_prices["AAPL"] = 100.0
    place(server, ws, "b1", qty=30, price=100.0)   # $3000 > $2600 buying power
    assert last_error(ws) == "INSUFFICIENT_CASH"


# ── Carry costs ───────────────────────────────────────────────────────────────

def test_interest_charged_on_borrowed_cash(server):
    p, _ = add_team(server, "b1", role="broker", cash=-10_000.0)
    p.positions["AAPL"] = 500   # keep net worth above maintenance
    server.ref_prices["AAPL"] = 100.0
    run(server._apply_carry_and_maintenance())
    assert p.total_carry_paid == pytest.approx(10_000 * 0.0001)
    assert p.cash == pytest.approx(-10_000 - 1.0)


def test_borrow_fee_charged_on_shorts(server):
    p, _ = add_team(server, "t1", cash=100_000.0)
    p.positions["AAPL"] = -50
    server.ref_prices["AAPL"] = 100.0
    run(server._apply_carry_and_maintenance())
    assert p.total_carry_paid == pytest.approx(5_000 * 0.0001)


def test_no_carry_on_positive_cash_and_longs(server):
    p, _ = add_team(server, "t1", cash=100_000.0)
    p.positions["AAPL"] = 50
    server.ref_prices["AAPL"] = 100.0
    run(server._apply_carry_and_maintenance())
    assert p.total_carry_paid == 0.0


# ── Liquidation ───────────────────────────────────────────────────────────────

def test_team_below_maintenance_is_liquidated(server):
    p, ws = add_team(server, "t1")     # starts with 100k → maintenance 10k
    p.cash = 5_000.0                   # losses take net worth below 10k
    run(server._apply_carry_and_maintenance())
    assert p.liquidated is True


def test_liquidation_flattens_positions_with_penalty(server):
    p, _ = add_team(server, "t1")      # starts with 100k → maintenance 10k
    p.cash = 2_000.0
    p.positions["AAPL"] = 50
    p.avg_cost["AAPL"] = 100.0
    server.ref_prices["AAPL"] = 100.0  # nw = 2000 + 5000 = 7000 < 10000
    run(server._apply_carry_and_maintenance())
    assert p.liquidated is True
    assert p.positions["AAPL"] == 0
    # sold 1% through the mark: 50 × 99 = 4950
    assert p.cash == pytest.approx(2_000 + 50 * 99.0)


def test_liquidated_team_cannot_trade(server):
    p, ws = add_team(server, "t1")
    p.liquidated = True
    place(server, ws, "t1", qty=1)
    assert last_error(ws) == "LIQUIDATED"


def test_healthy_team_not_liquidated(server):
    p, _ = add_team(server, "t1", cash=100_000.0)
    run(server._apply_carry_and_maintenance())
    assert p.liquidated is False


# ── Conservative marking ──────────────────────────────────────────────────────

def test_longs_marked_at_bid_shorts_at_ask():
    p = Portfolio(team_id="t", role="trader", level=1, cash=0.0)
    p.positions = {"AAPL": 10, "TSLA": -10}
    ref = {"AAPL": 100.0, "TSLA": 100.0}
    marks = {"AAPL": (98.0, 102.0), "TSLA": (98.0, 102.0)}
    # long 10 @ bid 98 = 980; short 10 @ ask 102 = -1020
    assert p.net_worth(ref, marks) == pytest.approx(980 - 1020)
    # without marks, both at ref 100 → net 0
    assert p.net_worth(ref) == pytest.approx(0.0)


# ── Post-only orders ──────────────────────────────────────────────────────────

def test_post_only_rests_when_not_crossing():
    book = OrderBook("AAPL")
    book.place_order("m1", "sell", 101.0, 10)
    order, trades = book.place_order("m2", "buy", 99.0, 10, order_type="post_only")
    assert not order.rejected and trades == []
    assert book.best_bid() == 99.0


def test_post_only_rejected_when_crossing():
    book = OrderBook("AAPL")
    book.place_order("m1", "sell", 101.0, 10)
    order, trades = book.place_order("m2", "buy", 101.0, 10, order_type="post_only")
    assert order.rejected and trades == []
    assert book.best_bid() is None


def test_post_only_sell_rejected_when_crossing():
    book = OrderBook("AAPL")
    book.place_order("m1", "buy", 100.0, 10)
    order, _ = book.place_order("m2", "sell", 100.0, 10, order_type="post_only")
    assert order.rejected


def test_post_only_rejected_via_server(server, monkeypatch):
    monkeypatch.setattr(config, "POSITION_LIMIT_SHARES", 0)
    p1, ws1 = add_team(server, "m1", role="broker")
    p2, ws2 = add_team(server, "m2", role="broker")
    place(server, ws1, "m1", side="sell", qty=10, price=101.0)
    place(server, ws2, "m2", side="buy", qty=10, price=101.0, order_type="post_only")
    assert last_error(ws2) == "POST_ONLY_CROSS"


def test_cancel_team_orders():
    book = OrderBook("AAPL")
    book.place_order("m1", "buy", 99.0, 10)
    book.place_order("m1", "buy", 98.0, 10)
    book.place_order("m2", "buy", 97.0, 10)
    assert book.cancel_team_orders("m1") == 2
    assert book.best_bid() == 97.0
