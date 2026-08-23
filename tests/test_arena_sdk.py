"""
The arena student SDK: three base classes whose hooks must reach the engine.

These tests are the contract the student experience rests on: an empty
subclass runs, every documented hook is called with the documented
arguments, and a hook that misbehaves cannot take the venue down.

Run with:  pytest tests/test_arena_sdk.py -v
"""

import asyncio
import json

import pytest

import exchange.config as ex_config
import trader.config as trader_config
from arena import Broker, Exchange, Signal, Trader
from arena.broker import _AdaptedBrokerBot
from arena.exchange import _AdaptedExchangeServer
from arena.trader import _AdaptedTraderBot
from shared.messages import Handshake, PlaceOrder, TradeExecution


def run(coro):
    return asyncio.run(coro)


# ── Trader ────────────────────────────────────────────────────────────────────

class _CountingTrader(Trader):
    def __init__(self):
        self.ticks = 0
        self.fills = []
        self.events = []

    def on_tick(self, market, portfolio):
        self.ticks += 1
        return Signal(symbol="AAPL", side="buy", quantity=1, price=100.0)

    def on_fill(self, side, symbol, quantity, price):
        self.fills.append((side, symbol, quantity, price))

    def on_event(self, event, message, data):
        self.events.append(event)


def test_trader_on_tick_becomes_the_strategy():
    owner = _CountingTrader()
    bot = _AdaptedTraderBot(owner)
    sig = bot.strategy.generate_signal(bot.market, bot.portfolio)
    assert owner.ticks == 1
    assert sig.symbol == "AAPL" and sig.side == "buy"


def test_trader_on_fill_fires_only_for_own_trades(monkeypatch):
    monkeypatch.setattr(trader_config, "TEAM_ID", "me")
    owner = _CountingTrader()
    bot = _AdaptedTraderBot(owner)
    mine = TradeExecution(trade_id="t1", symbol="AAPL", price=100.0,
                          quantity=2, buyer_id="me", seller_id="other",
                          aggressor="buy", fee=0.1, tick=1)
    theirs = mine.model_copy(update={"buyer_id": "a", "seller_id": "b"})
    bot._on_trade(mine)
    bot._on_trade(theirs)
    assert owner.fills == [("buy", "AAPL", 2, 100.0)]


def test_trader_is_abstract_without_on_tick():
    with pytest.raises(TypeError):
        Trader()  # type: ignore[abstract]


# ── Broker ────────────────────────────────────────────────────────────────────

class _OpinionatedBroker(Broker):
    def spread(self, symbol, price, history):
        self.seen = (symbol, price, list(history))
        return 0.42

    def skew(self, symbol, inventory):
        return -inventory * 0.01

    def toxic(self, trader_id):
        return trader_id == "shark"


def test_broker_hooks_reach_the_quoting_state():
    owner = _OpinionatedBroker()
    bot = _AdaptedBrokerBot(owner, "ws://unused:1")
    bot.state.exchange_prices["AAPL"] = 200.0
    bot.state.price_history["AAPL"].extend([199.0, 200.0])
    bot.state.positions["AAPL"] = 30

    assert bot.state.compute_spread("AAPL") == pytest.approx(0.42)
    assert owner.seen == ("AAPL", 200.0, [199.0, 200.0])
    assert bot.state.compute_skew("AAPL") == pytest.approx(-0.30)
    assert bot.state.is_toxic("shark") is True
    assert bot.state.is_toxic("lamb") is False


def test_empty_broker_subclass_uses_the_defaults():
    bot = _AdaptedBrokerBot(Broker(), "ws://unused:1")
    bot.state.exchange_prices["AAPL"] = 200.0
    default = bot.state.compute_spread("AAPL")
    assert default > 0
    assert bot.state.compute_skew("AAPL") == 0.0
    assert bot.state.is_toxic("anyone") is False


# ── Exchange ──────────────────────────────────────────────────────────────────

class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


def _errors(ws, code):
    return [d for d in map(json.loads, ws.sent)
            if d.get("type") == "error" and d.get("code") == code]


class _PickyExchange(Exchange):
    taker_bps = 8.0
    rebate_bps = 5.0

    def __init__(self):
        self.trades = []

    def accept_order(self, order, portfolio):
        if order.quantity > 5:
            return False, "too big for this venue"
        return True, ""

    def on_trade(self, symbol, price, quantity, buyer_id, seller_id):
        self.trades.append((symbol, quantity))


@pytest.fixture
def picky(monkeypatch):
    monkeypatch.setattr(ex_config, "RECORD_SESSIONS", False)
    monkeypatch.setattr(ex_config, "TAKER_FEE_RATE", 0.0015)
    monkeypatch.setattr(ex_config, "MAKER_REBATE_RATE", 0.0010)
    policy = _PickyExchange()
    policy._apply_fees()
    srv = _AdaptedExchangeServer(policy)
    srv.session_open = True
    return policy, srv


def test_exchange_fee_schedule_is_applied_and_clamped(picky):
    assert ex_config.TAKER_FEE_RATE == pytest.approx(0.0008)
    assert ex_config.MAKER_REBATE_RATE == pytest.approx(0.0005)

    class Greedy(Exchange):
        taker_bps = 100.0            # above the class ceiling
        rebate_bps = 100.0           # above taker − net floor

    Greedy()._apply_fees()
    assert ex_config.TAKER_FEE_RATE == pytest.approx(
        ex_config.TAKER_MAX_BPS / 10_000.0)
    assert ex_config.MAKER_REBATE_RATE == pytest.approx(
        (ex_config.TAKER_MAX_BPS - ex_config.VENUE_NET_MIN_BPS) / 10_000.0)


def test_exchange_accept_order_vetoes_before_the_book(picky):
    policy, srv = picky
    ws = FakeWS()
    run(srv.handle_client(_handshaking_ws(ws, "t1")))
    big = PlaceOrder(team_id="t1", symbol="AAPL", side="buy",
                     order_type="limit", price=100.0, quantity=6)
    run(srv._handle_place_order(ws, big, "t1"))
    rejects = _errors(ws, "VENUE_REJECTED")
    assert rejects and "too big" in rejects[0]["message"]


def test_exchange_on_trade_sees_broadcast_trades(picky):
    policy, srv = picky
    trade = TradeExecution(trade_id="x", symbol="AAPL", price=100.0,
                           quantity=3, buyer_id="a", seller_id="b",
                           aggressor="buy", fee=0.1, tick=1)
    run(srv._broadcast(trade))
    assert policy.trades == [("AAPL", 3)]


def _handshaking_ws(ws, team_id):
    """Wrap a FakeWS so handle_client sees one handshake then EOF."""
    class _WS:
        def __init__(self):
            self.sent = ws.sent

        async def recv(self):
            return Handshake(team_id=team_id, role="trader",
                             level=1).model_dump_json()

        async def send(self, data):
            await ws.send(data)

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration
    return _WS()
