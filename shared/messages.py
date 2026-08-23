"""
Pydantic v2 message schema library for AlgoArena.

All network messages between the exchange and clients must use these models.
Never send raw dicts — always call model.model_dump_json() and parse with
parse_message() on the receiving end.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, ValidationError

__all__ = [
    # Client → Exchange
    "Handshake",
    "PlaceOrder",
    "CancelOrder",
    "TeacherCommand",
    "UpgradeRequest",
    "SeatRequest",
    # Exchange → Client
    "OrderAck",
    "TradeExecution",
    "BookSnapshot",
    "PortfolioUpdate",
    "Leaderboard",
    "SessionEvent",
    "ErrorMsg",
    # Plugin types
    "Signal",
    "ShockResult",
    "SecurityDef",
    # Dispatcher
    "parse_message",
    "AnyClientMessage",
    "AnyExchangeMessage",
]


# ---------------------------------------------------------------------------
# Client → Exchange messages
# ---------------------------------------------------------------------------

class Handshake(BaseModel):
    type: Literal["handshake"] = "handshake"
    team_id: str
    role: Literal["broker", "trader", "observer", "teacher"]
    level: int
    # Team token issued at registration. Verified by the exchange when it
    # runs with AUTH_REQUIRED=true (hosted deployments). Empty for local play.
    token: str = ""


class PlaceOrder(BaseModel):
    type: Literal["place_order"] = "place_order"
    team_id: str
    symbol: str
    side: Literal["buy", "sell"]
    order_type: Literal["limit", "market", "ioc", "post_only"]
    price: float
    quantity: int


class CancelOrder(BaseModel):
    type: Literal["cancel_order"] = "cancel_order"
    team_id: str
    order_id: str
    symbol: str


class TeacherCommand(BaseModel):
    """Sent by teacher-role clients to control the session remotely.

    Recognised commands: open_session, close_session, inject_shock, set_fee_rate.
    Extra arguments go in params (e.g. {"shock_id": "flash_crash"}).
    """

    type: Literal["teacher_command"] = "teacher_command"
    command: str
    params: dict[str, Any] = Field(default_factory=dict)


class UpgradeRequest(BaseModel):
    """Buy a shop upgrade for a team (Phase 2 economy).

    Sent to the exchange by the student portal through the dashboard's
    teacher relay: the portal has already verified the student's team token,
    and the exchange re-validates everything else (purchase window, not
    already owned, sufficient cash) before debiting.

    The exchange is the only writer of the purchase, so a request that
    arrives twice cannot double-charge.
    """

    type: Literal["upgrade_request"] = "upgrade_request"
    team: str          # team NAME as it appears in the roster
    upgrade: str       # catalog key, e.g. "fee_tier"
    request_id: str = ""   # echoed back on the result, for the portal


class SeatRequest(BaseModel):
    """Hire a new bot mid-season: a trader seat, a broker desk, or a venue.

    Travels the same dashboard→exchange relay path as UpgradeRequest, for the
    same reason: the exchange is the only component that can see live cash, so
    it is the only one that may debit it, and being the single writer means a
    request that arrives twice cannot double-charge.

    `capital` is the allocation for a trader/broker seat — it is debited
    pro-rata from the team's existing bots and becomes the new bot's roster
    capital, which it receives when it first connects. An exchange seat ignores
    it and costs the fixed licence fee.
    """

    type: Literal["seat_request"] = "seat_request"
    team: str                                          # roster team NAME
    kind: Literal["trader", "broker", "exchange"]
    capital: int = 0
    bot_id: str = ""       # optional name suffix, e.g. "scalper"
    request_id: str = ""   # echoed back on the result, for the portal


# ---------------------------------------------------------------------------
# Exchange → Client messages
# ---------------------------------------------------------------------------

class OrderAck(BaseModel):
    type: Literal["order_ack"] = "order_ack"
    order_id: str
    team_id: str
    symbol: str
    side: Literal["buy", "sell"]
    price: float
    quantity: int


class TradeExecution(BaseModel):
    type: Literal["trade_execution"] = "trade_execution"
    trade_id: str
    symbol: str
    price: float
    quantity: int
    buyer_id: str
    seller_id: str
    aggressor: Literal["buy", "sell"]
    fee: float                      # fee paid by the taker (aggressor side)
    maker_rebate: float = 0.0       # rebate credited to the maker (resting side)


class BookSnapshot(BaseModel):
    type: Literal["book_snapshot"] = "book_snapshot"
    symbol: str
    bids: list[list[float]]   # [[price, qty], ...]
    asks: list[list[float]]   # [[price, qty], ...]
    mid_price: float
    spread: float
    # The venue's own mark: fair value (microprice blended with the shared
    # fundamental) plus undecayed trade impact. Unlike mid_price it exists
    # even when the book is one-sided, and it carries the fundamental, so it
    # is what a market maker should quote AROUND — quoting around the book mid
    # means quoting around your own quotes. Defaults to 0.0 for older venues.
    ref_price: float = 0.0


class PortfolioUpdate(BaseModel):
    type: Literal["portfolio_update"] = "portfolio_update"
    team_id: str
    cash: float
    positions: dict[str, int]
    realized_pnl: float
    unrealized_pnl: float
    total_fees_paid: float
    total_rebates_earned: float = 0.0
    total_carry_paid: float = 0.0     # margin interest + short borrow fees
    liquidated: bool = False
    net_worth: float


class Leaderboard(BaseModel):
    type: Literal["leaderboard"] = "leaderboard"
    traders: list[dict[str, Any]]
    brokers: list[dict[str, Any]]
    exchange_fees: float
    tick: int
    # Optional enriched fields (default-empty for backward compat)
    participants: list[dict[str, Any]] = Field(default_factory=list)
    connected_clients: list[str] = Field(default_factory=list)
    # Season section: risk-adjusted standings + per-team equity curves.
    # Empty outside a season (see exchange/scoring.build_season_block).
    season: dict[str, Any] = Field(default_factory=dict)


class SessionEvent(BaseModel):
    type: Literal["session_event"] = "session_event"
    event: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class ErrorMsg(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str


# ---------------------------------------------------------------------------
# Plugin-related types (not transmitted over the wire — used inside the engine)
# ---------------------------------------------------------------------------

class Signal(BaseModel):
    type: Literal["signal"] = "signal"
    symbol: str
    side: Literal["buy", "sell"]
    quantity: int
    price: float
    confidence: float = 1.0


class ShockResult(BaseModel):
    type: Literal["shock_result"] = "shock_result"
    prices: dict[str, float]
    message: str
    affected: list[str]


class SecurityDef(BaseModel):
    type: Literal["security_def"] = "security_def"
    id: str
    name: str
    asset_type: str
    base_price: float
    color: str


# ---------------------------------------------------------------------------
# Union types
# ---------------------------------------------------------------------------

AnyClientMessage = Annotated[
    Union[Handshake, PlaceOrder, CancelOrder, TeacherCommand, UpgradeRequest,
          SeatRequest],
    Field(discriminator="type"),
]

AnyExchangeMessage = Annotated[
    Union[
        OrderAck, TradeExecution, BookSnapshot, PortfolioUpdate,
        Leaderboard, SessionEvent, ErrorMsg,
    ],
    Field(discriminator="type"),
]

AnyPluginType = Annotated[
    Union[Signal, ShockResult, SecurityDef],
    Field(discriminator="type"),
]

# Map every known type discriminator to its model class.
_TYPE_MAP: dict[str, type[BaseModel]] = {
    "handshake": Handshake,
    "place_order": PlaceOrder,
    "cancel_order": CancelOrder,
    "teacher_command": TeacherCommand,
    "upgrade_request": UpgradeRequest,
    "seat_request": SeatRequest,
    "order_ack": OrderAck,
    "trade_execution": TradeExecution,
    "book_snapshot": BookSnapshot,
    "portfolio_update": PortfolioUpdate,
    "leaderboard": Leaderboard,
    "session_event": SessionEvent,
    "error": ErrorMsg,
    "signal": Signal,
    "shock_result": ShockResult,
    "security_def": SecurityDef,
}


def parse_message(raw: dict[str, Any]) -> BaseModel:
    """Dispatch a raw dict to the correct Pydantic model.

    Raises KeyError if the 'type' field is missing or unrecognised.
    Raises ValidationError if the payload is structurally invalid.
    """
    msg_type = raw.get("type")
    if msg_type is None:
        raise KeyError("Message is missing required 'type' field")
    model_cls = _TYPE_MAP.get(msg_type)
    if model_cls is None:
        raise KeyError(f"Unknown message type: {msg_type!r}")
    return model_cls.model_validate(raw)
