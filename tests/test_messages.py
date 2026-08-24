"""Round-trip serialization and dispatch tests for shared/messages.py."""

import json

import pytest
from pydantic import ValidationError

from shared.messages import (
    BookSnapshot,
    CancelOrder,
    ErrorMsg,
    Handshake,
    IPOSubscribe,
    Leaderboard,
    OrderAck,
    PlaceOrder,
    PortfolioUpdate,
    SecurityDef,
    SessionEvent,
    ShockResult,
    Signal,
    TeacherCommand,
    TradeExecution,
    parse_message,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def round_trip(model_instance):
    """Serialise to JSON and back; assert equality."""
    raw = json.loads(model_instance.model_dump_json())
    restored = model_instance.__class__.model_validate(raw)
    assert restored == model_instance
    return raw


# ---------------------------------------------------------------------------
# Client → Exchange: round-trip tests
# ---------------------------------------------------------------------------

class TestHandshake:
    def test_round_trip_trader(self):
        msg = Handshake(team_id="team_alpha", role="trader", level=2)
        round_trip(msg)

    def test_round_trip_broker(self):
        msg = Handshake(team_id="broker_1", role="broker", level=4)
        round_trip(msg)

    def test_round_trip_observer(self):
        msg = Handshake(team_id="obs", role="observer", level=1)
        round_trip(msg)

    def test_round_trip_teacher(self):
        msg = Handshake(team_id="teacher_dash", role="teacher", level=1)
        round_trip(msg)

    def test_type_discriminator(self):
        msg = Handshake(team_id="x", role="trader", level=1)
        assert msg.type == "handshake"

    def test_invalid_role(self):
        with pytest.raises(ValidationError):
            Handshake(team_id="x", role="admin", level=1)

    def test_missing_team_id(self):
        with pytest.raises(ValidationError):
            Handshake(role="trader", level=1)


class TestPlaceOrder:
    def test_round_trip_limit(self):
        msg = PlaceOrder(
            team_id="t1", symbol="AAPL", side="buy",
            order_type="limit", price=150.50, quantity=10,
        )
        round_trip(msg)

    def test_round_trip_market(self):
        msg = PlaceOrder(
            team_id="t1", symbol="TSLA", side="sell",
            order_type="market", price=0.0, quantity=5,
        )
        round_trip(msg)

    def test_round_trip_ioc(self):
        msg = PlaceOrder(
            team_id="t2", symbol="BTC", side="buy",
            order_type="ioc", price=30000.0, quantity=1,
        )
        round_trip(msg)

    def test_type_discriminator(self):
        msg = PlaceOrder(
            team_id="t1", symbol="X", side="buy",
            order_type="limit", price=1.0, quantity=1,
        )
        assert msg.type == "place_order"

    def test_invalid_side(self):
        with pytest.raises(ValidationError):
            PlaceOrder(
                team_id="t1", symbol="X", side="long",
                order_type="limit", price=1.0, quantity=1,
            )

    def test_invalid_order_type(self):
        with pytest.raises(ValidationError):
            PlaceOrder(
                team_id="t1", symbol="X", side="buy",
                order_type="teleport", price=1.0, quantity=1,
            )

    def test_stop_order_round_trip(self):
        msg = PlaceOrder(team_id="t1", symbol="AAPL", side="sell",
                         order_type="stop_limit", price=194.0, quantity=10,
                         stop_price=195.0)
        again = PlaceOrder.model_validate_json(msg.model_dump_json())
        assert again.stop_price == pytest.approx(195.0)
        assert again.order_type == "stop_limit"

    def test_stop_price_is_optional_for_wire_compat(self):
        raw = ('{"type":"place_order","team_id":"t1","symbol":"X",'
               '"side":"buy","order_type":"limit","price":1.0,"quantity":1}')
        assert PlaceOrder.model_validate_json(raw).stop_price is None

    def test_missing_quantity(self):
        with pytest.raises(ValidationError):
            PlaceOrder(
                team_id="t1", symbol="X", side="buy",
                order_type="limit", price=1.0,
            )


class TestCancelOrder:
    def test_round_trip(self):
        msg = CancelOrder(team_id="t1", order_id="ord-001", symbol="AAPL")
        round_trip(msg)

    def test_type_discriminator(self):
        msg = CancelOrder(team_id="t1", order_id="o", symbol="X")
        assert msg.type == "cancel_order"

    def test_missing_order_id(self):
        with pytest.raises(ValidationError):
            CancelOrder(team_id="t1", symbol="AAPL")


class TestTeacherCommand:
    def test_round_trip_open_session(self):
        msg = TeacherCommand(command="open_session")
        raw = round_trip(msg)
        assert raw["type"] == "teacher_command"
        assert raw["command"] == "open_session"
        assert raw["params"] == {}

    def test_round_trip_inject_shock(self):
        msg = TeacherCommand(
            command="inject_shock",
            params={"shock_id": "flash_crash"},
        )
        raw = round_trip(msg)
        assert raw["params"]["shock_id"] == "flash_crash"

    def test_round_trip_set_fee_rate(self):
        msg = TeacherCommand(command="set_fee_rate", params={"rate": 0.002})
        raw = round_trip(msg)
        assert raw["params"]["rate"] == pytest.approx(0.002)

    def test_type_discriminator(self):
        msg = TeacherCommand(command="close_session")
        assert msg.type == "teacher_command"

    def test_parse_message_dispatch(self):
        raw = {"type": "teacher_command", "command": "open_session", "params": {}}
        msg = parse_message(raw)
        assert isinstance(msg, TeacherCommand)
        assert msg.command == "open_session"

    def test_missing_command(self):
        with pytest.raises(ValidationError):
            TeacherCommand(params={"x": 1})


# ---------------------------------------------------------------------------
# Exchange → Client: round-trip tests
# ---------------------------------------------------------------------------

class TestOrderAck:
    def test_round_trip(self):
        msg = OrderAck(
            order_id="ord-42", team_id="t1", symbol="NVDA",
            side="buy", price=420.0, quantity=3,
        )
        round_trip(msg)

    def test_type_discriminator(self):
        msg = OrderAck(
            order_id="o", team_id="t", symbol="X",
            side="sell", price=1.0, quantity=1,
        )
        assert msg.type == "order_ack"

    def test_invalid_side(self):
        with pytest.raises(ValidationError):
            OrderAck(
                order_id="o", team_id="t", symbol="X",
                side="short", price=1.0, quantity=1,
            )


class TestTradeExecution:
    def test_round_trip(self):
        msg = TradeExecution(
            trade_id="tr-1", symbol="TSLA", price=200.0, quantity=5,
            buyer_id="b1", seller_id="s1", aggressor="buy", fee=0.10,
        )
        round_trip(msg)

    def test_type_discriminator(self):
        msg = TradeExecution(
            trade_id="t", symbol="X", price=1.0, quantity=1,
            buyer_id="b", seller_id="s", aggressor="sell", fee=0.0,
        )
        assert msg.type == "trade_execution"

    def test_invalid_aggressor(self):
        with pytest.raises(ValidationError):
            TradeExecution(
                trade_id="t", symbol="X", price=1.0, quantity=1,
                buyer_id="b", seller_id="s", aggressor="neutral", fee=0.0,
            )


class TestBookSnapshot:
    def test_round_trip(self):
        msg = BookSnapshot(
            symbol="AAPL",
            bids=[[149.5, 100.0], [149.0, 200.0]],
            asks=[[150.0, 50.0], [150.5, 75.0]],
            mid_price=149.75,
            spread=0.50,
        )
        round_trip(msg)

    def test_type_discriminator(self):
        msg = BookSnapshot(
            symbol="X", bids=[], asks=[], mid_price=0.0, spread=0.0,
        )
        assert msg.type == "book_snapshot"

    def test_empty_book(self):
        msg = BookSnapshot(
            symbol="BTC", bids=[], asks=[], mid_price=30000.0, spread=0.0,
        )
        raw = round_trip(msg)
        assert raw["bids"] == []
        assert raw["asks"] == []

    def test_asset_type_round_trips(self):
        msg = BookSnapshot(
            symbol="ARENA10", bids=[], asks=[], mid_price=100.0, spread=0.0,
            asset_type="future",
        )
        raw = round_trip(msg)
        assert raw["asset_type"] == "future"

    def test_asset_type_defaults_to_equity(self):
        # An older venue's payload has no asset_type; the field must default
        # so brokers can keep keying their quoting universe off it.
        raw = json.loads(BookSnapshot(
            symbol="X", bids=[], asks=[], mid_price=1.0, spread=0.0,
        ).model_dump_json())
        raw.pop("asset_type", None)
        assert parse_message(raw).asset_type == "equity"


class TestIPOSubscribe:
    def test_round_trip(self):
        msg = IPOSubscribe(team_id="alpha_trader_1", symbol="ORCA",
                           quantity=250, max_price=27.5, team="Team Alpha")
        round_trip(msg)

    def test_type_discriminator(self):
        msg = IPOSubscribe(team_id="t1", symbol="ORCA", quantity=1)
        assert msg.type == "ipo_subscribe"

    def test_max_price_defaults_to_top_of_range_sentinel(self):
        msg = IPOSubscribe(team_id="t1", symbol="ORCA", quantity=10)
        assert msg.max_price == 0.0 and msg.team == ""

    def test_missing_quantity(self):
        with pytest.raises(ValidationError):
            IPOSubscribe(team_id="t1", symbol="ORCA")


class TestPortfolioUpdate:
    def test_round_trip(self):
        msg = PortfolioUpdate(
            team_id="t1",
            cash=10000.0,
            positions={"AAPL": 5, "TSLA": -2},
            realized_pnl=250.0,
            unrealized_pnl=-50.0,
            total_fees_paid=3.75,
            net_worth=10200.0,
        )
        round_trip(msg)

    def test_type_discriminator(self):
        msg = PortfolioUpdate(
            team_id="t", cash=0.0, positions={},
            realized_pnl=0.0, unrealized_pnl=0.0,
            total_fees_paid=0.0, net_worth=0.0,
        )
        assert msg.type == "portfolio_update"

    def test_empty_positions(self):
        msg = PortfolioUpdate(
            team_id="t", cash=5000.0, positions={},
            realized_pnl=0.0, unrealized_pnl=0.0,
            total_fees_paid=0.0, net_worth=5000.0,
        )
        raw = round_trip(msg)
        assert raw["positions"] == {}


class TestLeaderboard:
    def test_round_trip(self):
        msg = Leaderboard(
            traders=[{"team_id": "t1", "net_worth": 10500.0}],
            brokers=[{"team_id": "b1", "spread_income": 200.0}],
            exchange_fees=50.0,
            tick=42,
        )
        round_trip(msg)

    def test_type_discriminator(self):
        msg = Leaderboard(traders=[], brokers=[], exchange_fees=0.0, tick=0)
        assert msg.type == "leaderboard"

    def test_empty_lists(self):
        msg = Leaderboard(traders=[], brokers=[], exchange_fees=0.0, tick=1)
        raw = round_trip(msg)
        assert raw["traders"] == []
        assert raw["brokers"] == []


class TestSessionEvent:
    def test_round_trip_with_data(self):
        msg = SessionEvent(
            event="SESSION_OPEN",
            message="Trading session has started",
            data={"tick": 1, "symbols": ["AAPL", "TSLA"]},
        )
        round_trip(msg)

    def test_round_trip_no_data(self):
        msg = SessionEvent(event="SESSION_CLOSED", message="Session over")
        raw = round_trip(msg)
        assert raw["data"] == {}

    def test_type_discriminator(self):
        msg = SessionEvent(event="E", message="M")
        assert msg.type == "session_event"

    def test_default_empty_data(self):
        msg = SessionEvent(event="TEST", message="hello")
        assert msg.data == {}


class TestErrorMsg:
    def test_round_trip(self):
        msg = ErrorMsg(code="INSUFFICIENT_FUNDS", message="Not enough cash to place order")
        round_trip(msg)

    def test_type_discriminator(self):
        msg = ErrorMsg(code="E", message="m")
        assert msg.type == "error"

    def test_missing_code(self):
        with pytest.raises(ValidationError):
            ErrorMsg(message="oops")

    def test_missing_message(self):
        with pytest.raises(ValidationError):
            ErrorMsg(code="X")


# ---------------------------------------------------------------------------
# Plugin types: round-trip tests
# ---------------------------------------------------------------------------

class TestSignal:
    def test_round_trip_with_confidence(self):
        msg = Signal(
            symbol="NVDA", side="buy", quantity=10,
            price=450.0, confidence=0.85,
        )
        round_trip(msg)

    def test_round_trip_default_confidence(self):
        msg = Signal(symbol="SPY", side="sell", quantity=5, price=440.0)
        raw = round_trip(msg)
        assert raw["confidence"] == 1.0

    def test_type_discriminator(self):
        msg = Signal(symbol="X", side="buy", quantity=1, price=1.0)
        assert msg.type == "signal"

    def test_invalid_side(self):
        with pytest.raises(ValidationError):
            Signal(symbol="X", side="hold", quantity=1, price=1.0)


class TestShockResult:
    def test_round_trip(self):
        msg = ShockResult(
            prices={"AAPL": 130.0, "TSLA": 180.0},
            message="Flash crash: equity selloff",
            affected=["AAPL", "TSLA"],
        )
        round_trip(msg)

    def test_type_discriminator(self):
        msg = ShockResult(prices={}, message="ok", affected=[])
        assert msg.type == "shock_result"

    def test_empty_affected(self):
        msg = ShockResult(prices={"BTC": 28000.0}, message="m", affected=[])
        raw = round_trip(msg)
        assert raw["affected"] == []


class TestSecurityDef:
    def test_round_trip(self):
        msg = SecurityDef(
            id="AAPL", name="Apple Inc.", asset_type="equity",
            base_price=150.0, color="#A2AAAD",
        )
        round_trip(msg)

    def test_type_discriminator(self):
        msg = SecurityDef(
            id="X", name="X", asset_type="equity",
            base_price=1.0, color="#fff",
        )
        assert msg.type == "security_def"

    def test_missing_asset_type(self):
        with pytest.raises(ValidationError):
            SecurityDef(id="X", name="X", base_price=1.0, color="#fff")


# ---------------------------------------------------------------------------
# parse_message dispatch tests
# ---------------------------------------------------------------------------

class TestParseMessage:
    def _raw(self, model_instance):
        return json.loads(model_instance.model_dump_json())

    def test_dispatch_handshake(self):
        msg = Handshake(team_id="t", role="trader", level=1)
        parsed = parse_message(self._raw(msg))
        assert isinstance(parsed, Handshake)

    def test_dispatch_place_order(self):
        msg = PlaceOrder(
            team_id="t", symbol="X", side="buy",
            order_type="limit", price=1.0, quantity=1,
        )
        parsed = parse_message(self._raw(msg))
        assert isinstance(parsed, PlaceOrder)

    def test_dispatch_cancel_order(self):
        msg = CancelOrder(team_id="t", order_id="o", symbol="X")
        parsed = parse_message(self._raw(msg))
        assert isinstance(parsed, CancelOrder)

    def test_dispatch_order_ack(self):
        msg = OrderAck(
            order_id="o", team_id="t", symbol="X",
            side="buy", price=1.0, quantity=1,
        )
        parsed = parse_message(self._raw(msg))
        assert isinstance(parsed, OrderAck)

    def test_dispatch_trade_execution(self):
        msg = TradeExecution(
            trade_id="t", symbol="X", price=1.0, quantity=1,
            buyer_id="b", seller_id="s", aggressor="buy", fee=0.0,
        )
        parsed = parse_message(self._raw(msg))
        assert isinstance(parsed, TradeExecution)

    def test_dispatch_book_snapshot(self):
        msg = BookSnapshot(
            symbol="X", bids=[], asks=[], mid_price=1.0, spread=0.0,
        )
        parsed = parse_message(self._raw(msg))
        assert isinstance(parsed, BookSnapshot)

    def test_dispatch_portfolio_update(self):
        msg = PortfolioUpdate(
            team_id="t", cash=0.0, positions={},
            realized_pnl=0.0, unrealized_pnl=0.0,
            total_fees_paid=0.0, net_worth=0.0,
        )
        parsed = parse_message(self._raw(msg))
        assert isinstance(parsed, PortfolioUpdate)

    def test_dispatch_leaderboard(self):
        msg = Leaderboard(traders=[], brokers=[], exchange_fees=0.0, tick=0)
        parsed = parse_message(self._raw(msg))
        assert isinstance(parsed, Leaderboard)

    def test_dispatch_session_event(self):
        msg = SessionEvent(event="E", message="m")
        parsed = parse_message(self._raw(msg))
        assert isinstance(parsed, SessionEvent)

    def test_dispatch_error_msg(self):
        msg = ErrorMsg(code="X", message="m")
        parsed = parse_message(self._raw(msg))
        assert isinstance(parsed, ErrorMsg)

    def test_dispatch_signal(self):
        msg = Signal(symbol="X", side="buy", quantity=1, price=1.0)
        parsed = parse_message(self._raw(msg))
        assert isinstance(parsed, Signal)

    def test_dispatch_shock_result(self):
        msg = ShockResult(prices={}, message="m", affected=[])
        parsed = parse_message(self._raw(msg))
        assert isinstance(parsed, ShockResult)

    def test_dispatch_security_def(self):
        msg = SecurityDef(
            id="X", name="X", asset_type="equity",
            base_price=1.0, color="#fff",
        )
        parsed = parse_message(self._raw(msg))
        assert isinstance(parsed, SecurityDef)

    def test_dispatch_preserves_values(self):
        msg = Handshake(team_id="alpha", role="broker", level=3)
        parsed = parse_message(self._raw(msg))
        assert parsed.team_id == "alpha"
        assert parsed.role == "broker"
        assert parsed.level == 3

    def test_unknown_type_raises_key_error(self):
        with pytest.raises(KeyError, match="Unknown message type"):
            parse_message({"type": "not_a_real_type", "foo": "bar"})

    def test_missing_type_raises_key_error(self):
        with pytest.raises(KeyError, match="missing required 'type'"):
            parse_message({"team_id": "t"})

    def test_bad_payload_raises_validation_error(self):
        with pytest.raises(ValidationError):
            parse_message({"type": "handshake", "role": "hacker", "level": 1})
