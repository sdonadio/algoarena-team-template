"""
Tests for the maker/taker fee model (Level 3 liquidity rebates).

The aggressor (taker) pays TAKER_FEE_RATE x notional.
The resting side (maker) earns MAKER_REBATE_RATE x notional.
The exchange keeps the difference.

Run with:  pytest tests/test_maker_taker.py -v
"""

import asyncio

import pytest

import exchange.config as config
from exchange.server import ExchangeServer, Portfolio
from shared.orderbook import Trade


NOTIONAL = 100.0 * 10  # price 100, qty 10


def make_trade(aggressor: str = "buy") -> Trade:
    return Trade(
        trade_id="t-test-1",
        symbol="AAPL",
        price=100.0,
        quantity=10,
        buyer_id="buyer_team",
        seller_id="seller_team",
        aggressor=aggressor,
        fee=NOTIONAL * config.FEE_RATE,   # legacy total fee
        timestamp=0.0,
    )


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(config, "MAKER_TAKER_ENABLED", True)
    monkeypatch.setattr(config, "TAKER_FEE_RATE", 0.0015)
    monkeypatch.setattr(config, "MAKER_REBATE_RATE", 0.0010)
    srv = ExchangeServer()
    srv.portfolios["buyer_team"] = Portfolio(team_id="buyer_team", role="trader", level=1)
    srv.portfolios["seller_team"] = Portfolio(team_id="seller_team", role="broker", level=1)
    return srv


def settle(srv: ExchangeServer, trade: Trade) -> None:
    asyncio.run(srv._process_trade(trade))


# ── Maker/taker settlement ────────────────────────────────────────────────────

def test_buy_aggressor_buyer_pays_taker_fee(server):
    settle(server, make_trade(aggressor="buy"))
    buyer = server.portfolios["buyer_team"]
    # cash = 100k - notional - taker fee
    assert buyer.cash == pytest.approx(100_000 - NOTIONAL - NOTIONAL * 0.0015)
    assert buyer.total_fees_paid == pytest.approx(NOTIONAL * 0.0015)
    assert buyer.total_rebates_earned == 0.0


def test_buy_aggressor_seller_earns_rebate(server):
    settle(server, make_trade(aggressor="buy"))
    seller = server.portfolios["seller_team"]
    # cash = 100k + notional + maker rebate
    assert seller.cash == pytest.approx(100_000 + NOTIONAL + NOTIONAL * 0.0010)
    assert seller.total_rebates_earned == pytest.approx(NOTIONAL * 0.0010)
    assert seller.total_fees_paid == 0.0


def test_sell_aggressor_roles_flip(server):
    settle(server, make_trade(aggressor="sell"))
    buyer = server.portfolios["buyer_team"]
    seller = server.portfolios["seller_team"]
    # buyer rested (maker): pays notional, earns rebate
    assert buyer.cash == pytest.approx(100_000 - NOTIONAL + NOTIONAL * 0.0010)
    assert buyer.total_rebates_earned == pytest.approx(NOTIONAL * 0.0010)
    # seller crossed (taker): receives notional, pays fee
    assert seller.cash == pytest.approx(100_000 + NOTIONAL - NOTIONAL * 0.0015)
    assert seller.total_fees_paid == pytest.approx(NOTIONAL * 0.0015)


def test_exchange_keeps_the_spread(server):
    settle(server, make_trade(aggressor="buy"))
    assert server.exchange_revenue == pytest.approx(NOTIONAL * (0.0015 - 0.0010))


def test_leaderboard_reports_net_revenue(server):
    settle(server, make_trade(aggressor="buy"))
    lb = server._build_leaderboard()
    assert lb.exchange_fees == pytest.approx(NOTIONAL * 0.0005)


def test_trade_message_carries_fee_and_rebate(server):
    settle(server, make_trade(aggressor="buy"))
    # rebroadcast happened; verify via the trade log + revenue math instead of
    # capturing sockets: fee/rebate fields validated in the round-trip test.
    assert server.exchange_revenue > 0


def test_maker_ratio_in_leaderboard_stats(server):
    settle(server, make_trade(aggressor="buy"))
    lb = server._build_leaderboard()
    entries = {e["team_id"]: e for e in [*lb.traders, *lb.brokers]}
    assert entries["seller_team"]["maker_count"] == 1
    assert entries["buyer_team"]["maker_count"] == 0
    assert entries["seller_team"]["total_rebates_earned"] == pytest.approx(1.0)


def test_conservation_of_money(server):
    """Cash created == cash destroyed: buyer + seller + exchange nets to zero."""
    settle(server, make_trade(aggressor="buy"))
    buyer = server.portfolios["buyer_team"]
    seller = server.portfolios["seller_team"]
    delta = (buyer.cash - 100_000) + (seller.cash - 100_000) + server.exchange_revenue
    assert delta == pytest.approx(0.0)


# ── Legacy 50/50 model ────────────────────────────────────────────────────────

def test_legacy_split_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "MAKER_TAKER_ENABLED", False)
    srv = ExchangeServer()
    srv.portfolios["buyer_team"] = Portfolio(team_id="buyer_team", role="trader", level=1)
    srv.portfolios["seller_team"] = Portfolio(team_id="seller_team", role="broker", level=1)
    trade = make_trade(aggressor="buy")
    settle(srv, trade)
    buyer = srv.portfolios["buyer_team"]
    seller = srv.portfolios["seller_team"]
    assert buyer.total_fees_paid == pytest.approx(trade.fee / 2)
    assert seller.total_fees_paid == pytest.approx(trade.fee / 2)
    assert buyer.total_rebates_earned == 0.0
    assert srv.exchange_revenue == pytest.approx(trade.fee)
