"""
exchange/server.py — AlgoArena WebSocket exchange server.

Run with:  python -m exchange.server
           (or)  python exchange/server.py

Architecture
------------
* One OrderBook per registered security.
* One Portfolio per connected team; portfolios survive reconnects.
* Three background loops: book snapshots, leaderboard, price ticks.
* Teacher controls the session from an interactive CLI on stdin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import signal
import socket
import sys
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import websockets
import websockets.exceptions
from pydantic import ValidationError

import exchange.config as config
import exchange.ipo as ipo_mod
import exchange.persistence as persistence
import exchange.scenario as scenario_mod
import exchange.scoring as scoring
import exchange.seats as seats
import exchange.upgrades as upgrades
import shared.auth as auth
from exchange.calendar import DIVIDEND, MarketCalendar
import exchange.limits as limits
from exchange.circuit_breaker import CircuitBreaker
import exchange.price_engine as price_engine
from exchange.price_engine import PriceEngine
from shared.messages import (
    BookSnapshot,
    CancelOrder,
    CommandAck,
    ErrorMsg,
    Handshake,
    Leaderboard,
    OrderAck,
    PlaceOrder,
    PortfolioUpdate,
    IPOSubscribe,
    ManualOrder,
    SeatRequest,
    SessionEvent,
    TeacherCommand,
    TradeExecution,
    UpgradeRequest,
    parse_message,
)
from shared.orderbook import OrderBook, Trade

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _new_part_stats() -> dict:
    """Empty cumulative activity record for one participant."""
    return {"trade_count": 0, "volume": 0.0, "buy_count": 0,
            "sell_count": 0, "maker_count": 0,
            "shares_bought": 0, "shares_sold": 0}


# ---------------------------------------------------------------------------
# Per-team portfolio state
# ---------------------------------------------------------------------------

@dataclass
class Portfolio:
    """Tracks cash, positions, P&L, and fees for a single team."""

    team_id: str
    role: str   # "broker" | "trader" | "observer"
    level: int
    cash: float = field(default_factory=lambda: config.INITIAL_CASH)
    positions: dict[str, int] = field(default_factory=dict)   # symbol → qty
    avg_cost: dict[str, float] = field(default_factory=dict)  # symbol → avg entry price
    realized_pnl: float = 0.0
    total_fees_paid: float = 0.0
    total_rebates_earned: float = 0.0
    total_carry_paid: float = 0.0    # margin interest + short borrow fees
    starting_cash: float = 0.0       # set on creation; maintenance baseline
    liquidated: bool = False
    # Set when the risk_shield upgrade waives a margin call: the tick until
    # which this book is exempt from the maintenance check. Not persisted —
    # a grace period is a within-session courtesy, not a season-long one.
    shield_until_tick: int = 0

    def __post_init__(self) -> None:
        if not self.starting_cash:
            self.starting_cash = self.cash

    def apply_buy(self, symbol: str, price: float, qty: int, fee: float,
                  rebate: float = 0.0) -> None:
        """Record a buy fill: debit cash, update position and average cost.

        Signed average-cost accounting: buying while SHORT is a cover and
        realises (avg_short − price) on the covered part — the old code
        skipped that, so a short round trip gained cash but booked zero
        realized P&L (attribution bug; net worth was always right).
        """
        self.cash -= price * qty + fee - rebate
        self.total_fees_paid += fee
        self.total_rebates_earned += rebate
        self._book_signed_fill(symbol, price, +qty)

    def apply_sell(self, symbol: str, price: float, qty: int, fee: float,
                   rebate: float = 0.0) -> None:
        """Record a sell fill: credit cash, realise P&L, update position.

        Selling while long realises (price − avg) on the closed part;
        selling flat/short OPENS a short at a tracked entry price, so an
        open short marks to market like any position.
        """
        self.cash += price * qty - fee + rebate
        self.total_fees_paid += fee
        self.total_rebates_earned += rebate
        self._book_signed_fill(symbol, price, -qty)

    def _book_signed_fill(self, symbol: str, price: float, signed: int) -> None:
        """Position/avg-cost/realized bookkeeping for a fill of `signed` shares.

        One rule for longs and shorts alike: same direction extends the
        position at a volume-weighted entry; opposite direction realises
        (price − avg) × closed × sign(old) on the part that closes, and a
        flip re-opens the remainder at the fill price.
        """
        old = self.positions.get(symbol, 0)
        entry = self.avg_cost.get(symbol, price)

        if old == 0 or (old > 0) == (signed > 0):
            total = abs(old) + abs(signed)
            self.avg_cost[symbol] = (
                (abs(old) * entry + abs(signed) * price) / total
                if total else price)
        else:
            closed = min(abs(old), abs(signed))
            direction = 1 if old > 0 else -1
            self.realized_pnl += (price - entry) * closed * direction
            if abs(signed) > abs(old):
                self.avg_cost[symbol] = price      # flipped through zero

        self.positions[symbol] = old + signed
        if self.positions[symbol] == 0:
            self.avg_cost.pop(symbol, None)

    # -- Futures (cash-settled; see exchange/config.FUTURES) ----------

    def apply_futures_fill(self, symbol: str, price: float, qty: int,
                           side: str, fee: float, rebate: float = 0.0) -> None:
        """Record a futures fill.

        No notional changes hands — a future is a promise, not a purchase. Only
        fees move cash; the position and its entry price are what matter, and
        P&L reaches cash through settle_future().
        """
        signed = qty if side == "buy" else -qty
        old = self.positions.get(symbol, 0)
        entry = self.avg_cost.get(symbol, price)

        self.cash += rebate - fee
        self.total_fees_paid += fee
        self.total_rebates_earned += rebate

        if old == 0 or (old > 0) == (signed > 0):
            # Opening or adding: volume-weighted entry price.
            total = abs(old) + qty
            self.avg_cost[symbol] = (
                (abs(old) * entry + qty * price) / total if total else price)
        else:
            # Reducing or flipping: realise P&L on the part that closes.
            closed = min(abs(old), qty)
            direction = 1 if old > 0 else -1
            self.realized_pnl += (price - entry) * closed * direction
            if qty > abs(old):
                self.avg_cost[symbol] = price      # flipped to the other side

        self.positions[symbol] = old + signed
        if self.positions[symbol] == 0:
            self.avg_cost.pop(symbol, None)

    def settle_future(self, symbol: str, mark: float) -> float:
        """Pay or collect variation margin against `mark`. Returns the amount.

        This is the mechanic that makes a future cash-settled: the open gain or
        loss becomes real money and the entry price is reset to the mark, so
        nothing is left unsettled.
        """
        qty = self.positions.get(symbol, 0)
        if not qty:
            return 0.0
        entry = self.avg_cost.get(symbol, mark)
        variation = (mark - entry) * qty
        self.cash += variation
        self.realized_pnl += variation
        self.avg_cost[symbol] = mark
        return variation

    def futures_margin(self) -> float:
        """Total initial margin tied up by open futures positions."""
        return sum(
            abs(qty) * config.FUTURES_MARGIN_PER_CONTRACT
            for sym, qty in self.positions.items()
            if qty and config.is_future(sym)
        )

    def unrealized_pnl(self, ref_prices: dict[str, float]) -> float:
        total = 0.0
        for sym, qty in self.positions.items():
            if qty:
                ref = ref_prices.get(sym, self.avg_cost.get(sym, 0.0))
                total += (ref - self.avg_cost.get(sym, ref)) * qty
        return total

    def net_worth(self, ref_prices: dict[str, float],
                  bidask: dict[str, tuple] | None = None) -> float:
        """Mark-to-market net worth.

        With `bidask` (symbol → (best_bid, best_ask)), marks conservatively:
        longs at the bid, shorts at the ask — the way real risk systems mark.
        """
        mv = 0.0
        for sym, qty in self.positions.items():
            ref = ref_prices.get(sym, self.avg_cost.get(sym, 0.0))
            if bidask and sym in bidask:
                bid, ask = bidask[sym]
                if qty > 0 and bid:
                    ref = bid
                elif qty < 0 and ask:
                    ref = ask
            if config.is_future(sym):
                # A future is not owned inventory: only the unsettled
                # variation since the last mark is worth anything. Counting
                # qty × price would credit a notional nobody paid for.
                mv += (ref - self.avg_cost.get(sym, ref)) * qty
            else:
                mv += qty * ref
        return self.cash + mv

    def to_message(self, ref_prices: dict[str, float]) -> PortfolioUpdate:
        live = {sym: qty for sym, qty in self.positions.items() if qty}
        return PortfolioUpdate(
            team_id=self.team_id,
            cash=round(self.cash, 4),
            positions=live,
            realized_pnl=round(self.realized_pnl, 4),
            unrealized_pnl=round(self.unrealized_pnl(ref_prices), 4),
            total_fees_paid=round(self.total_fees_paid, 4),
            total_rebates_earned=round(self.total_rebates_earned, 4),
            total_carry_paid=round(self.total_carry_paid, 4),
            liquidated=self.liquidated,
            net_worth=round(self.net_worth(ref_prices), 4),
        )


# ---------------------------------------------------------------------------
# Exchange server
# ---------------------------------------------------------------------------

class ExchangeServer:
    """Central WebSocket exchange: matches orders, tracks portfolios, runs the game."""

    def __init__(self) -> None:
        # Eagerly load default securities and shocks onto the global arena.
        import plugins.securities.defaults  # noqa: F401  (registers on import)
        import plugins.securities.futures   # noqa: F401  (ARENA-10, week 9)
        import plugins.shocks.defaults      # noqa: F401
        from plugins import arena
        self.registry = arena

        # This venue's own fee schedule: env var → roster ("fees" on the team
        # whose exchange_port is ours) → default. Re-resolved on demand
        # afterwards, so a live edit needs no restart (config.refresh_venue_fees).
        config.refresh_venue_fees(force=True)

        # One matching engine per listed security.
        self.books: dict[str, OrderBook] = {
            s.id: OrderBook(s.id, fee_rate=config.FEE_RATE,
                            tick_size=config.TICK_SIZE,
                            stp_key=self._stp_key)
            for s in self.registry.list_securities()
        }

        # team_id → Portfolio (survives reconnects within a session)
        self.portfolios: dict[str, Portfolio] = {}

        # Active WebSocket connections (team_id ↔ websocket)
        self.clients: dict[str, Any] = {}
        self.ws_to_team: dict[Any, str] = {}

        # Reference prices used for P&L marking — kept in sync by PriceEngine.
        self.ref_prices: dict[str, float] = dict(self.registry.prices)

        # One PriceEngine per symbol — drives market price discovery.
        self.price_engines: dict[str, PriceEngine] = {
            sym: PriceEngine(
                sym,
                self.registry.securities[sym]["defn"].base_price,
            )
            for sym in self.books
        }
        # One CircuitBreaker watching all symbols.
        self.circuit_breaker = CircuitBreaker(list(self.books.keys()))

        self.session_open: bool = False
        # The tick at which the current session opened. Scenario event ticks
        # (calendar, IPOs) are RELATIVE to this: a persisted season restores
        # tick 6000+, and absolute comparisons made every scheduled event
        # fire instantly at the open.
        self.session_open_tick: int = 0
        # Venue-held stop orders: symbol → order_id → entry. Armed, not in
        # the book; they fire on trade prints or the venue mark crossing
        # stop_price, then re-enter as market/limit orders. Day orders —
        # cleared at session close.
        self.stop_orders: dict[str, dict[str, dict]] = {}
        # Symbols under the short-sale rule (Rule 201): tripped at
        # SSR_TRIGGER_PCT below the session open, sticky until the next open.
        self.ssr_active: set[str] = set()
        # Auction lifecycle: "preopen" while the opening book builds, else
        # None. _session_granted keeps the share grant once-per-session
        # across the two open paths (pre-open grants early).
        self.auction_phase: str | None = None
        self.auction_ticks_left: int = 0
        self._session_granted: bool = False
        self._pending_close_persist: bool = True
        self._last_reject_relay: dict[str, float] = {}
        # Start-of-day snapshot (taken at open, after the grant) and the
        # ledger of shares granted MID-session (late joiners, ticket bots).
        # Together they close the reconciliation identities at EOD.
        self.sod: dict[str, dict] = {}
        self.sod_market_shares: int = 0
        self.midsession_grants: dict[str, int] = {}
        # Money-flow counters for the cash-conservation identity (audit R3):
        # every dollar entering or leaving the participants' pool, by source.
        # Market-on-close orders: held like stops, injected into the
        # closing auction book when the pre-close window opens.
        self.moc_orders: dict[str, dict[str, dict]] = {}
        # Live IPO offerings this session: symbol → IPOOffering.
        self.ipos: dict[str, ipo_mod.IPOOffering] = {}
        self.ipo_issued: dict[str, int] = {}   # shares created by listings
        self.ipo_proceeds: float = 0.0         # cash paid to the issuer
        # Season-persistent primary-market state. `listed_ipos` carries every
        # security this venue ever listed (symbol → name/offer/last price) so
        # a restart re-registers it and positions stay tradeable; it doubles
        # as the durable done-once guard — the in-memory `symbol in books`
        # check dies with the process, and re-running a week must never
        # re-mint a deal on top of live holdings.
        self.listed_ipos: dict[str, dict] = {}
        # Cumulative per-bot primary-market allocations (shares), for the
        # gradebook: who played the IPO weeks and how hard.
        self.ipo_allocations: dict[str, int] = {}
        self.dividends_net: float = 0.0      # signed: shorts PAY dividends
        self.interest_credited: float = 0.0  # idle-cash interest minted
        self.seat_debits: float = 0.0        # hire capital in flight
        self.tick: int = 0
        # Recent fills, bounded so a multi-hour session cannot exhaust memory.
        # Cumulative counts live in trade_count / part_stats (never trimmed).
        self.trade_log: deque[Trade] = deque(maxlen=config.TRADE_LOG_MAXLEN)
        self.trade_count: int = 0
        # Cumulative per-participant activity, updated incrementally on every
        # fill so _build_leaderboard() stays O(teams) instead of O(trades).
        self.part_stats: dict[str, dict] = defaultdict(_new_part_stats)
        # symbol → last good internal reference (microprice). Held across a
        # one-sided book so the fair value never teleports; see
        # _internal_reference.
        self._internal_ref: dict[str, float] = {}
        # Exchange revenue: taker fees collected minus maker rebates paid out.
        self.exchange_revenue: float = 0.0
        # Session recorder (JSONL file handle, active between open/close).
        self._recorder: Any = None

        # team_ids that authenticated as role="teacher"; allowed to send TeacherCommand.
        self.teacher_clients: set[str] = set()

        # Per-team message quota buckets (order + cancel traffic).
        self.quotas: dict[str, limits.TokenBucket] = {}

        # Strong references to fire-and-forget tasks (delayed sends, halt
        # timers). asyncio keeps only weak references, so without this a
        # pending task can be garbage-collected before it runs.
        self._bg_tasks: set[asyncio.Task] = set()

        # ── Season state ──────────────────────────────────────────────
        # The active week's rule set. With no SCENARIO_PATH / GAME_WEEK this
        # is OPEN_PLAY: every gate permissive, no config override, so local
        # play behaves exactly as it did before the season system existed.
        self.scenario = scenario_mod.active_scenario()
        self.scenario.apply()
        # The week's event calendar, plus the ramp scheduler that both
        # calendar events and teacher shocks apply their moves through.
        self.calendar = MarketCalendar(self.scenario.events)
        # team_id → [(tick, net_worth), ...] — the season scoring input.
        self.equity_history: dict[str, list[tuple[int, float]]] = {}
        self.sessions_played: int = 0
        self._last_season_save: float = 0.0
        self._last_equity_snapshot_tick: int = 0
        # Cached season standings — see _season_block().
        self._season_cache: dict | None = None
        self._season_dirty: bool = True
        self.load_season()

    # ------------------------------------------------------------------
    # Connection handler (one coroutine per connected client)
    # ------------------------------------------------------------------

    def _authenticate(self, msg: Handshake) -> bool:
        """Verify a handshake token against the token store.

        Teachers need the teacher token. Brokers/traders need their team's
        token (bot id is mapped to its team via the roster). Observers are
        admitted with any valid token (member market-data access).
        """
        if msg.role == "teacher":
            return auth.verify_teacher(msg.token)
        if msg.role == "observer":
            return auth.verify_any(msg.token)
        team = auth.team_for_bot(msg.team_id, config.ROSTER_PATH)
        if team is None:
            return False        # unregistered bot id
        return auth.verify(team, msg.token)

    async def handle_client(self, websocket: Any) -> None:
        """Manage a single client from handshake through disconnect."""
        team_id: str | None = None
        try:
            # ── Handshake (15 s timeout) ──────────────────────────────
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=15.0)
            except asyncio.TimeoutError:
                await self._send(websocket, ErrorMsg(
                    code="TIMEOUT",
                    message="No Handshake received within 15 seconds — disconnecting",
                ))
                return

            try:
                msg = parse_message(json.loads(raw))
            except Exception as exc:
                await self._send(websocket, ErrorMsg(code="PARSE_ERROR", message=str(exc)))
                return

            if not isinstance(msg, Handshake):
                await self._send(websocket, ErrorMsg(
                    code="EXPECTED_HANDSHAKE",
                    message="First message must be a Handshake",
                ))
                return

            # ── Authentication (hosted deployments: AUTH_REQUIRED=true) ──
            if config.AUTH_REQUIRED and not self._authenticate(msg):
                await self._send(websocket, ErrorMsg(
                    code="AUTH_FAILED",
                    message=("Invalid or missing token for "
                             f"{msg.team_id!r} — register your team first "
                             "(make register) and set ARENA_TOKEN"),
                ))
                logger.warning("Auth failed: %s (role=%s)", msg.team_id, msg.role)
                return

            team_id = msg.team_id
            if team_id in self.clients:
                logger.info("Reconnect: %s", team_id)
            self.clients[team_id] = websocket
            self.ws_to_team[websocket] = team_id

            new_portfolio = False
            if msg.role == "teacher":
                self.teacher_clients.add(team_id)
                logger.info("Teacher connected: %s", team_id)
            elif team_id not in self.portfolios:
                new_portfolio = True
                self.portfolios[team_id] = Portfolio(
                    team_id=team_id, role=msg.role, level=msg.level,
                    cash=config.funded_cash_for(team_id),
                )
                logger.info(
                    "Joined: %-20s  role=%-8s  level=%d",
                    team_id, msg.role, msg.level,
                )

            # Send current book state so client can build a local picture.
            for symbol, book in self.books.items():
                await self._send(websocket, self._make_snapshot(symbol, book))

            # Send current leaderboard so dashboards see connected teams immediately.
            await self._send(websocket, self._build_leaderboard())

            # Late joiners: bots gate all trading on the SESSION_OPEN event,
            # which is only broadcast at the moment the teacher opens. A bot
            # that connects (or crash-reconnects) afterwards would otherwise
            # sit dormant for the whole session.
            if self.auction_phase == "preopen":
                await self._send(websocket, SessionEvent(
                    event="SESSION_PREOPEN",
                    message="Pre-open in progress — limit orders only.",
                    data={"symbols": list(self.books.keys()),
                          "ticks": self.auction_ticks_left,
                          "late_join": True},
                ))
            if self.session_open:
                if new_portfolio and msg.role != "observer":
                    # First appearance mid-session: give the same starting
                    # grant everyone else got at the open. Reconnects reuse
                    # the existing portfolio, so this can never double-grant.
                    n = config.STARTING_SHARES_PER_SYMBOL
                    portfolio = self.portfolios[team_id]
                    if n > 0:
                        for sym in self.books:
                            if config.is_future(sym):
                                continue
                            portfolio.positions[sym] = (
                                portfolio.positions.get(sym, 0) + n)
                            if sym not in portfolio.avg_cost:
                                portfolio.avg_cost[sym] = self.ref_prices.get(sym, 0.0)
                            self.midsession_grants[sym] = (
                                self.midsession_grants.get(sym, 0) + n)
                        await self._send(
                            websocket, portfolio.to_message(self.ref_prices))
                await self._send(websocket, SessionEvent(
                    event="SESSION_OPEN",
                    message="Session already open — you joined mid-session.",
                    data={"symbols": list(self.books.keys()),
                          "tick": self.tick,
                          "starting_shares": config.STARTING_SHARES_PER_SYMBOL,
                          "week": self.scenario.week,
                          "scenario": self.scenario.to_dict(),
                          "late_join": True},
                ))
                # Replay live primary-market state: on_ipo fires off the
                # IPO_OPEN broadcast, so without this a bot that connects
                # (or crash-reconnects) during the window is silently
                # excluded from the deal.
                for deal in self.ipos.values():
                    if deal.state == "announced":
                        await self._send(websocket, SessionEvent(
                            event="IPO_ANNOUNCE",
                            message=(f"IPO: {deal.name} ({deal.symbol}) — "
                                     f"book opens t{deal.open_tick}, "
                                     f"lists t{deal.list_tick}"),
                            data=dict(deal.to_public(), late_join=True),
                        ))
                    elif deal.state == "open":
                        await self._send(websocket, SessionEvent(
                            event="IPO_OPEN",
                            message=(f"{deal.symbol} book is OPEN — "
                                     f"subscribe before t{deal.close_tick}"),
                            data=dict(deal.to_public(), late_join=True),
                        ))

            # ── Message loop ─────────────────────────────────────────
            async for raw_msg in websocket:
                try:
                    incoming = parse_message(json.loads(raw_msg))
                except (json.JSONDecodeError, KeyError, ValidationError) as exc:
                    await self._send(websocket, ErrorMsg(
                        code="PARSE_ERROR", message=str(exc)
                    ))
                    continue

                if isinstance(incoming, PlaceOrder):
                    await self._handle_place_order(websocket, incoming, team_id)
                elif isinstance(incoming, CancelOrder):
                    await self._handle_cancel_order(websocket, incoming, team_id)
                elif isinstance(incoming, TeacherCommand):
                    await self._handle_teacher_command(websocket, incoming, team_id)
                elif isinstance(incoming, UpgradeRequest):
                    await self._handle_upgrade_request(websocket, incoming, team_id)
                elif isinstance(incoming, SeatRequest):
                    await self._handle_seat_request(websocket, incoming, team_id)
                elif isinstance(incoming, ManualOrder):
                    await self._handle_manual_order(websocket, incoming, team_id)
                elif isinstance(incoming, IPOSubscribe):
                    await self._handle_ipo_subscribe(websocket, incoming, team_id)
                else:
                    await self._send(websocket, ErrorMsg(
                        code="UNEXPECTED_MESSAGE",
                        message=f"Unexpected message type: {type(incoming).__name__}",
                    ))

        except websockets.exceptions.ConnectionClosed:
            pass  # expected on normal disconnects
        except Exception:
            logger.exception("Unhandled error for client %s", team_id or "unknown")
        finally:
            if team_id:
                self.clients.pop(team_id, None)
                self.teacher_clients.discard(team_id)
            self.ws_to_team.pop(websocket, None)
            if team_id:
                logger.info("Disconnected: %s", team_id)

    # ------------------------------------------------------------------
    # Order handling
    # ------------------------------------------------------------------

    async def _handle_place_order(
        self, ws: Any, msg: PlaceOrder, team_id: str
    ) -> None:
        """Validate and route a PlaceOrder to the matching engine."""

        # ── Guard rails ──────────────────────────────────────────────
        if not self.session_open:
            if self.auction_phase in ("preopen", "preclose"):
                # Auction windows build a book: resting orders only.
                if msg.order_type not in ("limit", "post_only"):
                    await self._reject(ws, team_id, ErrorMsg(
                        code="AUCTION_ONLY_LIMIT",
                        message=("Pre-open: only limit orders may be entered "
                                 "into the opening auction"),
                    ))
                    return
            else:
                await self._reject(ws, team_id, ErrorMsg(
                    code="SESSION_CLOSED",
                    message="Session not open — wait for SESSION_OPEN event",
                ))
                return
        elif (self.auction_phase == "preclose"
                and msg.order_type not in ("limit", "post_only")):
            # The session is still open during the pre-close, but matching
            # is frozen: marketable orders would rest at a nonsense price.
            await self._reject(ws, team_id, ErrorMsg(
                code="AUCTION_ONLY_LIMIT",
                message=("Closing auction: only limit orders may be entered "
                         "into the closing cross"),
            ))
            return

        if msg.team_id != team_id:
            await self._reject(ws, team_id, ErrorMsg(
                code="TEAM_MISMATCH",
                message="team_id in message does not match your authenticated identity",
            ))
            return

        if msg.symbol not in self.books:
            await self._reject(ws, team_id, ErrorMsg(
                code="UNKNOWN_SYMBOL",
                message=f"{msg.symbol!r} is not listed on this exchange",
            ))
            return

        if self.circuit_breaker.is_halted(msg.symbol):
            remaining = self.circuit_breaker.get_resume_time(msg.symbol)
            await self._reject(ws, team_id, ErrorMsg(
                code="SYMBOL_HALTED",
                message=(
                    f"{msg.symbol} trading is halted. "
                    f"Resume in {remaining:.0f}s"
                ),
            ))
            return

        if not (1 <= msg.quantity <= config.MAX_ORDER_SIZE):
            await self._reject(ws, team_id, ErrorMsg(
                code="INVALID_QUANTITY",
                message=f"Quantity must be 1 – {config.MAX_ORDER_SIZE}",
            ))
            return

        portfolio = self.portfolios.get(team_id)
        if portfolio is None:
            await self._reject(ws, team_id, ErrorMsg(code="NO_PORTFOLIO", message="Portfolio not found"))
            return

        if portfolio.role == "observer":
            await self._reject(ws, team_id, ErrorMsg(
                code="FORBIDDEN", message="Observers cannot place orders",
            ))
            return

        # ── Week gates (season scenario) ──────────────────────────────
        # Each week unlocks mechanics. Outside a season these are all
        # permissive, so nothing changes for local play.
        if config.is_future(msg.symbol) and not self.scenario.flag(
                "futures_enabled"):
            await self._reject(ws, team_id, ErrorMsg(
                code="FUTURES_LOCKED",
                message=(f"{msg.symbol} is not tradeable yet — the index "
                         f"future lists in week 9 (currently week "
                         f"{self.scenario.week})"),
            ))
            return

        if msg.order_type == "post_only" and not self.scenario.flag(
                "post_only_allowed"):
            await self._reject(ws, team_id, ErrorMsg(
                code="ORDER_TYPE_LOCKED",
                message=(f"Post-only orders unlock in a later week "
                         f"(week {self.scenario.week}: "
                         f"{self.scenario.label}) — use a limit order"),
            ))
            return

        # ── Message quota ─────────────────────────────────────────────
        # Real venues meter order traffic. Over quota the order is refused,
        # which is what makes quote lifetime a decision rather than a freebie.
        if not self._take_quota(team_id):
            await self._reject(ws, team_id, ErrorMsg(
                code="RATE_LIMITED",
                message=(
                    f"Message quota exceeded — you may send "
                    f"{self.quota_for(team_id):.0f} order/cancel messages per "
                    f"tick. Slow your requoting or buy a quota increase."
                ),
            ))
            return

        # ── Pre-trade risk checks ─────────────────────────────────────
        if portfolio.liquidated:
            await self._reject(ws, team_id, ErrorMsg(
                code="LIQUIDATED",
                message="Your account was liquidated — no further trading this session",
            ))
            return

        cur = portfolio.positions.get(msg.symbol, 0)
        worst_pos = cur + msg.quantity if msg.side == "buy" else cur - msg.quantity

        # Short-selling gate: until the week that unlocks it, a sell may only
        # reduce an existing long.
        if (msg.side == "sell" and worst_pos < 0
                and not self.scenario.flag("shorts_allowed")):
            await self._reject(ws, team_id, ErrorMsg(
                code="SHORTS_LOCKED",
                message=(
                    f"Short selling is not unlocked yet (week "
                    f"{self.scenario.week}: {self.scenario.label}). You hold "
                    f"{cur} {msg.symbol} — sell at most that many."
                ),
            ))
            return

        # Per-symbol position limit (worst case: full fill of this order).
        limit = self.position_limit_for(team_id)
        if limit > 0:
            if abs(worst_pos) > limit:
                await self._reject(ws, team_id, ErrorMsg(
                    code="POSITION_LIMIT",
                    message=(
                        f"Order would take {msg.symbol} position to {worst_pos:+d} "
                        f"(limit ±{limit})"
                    ),
                ))
                return

        if config.is_future(msg.symbol):
            # Futures post margin, not notional, and the requirement is
            # symmetric: a short contract ties up exactly as much as a long.
            free_cash = portfolio.cash - portfolio.futures_margin()
            needed = abs(worst_pos) * config.FUTURES_MARGIN_PER_CONTRACT \
                - abs(cur) * config.FUTURES_MARGIN_PER_CONTRACT
            if needed > 0 and free_cash < needed:
                await self._reject(ws, team_id, ErrorMsg(
                    code="INSUFFICIENT_MARGIN",
                    message=(
                        f"{msg.symbol} needs ${needed:,.2f} additional margin "
                        f"(${config.FUTURES_MARGIN_PER_CONTRACT:,.2f} per "
                        f"contract); you have ${free_cash:,.2f} free"
                    ),
                ))
                return
        elif msg.side == "buy":
            ref = (
                msg.price if msg.order_type != "market"
                else self.ref_prices.get(msg.symbol, msg.price)
            )
            worst_fee = (config.config_for_team(team_id, "taker_fee")
                         if config.MAKER_TAKER_ENABLED
                         else config.FEE_RATE / 2)
            est_cost = ref * msg.quantity * (1.0 + worst_fee)

            # Buying power: brokers may finance inventory on margin.
            buying_power = portfolio.cash
            if config.MARGIN_ENABLED and portfolio.role == "broker":
                inv_mv = sum(
                    q * self.ref_prices.get(s, 0.0)
                    for s, q in portfolio.positions.items()
                    if q > 0 and not config.is_future(s)
                )
                buying_power += (
                    config.config_for_team(team_id, "margin_haircut") * inv_mv
                )

            if buying_power < est_cost:
                await self._reject(ws, team_id, ErrorMsg(
                    code="INSUFFICIENT_CASH",
                    message=(
                        f"Estimated cost ${est_cost:,.2f}, "
                        f"buying power ${buying_power:,.2f}"
                    ),
                ))
                return
        # Sell orders may create short positions (market makers must be able
        # to post ask quotes before receiving fills). Shorts pay a per-tick
        # borrow fee and count against the position limit above — plus two
        # real-market brakes: the short-sale rule and borrow availability.
        if (msg.side == "sell" and not config.is_future(msg.symbol)):
            pos = portfolio.positions.get(msg.symbol, 0)
            new_short = max(0, msg.quantity - max(pos, 0))
            if new_short > 0:
                # SSR (Rule 201): once a symbol is down SSR_TRIGGER_PCT from
                # the open, shorts may only ADD liquidity above the bid —
                # no hitting bids on the way down.
                if msg.symbol in self.ssr_active:
                    best_bid = self.books[msg.symbol].best_bid()
                    passive = (msg.order_type in ("limit", "post_only")
                               and (best_bid is None or msg.price > best_bid))
                    if not passive:
                        await self._reject(ws, team_id, ErrorMsg(
                            code="SSR_RESTRICTED",
                            message=(f"{msg.symbol} is under the short-sale "
                                     f"rule (down {config.SSR_TRIGGER_PCT:.0%}"
                                     f" from open) — shorts must be limit "
                                     f"orders priced above the best bid"),
                        ))
                        return
                # Borrow locate: total short interest per symbol is capped.
                # A locate_desk upgrade adds private borrow beyond the pool.
                cap = config.SHORT_LOCATE_CAP
                if cap > 0:
                    cap += config.config_for_team(team_id, "locate_extra")
                    outstanding = sum(
                        max(0, -p.positions.get(msg.symbol, 0))
                        for p in self.portfolios.values())
                    if outstanding + new_short > cap:
                        await self._reject(ws, team_id, ErrorMsg(
                            code="BORROW_UNAVAILABLE",
                            message=(f"No borrow: {outstanding} of {cap} "
                                     f"{msg.symbol} shares already short "
                                     f"across the market"),
                        ))
                        return

        # ── LULD price band (erroneous-order collar) ──────────────────
        # A limit price too far from the venue mark is rejected at entry:
        # the halt layers react AFTER a bad print; the band prevents it.
        # Market orders carry no price, and a stop's limit is checked when
        # it fires (it re-enters here as a plain limit).
        if (config.LULD_BAND_PCT > 0
                and msg.order_type in ("limit", "ioc", "post_only")):
            ref = self.ref_prices.get(msg.symbol)
            if ref and ref > 0:
                lo = ref * (1 - config.LULD_BAND_PCT)
                hi = ref * (1 + config.LULD_BAND_PCT)
                if not (lo <= msg.price <= hi):
                    await self._reject(ws, team_id, ErrorMsg(
                        code="PRICE_BAND",
                        message=(f"{msg.symbol} {msg.side} at {msg.price} is "
                                 f"outside the LULD band "
                                 f"[{lo:.2f}, {hi:.2f}] around the mark "
                                 f"{ref:.2f} — rejected"),
                    ))
                    return

        # ── Market-on-close: held for the closing auction ─────────────
        if msg.order_type == "moc":
            if not config.CLOSING_AUCTION:
                await self._reject(ws, team_id, ErrorMsg(
                    code="MOC_UNAVAILABLE",
                    message=("This venue runs no closing auction — "
                             "market-on-close orders cannot execute"),
                ))
                return
            oid = f"moc-{uuid.uuid4()}"
            self.moc_orders.setdefault(msg.symbol, {})[oid] = {
                "order_id": oid, "team_id": team_id, "symbol": msg.symbol,
                "side": msg.side, "quantity": int(msg.quantity),
            }
            await self._send(ws, OrderAck(
                order_id=oid, team_id=team_id, symbol=msg.symbol,
                side=msg.side, price=0.0, quantity=msg.quantity,
            ))
            return

        # ── Stop orders: armed at the venue, not matched now ──────────
        if msg.order_type in ("stop", "stop_limit"):
            if not msg.stop_price or msg.stop_price <= 0:
                await self._reject(ws, team_id, ErrorMsg(
                    code="STOP_PRICE_REQUIRED",
                    message="stop/stop_limit orders need stop_price > 0",
                ))
                return
            oid = f"stop-{uuid.uuid4()}"
            self.stop_orders.setdefault(msg.symbol, {})[oid] = {
                "order_id": oid, "team_id": team_id, "symbol": msg.symbol,
                "side": msg.side, "stop_price": float(msg.stop_price),
                "price": float(msg.price), "quantity": int(msg.quantity),
                "order_type": msg.order_type,
            }
            await self._send(ws, OrderAck(
                order_id=oid, team_id=team_id, symbol=msg.symbol,
                side=msg.side, price=float(msg.stop_price),
                quantity=msg.quantity,
            ))
            return

        # ── Match ────────────────────────────────────────────────────
        book = self.books[msg.symbol]
        order, trades = book.place_order(
            team_id=team_id,
            side=msg.side,
            price=msg.price,
            quantity=msg.quantity,
            order_type=msg.order_type,
        )

        if order.rejected:
            await self._send(ws, ErrorMsg(
                code="POST_ONLY_CROSS",
                message=(f"Post-only {msg.side} at {order.price} would cross "
                         f"the book — rejected to protect your maker rebate"),
            ))
            return

        # Self-trade prevention: tell the owner its resting order was
        # cancelled rather than matched against its own incoming order.
        for cancelled in book.stp_cancels:
            owner_ws = self.clients.get(cancelled.team_id)
            if owner_ws:
                await self._send(owner_ws, ErrorMsg(
                    code="STP_CANCEL",
                    message=(f"Your resting {cancelled.side} "
                             f"{cancelled.remaining} {cancelled.symbol} @ "
                             f"{cancelled.price} was cancelled by self-trade "
                             f"prevention (your own order crossed it)"),
                ))

        await self._send(ws, OrderAck(
            order_id=order.order_id,
            team_id=team_id,
            symbol=msg.symbol,
            side=msg.side,
            price=order.price,
            quantity=order.quantity,
        ))

        for trade in trades:
            await self._process_trade(trade)
        if trades:
            await self._check_stop_triggers(msg.symbol, trades[-1].price)

    @staticmethod
    def _stp_key(team_id: str) -> str:
        """STP scope resolver: the bot itself, or its whole roster team."""
        if config.STP_SCOPE == "team":
            return config.team_of(team_id) or team_id
        return team_id

    async def _reject(self, ws, team_id: str, err) -> None:
        """Send a rejection to the bot AND surface it to the teacher relay.

        A student whose orders are silently dying in their bot's console
        burns lab time not knowing they are being throttled/banded/halted.
        The relay copy (throttled to one per bot per second — a rate-limited
        bot would otherwise flood it) reaches the dashboard's market log and
        the team's portal event feed.
        """
        await self._send(ws, err)
        now = time.time()
        if now - self._last_reject_relay.get(team_id, 0.0) < 1.0:
            return
        self._last_reject_relay[team_id] = now
        for teacher in self.teacher_clients:
            tws = self.clients.get(teacher)
            if tws:
                await self._send(tws, SessionEvent(
                    event="ORDER_REJECT",
                    message=f"{team_id}: [{err.code}] {err.message}",
                    data={"bot": team_id, "code": err.code,
                          "detail": err.message},
                ))

    async def _check_stop_triggers(self, symbol: str, last_price: float) -> None:
        """Fire armed stops crossed by a print or the venue mark.

        Fired entries are removed BEFORE re-injection, so the recursion a
        triggered stop causes (its own fills re-enter here) terminates.
        """
        pending = self.stop_orders.get(symbol)
        if not pending:
            return
        fired = [e for e in pending.values()
                 if (e["side"] == "buy" and last_price >= e["stop_price"])
                 or (e["side"] == "sell" and last_price <= e["stop_price"])]
        for entry in fired:
            pending.pop(entry["order_id"], None)
            owner_ws = self.clients.get(entry["team_id"])
            if owner_ws:
                await self._send(owner_ws, SessionEvent(
                    event="STOP_TRIGGERED",
                    message=(f"Stop {entry['side']} {entry['quantity']} "
                             f"{symbol} armed at {entry['stop_price']} "
                             f"triggered at {last_price:.2f}"),
                    data={"order_id": entry["order_id"], "symbol": symbol,
                          "trigger_price": last_price},
                ))
            await self._handle_place_order(owner_ws, PlaceOrder(
                team_id=entry["team_id"], symbol=symbol, side=entry["side"],
                order_type=("market" if entry["order_type"] == "stop"
                            else "limit"),
                price=entry["price"], quantity=entry["quantity"],
            ), entry["team_id"])

    async def _handle_cancel_order(
        self, ws: Any, msg: CancelOrder, team_id: str
    ) -> None:
        """Cancel a resting limit order. Sends ErrorMsg if not found."""
        if msg.team_id != team_id:
            await self._send(ws, ErrorMsg(
                code="TEAM_MISMATCH",
                message="team_id does not match your authenticated identity",
            ))
            return

        book = self.books.get(msg.symbol)
        if book is None:
            await self._send(ws, ErrorMsg(
                code="UNKNOWN_SYMBOL",
                message=f"{msg.symbol!r} is not listed",
            ))
            return

        # Cancels consume quota too — cancel/replace spam is the thing being
        # metered. Charged on receipt, whether or not the order turns out to
        # exist: a venue meters messages, and billing only successful cancels
        # would let a bot probe the book with junk order ids for free.
        if not self._take_quota(team_id):
            await self._send(ws, ErrorMsg(
                code="RATE_LIMITED",
                message=(
                    f"Message quota exceeded — you may send "
                    f"{self.quota_for(team_id):.0f} order/cancel messages per tick"
                ),
            ))
            return

        # Held MOCs are cancellable until the pre-close injects them.
        moc = self.moc_orders.get(msg.symbol, {})
        entry_moc = moc.get(msg.order_id)
        if entry_moc is not None and entry_moc["team_id"] == team_id:
            del moc[msg.order_id]
            return

        # Armed stops live at the venue, not in the book — check them first.
        armed = self.stop_orders.get(msg.symbol, {})
        entry = armed.get(msg.order_id)
        if entry is not None and entry["team_id"] == team_id:
            del armed[msg.order_id]
            return

        cancelled = book.cancel_order(msg.order_id, team_id)
        if cancelled is None:
            await self._send(ws, ErrorMsg(
                code="ORDER_NOT_FOUND",
                message=(
                    f"Order {msg.order_id!r} not found "
                    f"or not owned by {team_id}"
                ),
            ))
            return

        self._charge_cancel_fee(team_id, cancelled)

    async def _handle_teacher_command(
        self, ws: Any, msg: TeacherCommand, team_id: str
    ) -> None:
        """Execute a remote teacher command sent by shock_tool or other tool."""
        if team_id not in self.teacher_clients:
            await self._send(ws, ErrorMsg(
                code="FORBIDDEN", message="Only teacher-role connections may send TeacherCommand",
            ))
            return

        cmd = msg.command
        if cmd == "open_session":
            # Ack no-ops explicitly: on hosted deployments the session
            # auto-opens, so START from the dashboard is usually a no-op —
            # without feedback that reads as a dead button.
            already = bool(self.session_open or self.auction_phase)
            await self.open_session()
            await self._send(ws, CommandAck(
                command=cmd, ok=not already,
                detail=("session already open — nothing to do" if already
                        else "session opening")))
        elif cmd == "close_session":
            noop = not (self.session_open or self.auction_phase)
            await self.close_session()
            await self._send(ws, CommandAck(
                command=cmd, ok=not noop,
                detail=("session already closed — nothing to do" if noop
                        else "session closing")))
        elif cmd == "end_session":
            await self.end_session()
            await self._send(ws, CommandAck(
                command=cmd, detail="session ended — week banked"))
        elif cmd == "new_season":
            await self.new_season()
            await self._send(ws, CommandAck(
                command=cmd, detail="new season — all season state wiped"))
        elif cmd == "set_week":
            try:
                week = int(msg.params["week"])
            except (KeyError, TypeError, ValueError):
                await self._send(ws, ErrorMsg(
                    code="MISSING_PARAM", message="week (int) required"))
                return
            await self.set_week(week)
            await self._send(ws, CommandAck(
                command=cmd, detail=f"week set to {week}"))
        elif cmd == "inject_shock":
            shock_id = msg.params.get("shock_id", "")
            if not shock_id:
                await self._send(ws, ErrorMsg(code="MISSING_PARAM", message="shock_id required"))
                return
            await self.inject_shock(shock_id, msg.params.get("shock_params"))
            await self._send(ws, CommandAck(
                command=cmd, detail=f"shock fired: {shock_id}"))
        elif cmd == "set_fee_rate":
            try:
                rate = float(msg.params["rate"])
            except (KeyError, ValueError):
                await self._send(ws, ErrorMsg(code="MISSING_PARAM", message="rate (float) required"))
                return
            await self.set_fee_rate(rate)
        elif cmd == "fee_schedule":
            # The roster has already been written by the portal; pick it up
            # and announce it to this venue's clients.
            old = msg.params.get("old") if isinstance(
                msg.params.get("old"), dict) else None
            await self.announce_fee_schedule(old)
        elif cmd == "lift_circuit_breakers":
            await self.lift_circuit_breakers()
            await self._send(ws, CommandAck(
                command=cmd, detail="circuit breakers lifted"))
        elif cmd == "ipo_announce":
            ok, detail = await self.teacher_announce_ipo(msg.params)
            if not ok:
                await self._send(ws, ErrorMsg(code="BAD_IPO", message=detail))
        elif cmd == "ipo_cancel":
            ok, detail = await self.teacher_cancel_ipo(
                str(msg.params.get("symbol", "")))
            if not ok:
                await self._send(ws, ErrorMsg(code="BAD_IPO", message=detail))
        else:
            await self._send(ws, ErrorMsg(
                code="UNKNOWN_COMMAND", message=f"Unknown teacher command: {cmd!r}",
            ))

    # ------------------------------------------------------------------
    # Upgrade purchases (Phase 2 economy)
    # ------------------------------------------------------------------

    async def _handle_upgrade_request(
        self, ws: Any, msg: UpgradeRequest, team_id: str
    ) -> None:
        """Handle a shop purchase submitted through the teacher relay.

        Only teacher-role connections may submit these: the student portal
        verifies the team token over HTTPS and forwards the request on its
        relay connection. The exchange still re-validates everything except
        identity, because it is the only component that can see live cash.
        """
        if team_id not in self.teacher_clients:
            await self._send(ws, ErrorMsg(
                code="FORBIDDEN",
                message=("Upgrade purchases must be submitted through the "
                         "student portal"),
            ))
            return

        ok, message, detail = await self.purchase_upgrade(msg.team, msg.upgrade)
        await self._send(ws, SessionEvent(
            event="UPGRADE_RESULT",
            message=message,
            data={"ok": ok, "team": msg.team, "upgrade": msg.upgrade,
                  "request_id": msg.request_id, **detail},
        ))
        if ok:
            await self._broadcast(SessionEvent(
                event="UPGRADE_PURCHASED",
                message=message,
                data={"team": msg.team, "upgrade": msg.upgrade, **detail},
            ))
            await self._broadcast(self._build_leaderboard())

    async def purchase_upgrade(
        self, team: str, key: str
    ) -> tuple[bool, str, dict]:
        """Validate, debit, grant. Returns (ok, message, detail).

        Order of checks matters: nothing is debited until every check has
        passed, and the roster is written last so a failed write cannot leave
        a team charged for an upgrade it does not own.
        """
        if key not in upgrades.CATALOG:
            return False, f"Unknown upgrade {key!r}", {}
        item = upgrades.CATALOG[key]
        cost = float(item["price"])

        if not self.scenario.flag("purchase_window"):
            return False, (
                f"The shop is closed. Purchases are only open during a "
                f"capital-allocation window (weeks 4 and 7); it is currently "
                f"week {self.scenario.week}."
            ), {}

        if upgrades.owned(team).get(key):
            return False, f"{team} already owns {item['label']}", {}

        # Only the team's bots that actually hold cash on THIS exchange can
        # fund the purchase.
        bots = [b for b in upgrades.team_bots(team) if b in self.portfolios]
        if not bots:
            return False, (
                f"No connected bots for {team!r} on this exchange — connect "
                f"a bot before buying"
            ), {}

        bot_cash = {b: self.portfolios[b].cash for b in bots}
        available = sum(c for c in bot_cash.values() if c > 0)
        if available < cost:
            return False, (
                f"{item['label']} costs ${cost:,.0f} but {team} has only "
                f"${available:,.0f} in available cash"
            ), {"cost": cost, "available": round(available, 2)}

        shares = upgrades.split_cost(bot_cash, cost)
        if not shares:
            return False, f"{team} has no fundable cash balance", {}

        if not upgrades.grant(team, key):
            return False, (
                f"Could not record the purchase for {team!r} — is the team in "
                f"the roster?"
            ), {}

        # Debit pro-rata, and credit the hosting exchange team. Venue revenue
        # is exactly where colocation and data money goes in reality, and it
        # shows up in exchange_fees_by_team on the leaderboard.
        for bot, amount in shares.items():
            self.portfolios[bot].cash -= amount
            self.seat_debits += amount
        self.exchange_revenue += cost

        logger.info(
            "UPGRADE %s bought %s for $%.0f (split across %d bot(s))",
            team, key, cost, len(shares),
        )
        for bot in shares:
            client = self.clients.get(bot)
            if client:
                await self._send(
                    client, self.portfolios[bot].to_message(self.ref_prices))

        return True, (
            f"{team} bought {item['label']} for ${cost:,.0f} — {item['effect']}"
        ), {
            "cost": cost,
            "split": {b: round(a, 2) for b, a in shares.items()},
            "effect": item["effect"],
        }

    # ------------------------------------------------------------------
    # Seat purchases (Phase 10: grow the firm)
    # ------------------------------------------------------------------

    class _CaptureWS:
        """Collects what the order path would send, for the ticket result."""

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

    async def _handle_manual_order(
        self, ws: Any, msg: ManualOrder, team_id: str
    ) -> None:
        """An ORDER TICKET submission from the team portal.

        Same trust model as upgrades/seats: the portal verified the team
        token over HTTP and forwards on the teacher relay; the exchange
        re-validates that the bot belongs to the team, then runs the NORMAL
        order path as that bot — every guard (band, SSR, halts, cash,
        quota) applies exactly as it would to the bot's own order.
        """
        if team_id not in self.teacher_clients:
            await self._send(ws, ErrorMsg(
                code="FORBIDDEN",
                message="Manual orders must come through the team portal",
            ))
            return

        if msg.bot_id not in upgrades.team_bots(msg.team):
            await self._send(ws, SessionEvent(
                event="MANUAL_ORDER_RESULT",
                message=f"{msg.bot_id!r} is not one of {msg.team}'s bots",
                data={"ok": False, "request_id": msg.request_id,
                      "error": f"{msg.bot_id!r} is not one of your bots"},
            ))
            return

        # The whole point of the ticket is trying the market BEFORE your
        # bot runs — create the portfolio on demand, exactly as the
        # handshake would (allocated capital + the session's share grant).
        if msg.bot_id not in self.portfolios:
            cfg = config._read_roster().get(msg.team) or {}
            import shared.roster as roster_shape
            role = ("broker" if msg.bot_id in roster_shape.broker_ids_of(cfg)
                    else "trader")
            portfolio = Portfolio(
                team_id=msg.bot_id, role=role, level=1,
                cash=config.funded_cash_for(msg.bot_id))
            self.portfolios[msg.bot_id] = portfolio
            n = config.STARTING_SHARES_PER_SYMBOL
            if self.session_open and self._session_granted and n > 0:
                for sym in self.books:
                    if not config.is_future(sym):
                        portfolio.positions[sym] = n
                        portfolio.avg_cost[sym] = self.ref_prices.get(sym, 0.0)
                        self.midsession_grants[sym] = (
                            self.midsession_grants.get(sym, 0) + n)

        capture = self._CaptureWS()
        await self._handle_place_order(capture, PlaceOrder(
            team_id=msg.bot_id, symbol=msg.symbol, side=msg.side,
            order_type=msg.order_type, price=msg.price,
            quantity=msg.quantity,
        ), msg.bot_id)

        # Forward everything to the bot's live connection too, then build
        # the ticket verdict from what the order path actually said.
        bot_ws = self.clients.get(msg.bot_id)
        result: dict = {"ok": True, "request_id": msg.request_id}
        detail = f"{msg.side} {msg.quantity} {msg.symbol} submitted"
        for payload in capture.sent:
            if bot_ws:
                await self._send_payload(msg.bot_id, bot_ws, payload)
            data = json.loads(payload)
            if data.get("type") == "error":
                result = {"ok": False, "request_id": msg.request_id,
                          "error": f"[{data.get('code')}] {data.get('message')}"}
                detail = result["error"]
                break
            if data.get("type") == "order_ack":
                detail = (f"Order accepted: {msg.side} {msg.quantity} "
                          f"{msg.symbol} @ {data.get('price')}")
        result["detail"] = detail
        await self._send(ws, SessionEvent(
            event="MANUAL_ORDER_RESULT", message=detail, data=result))

    async def _handle_seat_request(
        self, ws: Any, msg: SeatRequest, team_id: str
    ) -> None:
        """Handle a mid-season hire submitted through the teacher relay.

        Same trust model as an upgrade purchase: the portal has verified the
        student's team token over HTTP and forwards the request on its relay
        connection; the exchange re-validates everything except identity.
        """
        if team_id not in self.teacher_clients:
            await self._send(ws, ErrorMsg(
                code="FORBIDDEN",
                message="Seat purchases must be submitted through the portal",
            ))
            return

        ok, message, detail = await self.purchase_seat(
            msg.team, msg.kind, msg.capital, msg.bot_id)
        await self._send(ws, SessionEvent(
            event="SEAT_RESULT",
            message=message,
            data={"ok": ok, "team": msg.team, "kind": msg.kind,
                  "request_id": msg.request_id, **detail},
        ))
        if ok:
            await self._broadcast(SessionEvent(
                event="SEAT_PURCHASED",
                message=message,
                data={"team": msg.team, "kind": msg.kind,
                      "bot_id": detail.get("bot_id", "")},
            ))
            await self._broadcast(self._build_leaderboard())

    async def purchase_seat(
        self, team: str, kind: str, capital: int = 0, bot_id_hint: str = "",
    ) -> tuple[bool, str, dict]:
        """Validate, write the roster, debit. Returns (ok, message, detail).

        A seat is not an upgrade: for a trader or broker the money is *moved*,
        not spent. It is debited pro-rata from the team's existing bots and
        written into the roster as the new bot's allocated capital, which
        `config.starting_cash_for()` hands over the first time that bot
        connects — so the team's combined net worth is unchanged at the moment
        of purchase, and the new seat starts with exactly what was taken.

        An exchange licence is a genuine outflow: a fee for the right to charge
        other people's flow. It is NOT credited to the hosting venue — you are
        not buying a competing venue from a competitor.
        """
        try:
            cost = seats.validate(kind, capital,
                                  config._read_roster().get(team) or {})
        except seats.SeatError as exc:
            return False, str(exc), {}

        if not self.scenario.flag("purchase_window"):
            return False, (
                f"Hiring is only open during a capital-allocation window "
                f"(weeks 4 and 7); it is currently week {self.scenario.week}."
            ), {}

        if team not in config._read_roster():
            return False, f"Unknown team {team!r}", {}

        # Only the team's bots holding cash on THIS exchange can fund a hire.
        bots = [b for b in upgrades.team_bots(team) if b in self.portfolios]
        if not bots:
            return False, (
                f"No connected bots for {team!r} on this exchange — connect a "
                f"bot before hiring"
            ), {}

        bot_cash = {b: self.portfolios[b].cash for b in bots}
        available = sum(c for c in bot_cash.values() if c > 0)
        if available < cost:
            return False, (
                f"That seat costs ${cost:,.0f} but {team} has only "
                f"${available:,.0f} in available cash"
            ), {"cost": cost, "available": round(available, 2)}

        shares = upgrades.split_cost(bot_cash, cost)
        if not shares:
            return False, f"{team} has no fundable cash balance", {}

        # Roster first: a failed write must not leave a team charged for a
        # seat that does not exist.
        try:
            seat = seats.add_seat(team, kind, int(cost) if kind != "exchange"
                                  else 0, bot_id_hint)
        except seats.SeatError as exc:
            return False, str(exc), {}

        for bot, amount in shares.items():
            self.portfolios[bot].cash -= amount

        logger.info("SEAT %s hired %s for $%.0f (split across %d bot(s))",
                    team, seat["bot_id"], cost, len(shares))
        for bot in shares:
            client = self.clients.get(bot)
            if client:
                await self._send(
                    client, self.portfolios[bot].to_message(self.ref_prices))

        label = seats.SEAT_CATALOG[kind]["label"]
        if kind == "exchange":
            what = (f"{team} bought an {label.lower()} for ${cost:,.0f} — "
                    f"venue {seat['bot_id']} on port {seat['port']}")
        else:
            what = (f"{team} hired {seat['bot_id']} with ${cost:,.0f} of "
                    f"capital, debited pro-rata from its existing bots")
        return True, what, {
            "bot_id": seat["bot_id"],
            "cost": cost,
            "capital": seat["capital"],
            "port": seat["port"],
            "split": {b: round(a, 2) for b, a in shares.items()},
            "run": seats.run_command(
                kind, seat["bot_id"],
                port=seat["port"] or config.PORT),
        }

    # ------------------------------------------------------------------
    # Trade settlement
    # ------------------------------------------------------------------

    async def _process_trade(self, trade: Trade) -> None:
        """Settle a trade: update portfolios, notify parties, broadcast snapshot.

        Maker/taker model (Level 3, default): the aggressor (taker) pays
        TAKER_FEE_RATE × notional; the resting side (maker) earns
        MAKER_REBATE_RATE × notional as a liquidity rebate. The exchange
        keeps the difference. Legacy model: trade.fee split 50/50.
        """
        notional = trade.price * trade.quantity

        if config.MAKER_TAKER_ENABLED:
            # Fee tier is per team: a team that bought the fee_tier upgrade
            # pays less as taker and earns more as maker.
            taker_id = trade.buyer_id if trade.aggressor == "buy" else trade.seller_id
            maker_id = trade.seller_id if trade.aggressor == "buy" else trade.buyer_id
            taker_fee = round(
                notional * config.config_for_team(taker_id, "taker_fee"), 8)
            rebate = round(
                notional * config.config_for_team(maker_id, "maker_rebate"), 8)
            if trade.aggressor == "buy":     # buyer crossed the spread
                buyer_fee, buyer_rebate  = taker_fee, 0.0
                seller_fee, seller_rebate = 0.0, rebate
            else:                            # seller crossed the spread
                buyer_fee, buyer_rebate  = 0.0, rebate
                seller_fee, seller_rebate = taker_fee, 0.0
            self.exchange_revenue += taker_fee - rebate
            fee_paid, rebate_paid = taker_fee, rebate
        else:
            buyer_fee = seller_fee = trade.fee / 2
            buyer_rebate = seller_rebate = 0.0
            self.exchange_revenue += trade.fee
            fee_paid, rebate_paid = trade.fee, 0.0

        buyer = self.portfolios.get(trade.buyer_id)
        seller = self.portfolios.get(trade.seller_id)

        if config.is_future(trade.symbol):
            # Cash-settled: no notional moves, only fees and the position.
            if buyer:
                buyer.apply_futures_fill(trade.symbol, trade.price,
                                         trade.quantity, "buy",
                                         buyer_fee, buyer_rebate)
            if seller:
                seller.apply_futures_fill(trade.symbol, trade.price,
                                          trade.quantity, "sell",
                                          seller_fee, seller_rebate)
        else:
            if buyer:
                buyer.apply_buy(trade.symbol, trade.price, trade.quantity,
                                buyer_fee, buyer_rebate)
            if seller:
                seller.apply_sell(trade.symbol, trade.price, trade.quantity,
                                  seller_fee, seller_rebate)

        trade_msg = TradeExecution(
            trade_id=trade.trade_id,
            symbol=trade.symbol,
            price=trade.price,
            quantity=trade.quantity,
            buyer_id=trade.buyer_id,
            seller_id=trade.seller_id,
            aggressor=trade.aggressor,
            fee=fee_paid,
            maker_rebate=rebate_paid,
        )

        buyer_ws = self.clients.get(trade.buyer_id)
        seller_ws = self.clients.get(trade.seller_id)

        if buyer_ws:
            await self._send(buyer_ws, trade_msg)
            if buyer:
                await self._send(buyer_ws, buyer.to_message(self.ref_prices))
        if seller_ws:
            await self._send(seller_ws, trade_msg)
            if seller:
                await self._send(seller_ws, seller.to_message(self.ref_prices))

        # Apply market impact — shifts price in the aggressor's direction.
        engine = self.price_engines.get(trade.symbol)
        if engine:
            self.ref_prices[trade.symbol] = engine.apply_trade_impact(trade)
            if config.CIRCUIT_BREAKERS_ENABLED:
                should_halt, reason = self.circuit_breaker.check_symbol(trade.symbol, engine)
                if should_halt:
                    await self._halt_symbol(trade.symbol, reason)
        else:
            self.ref_prices[trade.symbol] = trade.price

        # Book the fill before broadcasting so the leaderboard that follows
        # already includes it.
        self.trade_log.append(trade)
        self.trade_count += 1
        self._record_trade_stats(trade)

        # Broadcast trade to all observers (teacher relay, dashboards).
        # Buyer and seller already received it directly above.
        await self._broadcast(trade_msg, skip_ids={trade.buyer_id, trade.seller_id})
        await self._broadcast(self._make_snapshot(trade.symbol, self.books[trade.symbol]))
        # NO leaderboard push here: broadcasting a full leaderboard to every
        # client per trade is O(clients × trades) and was the load-test
        # bottleneck (45 conns fine, 90 saturated). The 2-second leaderboard
        # loop keeps dashboards fresh at a cost that never scales with fills.

        logger.info(
            "TRADE %-8s  %-5s  %4d @ %10.4f  buyer=%-18s  seller=%-18s  fee=%.4f",
            trade.trade_id[:8], trade.symbol, trade.quantity, trade.price,
            trade.buyer_id, trade.seller_id, trade.fee,
        )

    def _record_trade_stats(self, trade: Trade) -> None:
        """Fold one fill into the cumulative per-participant activity counters."""
        notional = trade.price * trade.quantity
        maker_id = trade.seller_id if trade.aggressor == "buy" else trade.buyer_id
        b = self.part_stats[trade.buyer_id]
        b["trade_count"] += 1
        b["volume"] += notional
        b["buy_count"] += 1
        b["shares_bought"] += trade.quantity
        s = self.part_stats[trade.seller_id]
        s["trade_count"] += 1
        s["volume"] += notional
        s["sell_count"] += 1
        s["shares_sold"] += trade.quantity
        self.part_stats[maker_id]["maker_count"] += 1

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------

    async def _book_snapshot_loop(self) -> None:
        """Push book snapshots to all clients every SNAPSHOT_INTERVAL_SEC."""
        while True:
            await asyncio.sleep(config.SNAPSHOT_INTERVAL_SEC)
            if not self.clients:
                continue
            for symbol, book in self.books.items():
                await self._broadcast(self._make_snapshot(symbol, book))

    async def _leaderboard_loop(self) -> None:
        """Push the leaderboard every LEADERBOARD_INTERVAL_SEC whenever clients are connected."""
        while True:
            await asyncio.sleep(config.LEADERBOARD_INTERVAL_SEC)
            if self.clients:
                await self._broadcast(self._build_leaderboard())

    async def _price_tick_loop(self) -> None:
        """Advance prices once per second through supply/demand.

        Each tick:
          1. The registry advances the SHARED fundamental (same path on every
             venue — plugins/securities/defaults.py).
          2. The book's depth-weighted microprice is blended with it
             (MID_BLEND_WEIGHT internal / the rest fundamental), the move is
             clamped to MAX_TICK_MOVE, and the result becomes fair value.
          3. Engine.tick() decays temporary trade impact and returns
             market_price — flow, not randomness, is the intraday driver.
          4. Market-wide circuit breaker is checked.
        """
        while True:
            await asyncio.sleep(1.0)
            await self.advance_tick()

    async def advance_tick(self) -> None:
        """Advance the game by exactly one tick.

        Extracted from the live loop so the season simulator can drive the
        REAL exchange mechanics at maximum speed with a virtual clock.
        """
        self.tick += 1
        self._refill_quotas()
        new_prices = self.registry.tick_prices(self.tick)

        for sym, fundamental in new_prices.items():
            engine = self.price_engines.get(sym)
            if not engine:
                self.ref_prices[sym] = fundamental
                continue

            # Endogenous price formation: the traded market outweighs the
            # shared fundamental. The book is read as a depth-weighted
            # microprice, which IS this venue's supply/demand reading.
            internal = self._internal_reference(sym, fundamental)
            # A cash-settled future is MARKED against its index, not against
            # its own book — that is what settlement means. So its book is
            # free to trade at a basis to fair value (a real arbitrage for
            # students to find) without dragging the mark with it.
            w = (config.FUTURES_MID_BLEND_WEIGHT if config.is_future(sym)
                 else config.MID_BLEND_WEIGHT)
            fair = (w * internal + (1 - w) * fundamental) if internal else fundamental
            # A future is marked from its index, and that index is built out of
            # prices this clamp has already smoothed. Clamping it a second time
            # would only make the contract lag its own underlying and break the
            # basis a hedger depends on.
            if not config.is_future(sym):
                fair = self._clamp_fair_move(engine.fair_value, fair)

            engine.update_fair_value(fair)
            self.ref_prices[sym] = engine.tick()

        # Auction countdowns: broadcast the indicative cross each tick,
        # then run the cross when the clock reaches zero.
        if self.auction_phase == "preclose":
            self.auction_ticks_left -= 1
            if self.auction_ticks_left <= 0:
                await self._run_closing_cross()
        if self.auction_phase == "preopen":
            self.auction_ticks_left -= 1
            if self.auction_ticks_left <= 0:
                await self._run_opening_cross()
            else:
                indicative = {s: v for s, v in
                              ((sym, b.auction_preview())
                               for sym, b in self.books.items()) if v}
                await self._broadcast(SessionEvent(
                    event="AUCTION_INDICATIVE",
                    message=(f"Opening cross in "
                             f"{self.auction_ticks_left} ticks"),
                    data={"ticks_left": self.auction_ticks_left,
                          "symbols": indicative},
                ))

        # Armed stops also fire on the venue mark, not only on prints — a
        # gap with no trades must still take a student out of a position.
        if self.session_open:
            for sym in [s for s, pend in self.stop_orders.items() if pend]:
                mark = self.ref_prices.get(sym)
                if mark:
                    await self._check_stop_triggers(sym, mark)

        # IPO lifecycle: open the book, price it, list the stock.
        if self.session_open and self.ipos:
            await self._advance_ipos()

        # Short-sale rule triggers: down SSR_TRIGGER_PCT from the open.
        if self.session_open and config.SSR_TRIGGER_PCT > 0:
            for sym, engine in self.price_engines.items():
                if (sym not in self.ssr_active
                        and not config.is_future(sym)
                        and engine.session_return() <= -config.SSR_TRIGGER_PCT):
                    self.ssr_active.add(sym)
                    logger.info("SSR ON  %s (down %.1f%% from open)",
                                sym, engine.session_return() * -100)
                    await self._broadcast(SessionEvent(
                        event="SSR_ON",
                        message=(f"{sym} is now under the short-sale rule — "
                                 f"shorts must add liquidity above the bid"),
                        data={"symbol": sym},
                    ))

        # Calendar: announce, fire, and advance any ramp in progress. Runs
        # after the price blend so a ramp's fair value is not averaged away.
        if self.session_open:
            await self._advance_calendar()

        # Futures variation margin — the "daily" mark, run before the
        # maintenance check so a losing future can actually trigger it.
        if self.session_open and self.tick % max(
                1, config.FUTURES_SETTLE_TICKS) == 0:
            await self._settle_futures()

        # Financing costs and maintenance-margin enforcement.
        if self.session_open:
            await self._apply_carry_and_maintenance()
            # Season bookkeeping: equity snapshots feed risk-adjusted rank,
            # and a periodic checkpoint means a crash costs a minute, not a week.
            self.snapshot_equity()
            if (self.season_persists
                    and time.time() - self._last_season_save
                    >= persistence.SEASON_SAVE_INTERVAL_SEC):
                self.save_season()

        # Market-wide circuit breaker — checked every second.
        if config.CIRCUIT_BREAKERS_ENABLED:
            should_halt, reason, level = self.circuit_breaker.check_market_wide(
                self.price_engines
            )
            if should_halt and not self.circuit_breaker.market_halted:
                await self._halt_all_symbols(reason, level)

    def _internal_reference(self, symbol: str, fundamental: float) -> float | None:
        """This venue's own reading of the price, from its book. Never teleports.

        Two-sided book → the depth-weighted microprice, so demand pressure
        (size sitting on each side) moves the reference toward the heavy side.

        One-sided, empty or torn book → the PREVIOUS reference, decayed
        DEAD_BOOK_DECAY of the way toward the fundamental. This is the
        no-teleport rule: a market maker's cancel/replace leaves the book
        momentarily one-sided every requote, and the old code answered that by
        falling back to a possibly-minutes-old last trade, which made the fair
        value jump. Holding the last good reading is what a real venue does.
        """
        book = self.books.get(symbol)
        prev = self._internal_ref.get(symbol)
        if book is not None:
            snap = book.get_snapshot(depth=1)
            micro = price_engine.microprice(snap["bids"], snap["asks"])
            if micro:
                self._internal_ref[symbol] = micro
                return micro
        if prev is None:
            return None
        # Dead book: drift home rather than freeze at a stale level.
        decayed = prev + config.DEAD_BOOK_DECAY * (fundamental - prev)
        self._internal_ref[symbol] = decayed
        return decayed

    @staticmethod
    def _clamp_fair_move(previous: float, proposed: float) -> float:
        """Limit one tick's fair-value move to MAX_TICK_MOVE.

        The blend can only ever propose a big jump when something is wrong (a
        torn book, a market maker's quote landing far from the last one), and a
        student watching a chart cannot tell a bug from a shock. News is NOT
        clamped here: shocks, calendar prints and dividends own the fair value
        directly through the ramp scheduler, so they still move prices fast.
        """
        limit = config.MAX_TICK_MOVE
        if previous <= 0 or limit <= 0:
            return proposed
        ceiling = previous * (1.0 + limit)
        floor = previous * (1.0 - limit)
        return min(ceiling, max(floor, proposed))

    # ------------------------------------------------------------------
    # Market calendar
    # ------------------------------------------------------------------

    async def _advance_calendar(self) -> None:
        """One calendar tick: announcements, firings, then ramp steps.

        Event ticks are SESSION-RELATIVE ("tick 400" = 400 ticks into the
        class), so a restored season with tick 6000+ does not detonate the
        whole calendar at the open.
        """
        tick = self.tick - self.session_open_tick

        # The priority feed goes out first, to buyers only (calendar_feed).
        early = self.calendar.due_early_announcements(tick)
        if early:
            await self._announce_calendar(imminent=early,
                                          only_upgrade="calendar_feed")

        due = self.calendar.due_announcements(tick)
        if due:
            await self._announce_calendar(imminent=due)

        for event in self.calendar.due_events(tick):
            if event.kind == DIVIDEND:
                await self._pay_dividend(event)
            else:
                await self._fire_price_event(event)

        # Advance ramps. A ramp owns its symbol's fair value while it runs,
        # so this is applied after the GBM/book blend, not blended into it.
        for sym, fair in self.calendar.step_ramps().items():
            engine = self.price_engines.get(sym)
            if engine is None:
                continue
            engine.update_fair_value(fair)
            self.ref_prices[sym] = engine.market_price
            # Keep the external anchor in step so the GBM continues from the
            # new level once the ramp finishes.
            self.registry.prices[sym] = fair
            if sym in self.registry.securities:
                self.registry.securities[sym]["current_price"] = fair
            if config.CIRCUIT_BREAKERS_ENABLED:
                should_halt, reason = self.circuit_breaker.check_symbol(
                    sym, engine)
                if should_halt:
                    await self._halt_symbol(sym, reason)

    async def _announce_calendar(self, imminent: list | None = None,
                                 only_upgrade: str | None = None) -> None:
        """Publish the upcoming event list — timing only, never direction.

        `only_upgrade` narrows delivery to the bots of teams that bought that
        upgrade, which is how the priority calendar feed is enforced: the early
        wave is a per-client send, the normal wave a broadcast. What is being
        sold is delivery speed — the same public facts, sooner.
        """
        upcoming = self.calendar.upcoming(self.tick)
        if imminent:
            names = ", ".join(
                f"{e.kind} {e.symbol or 'MARKET'} @ t{e.tick}" for e in imminent)
            message = f"Upcoming: {names}"
            if only_upgrade:
                message = f"[priority feed] {message}"
        else:
            message = (f"{len(upcoming)} scheduled event(s) this session"
                       if upcoming else "No scheduled events this session")
        msg = SessionEvent(
            event="CALENDAR",
            message=message,
            data={
                "tick": self.tick,
                "week": self.scenario.week,
                "events": upcoming,
                "imminent": [e.public() for e in (imminent or [])],
                "announce_lead": self.calendar.announce_lead,
                "early": bool(only_upgrade),
            },
        )
        if only_upgrade is None:
            await self._broadcast(msg)
            return
        recipients = self._clients_with_upgrade(only_upgrade)
        if not recipients:
            return
        payload = msg.model_dump_json()
        self._record(payload)
        await asyncio.gather(
            *(self._send_payload(tid, ws, payload) for tid, ws in recipients),
            return_exceptions=True)

    def _clients_with_upgrade(self, key: str) -> list[tuple[str, Any]]:
        """(team_id, ws) for every connected bot whose team owns `key`.

        Teacher/observer connections are excluded: a targeted send is a
        product being delivered to a customer, not market data.
        """
        out = []
        for team_id, ws in list(self.clients.items()):
            if team_id in self.teacher_clients:
                continue
            portfolio = self.portfolios.get(team_id)
            if portfolio is not None and portfolio.role == "observer":
                continue
            if config.upgrades_for(team_id).get(key):
                out.append((team_id, ws))
        return out

    async def _fire_price_event(self, event) -> None:
        """Turn an earnings/econ print into ramped price moves."""
        pct = event.resolve()
        targets = ([event.symbol] if event.symbol and not event.market_wide
                   else list(self.books.keys()))
        affected = []
        for sym in targets:
            if sym not in self.price_engines:
                continue
            engine = self.price_engines[sym]
            self.calendar.start_ramp(
                sym, engine.fair_value, pct,
                label=f"{event.kind}@{event.tick}")
            affected.append(sym)

        logger.info("CALENDAR %s %s %+.2f%% over %d ticks",
                    event.kind, ",".join(affected) or "-", pct * 100,
                    self.calendar.ramp_ticks)
        await self._broadcast(SessionEvent(
            event="CALENDAR_EVENT",
            message=(f"{event.kind.replace('_', ' ').title()}: "
                     f"{event.symbol or 'market-wide'} moving "
                     f"{pct * 100:+.1f}%"),
            data={
                "kind": event.kind,
                "symbol": event.symbol,
                "market_wide": event.market_wide,
                "pct": round(pct, 6),
                "affected": affected,
                "ramp_ticks": self.calendar.ramp_ticks,
                "overshoot": self.calendar.overshoot,
                "tick": self.tick,
            },
        ))

    async def _pay_dividend(self, event) -> None:
        """Settle a dividend in cash and drop the price ex-dividend.

        Longs receive `amount_per_share`; shorts PAY it, because a borrowed
        share still owes its dividend. The fair value drops by the same amount
        so the dividend is not a free arbitrage for anyone who buys the tick
        before — the ex-dividend adjustment is the whole lesson.
        """
        sym = event.symbol
        amount = float(event.amount_per_share)
        if not sym or sym not in self.books or amount <= 0:
            return

        paid = 0.0
        for p in self.portfolios.values():
            qty = p.positions.get(sym, 0)
            if not qty or p.role == "observer":
                continue
            cash = qty * amount
            p.cash += cash
            paid += cash
            ws = self.clients.get(p.team_id)
            if ws:
                await self._send(ws, p.to_message(self.ref_prices))

        engine = self.price_engines.get(sym)
        if engine:
            engine.update_fair_value(max(0.01, engine.fair_value - amount))
            self.ref_prices[sym] = engine.market_price
            self.registry.prices[sym] = engine.fair_value
            if sym in self.registry.securities:
                self.registry.securities[sym]["current_price"] = engine.fair_value

        self.dividends_net += paid
        logger.info("DIVIDEND %s $%.2f/share  net cash to holders $%.2f",
                    sym, amount, paid)
        await self._broadcast(SessionEvent(
            event="DIVIDEND",
            message=(f"{sym} pays ${amount:.2f}/share — longs credited, "
                     f"shorts debited, price marked down ex-dividend"),
            data={"symbol": sym, "amount_per_share": amount,
                  "net_paid": round(paid, 2), "tick": self.tick},
        ))

    async def _settle_futures(self) -> None:
        """Mark every open futures position to the index and settle in cash.

        This is the variation-margin call: whatever the position has gained or
        lost since the last mark becomes real money, and the entry price resets
        to the mark. A team that is wrong on the index pays for it here rather
        than at some distant expiry.
        """
        settled: dict[str, float] = {}
        for sym in config.FUTURES:
            if sym not in self.books:
                continue
            mark = self.ref_prices.get(sym)
            if not mark:
                continue
            for p in self.portfolios.values():
                if p.role == "observer" or not p.positions.get(sym):
                    continue
                variation = p.settle_future(sym, mark)
                if variation:
                    settled[p.team_id] = settled.get(p.team_id, 0.0) + variation
                ws = self.clients.get(p.team_id)
                if ws:
                    await self._send(ws, p.to_message(self.ref_prices))

        if settled:
            logger.info("FUTURES MARK  %s",
                        "  ".join(f"{t}={v:+.2f}" for t, v in settled.items()))
            await self._broadcast(SessionEvent(
                event="FUTURES_SETTLEMENT",
                message=("Index futures marked to market — variation margin "
                         "settled in cash"),
                data={"tick": self.tick,
                      "marks": {s: round(self.ref_prices.get(s, 0.0), 4)
                                for s in config.FUTURES if s in self.books},
                      "settled": {t: round(v, 2) for t, v in settled.items()}},
            ))

    async def _apply_carry_and_maintenance(self) -> None:
        """Once per tick: charge financing costs, then liquidate any team
        whose conservatively-marked net worth is below maintenance."""
        # Carry costs: margin interest on borrowed cash, borrow fees on shorts.
        # Idle cash earns interest — the hurdle rate a strategy must beat.
        for p in self.portfolios.values():
            if p.role == "observer" or p.liquidated:
                continue
            carry = 0.0
            if config.MARGIN_ENABLED:
                if p.cash < 0:
                    carry += -p.cash * config.config_for_team(
                        p.team_id, "margin_rate")
                # Short futures owe no stock borrow — there is no share to
                # locate, only margin.
                short_mv = sum(
                    -q * self.ref_prices.get(s, 0.0)
                    for s, q in p.positions.items()
                    if q < 0 and not config.is_future(s)
                )
                if short_mv > 0:
                    carry += short_mv * config.BORROW_FEE_PER_TICK
            if carry > 0:
                p.cash -= carry
                p.total_carry_paid += carry
            elif p.cash > 0 and config.CASH_INTEREST_PER_TICK > 0:
                # Netted against carry so a borrower never earns interest too.
                interest = p.cash * config.CASH_INTEREST_PER_TICK
                p.cash += interest
                self.interest_credited += interest

        # Maintenance check: force-liquidate teams below the threshold.
        if config.LIQUIDATION_ENABLED:
            bidask = self._bidask_marks()
            for p in list(self.portfolios.values()):
                if p.role == "observer" or p.liquidated or p.starting_cash <= 0:
                    continue
                nw = p.net_worth(self.ref_prices, bidask)
                if nw >= p.starting_cash * config.MAINTENANCE_FRACTION:
                    continue
                if self.tick < p.shield_until_tick:
                    continue        # inside a shield grace period
                if await self._try_risk_shield(p, nw):
                    continue
                await self._liquidate(p, nw)

    # ------------------------------------------------------------------
    # Message quotas, cancellation fees, latency tiers
    # ------------------------------------------------------------------

    def quota_for(self, team_id: str) -> float:
        """Order/cancel messages per tick for one bot (0 = unmetered)."""
        return float(config.config_for_team(team_id, "order_quota"))

    def _take_quota(self, team_id: str, n: float = 1.0) -> bool:
        """Consume one message of quota. True if the bot was within it."""
        quota = self.quota_for(team_id)
        if quota <= 0:
            return True
        bucket = self.quotas.get(team_id)
        if bucket is None or bucket.refill != quota:
            # First message, or the quota changed (new week / quota upgrade).
            bucket = limits.bucket_for(quota)
            self.quotas[team_id] = bucket
        return bucket.take(n)

    def _refill_quotas(self) -> None:
        """One tick's allowance for every bot. Called from advance_tick."""
        for bucket in self.quotas.values():
            bucket.tick()

    def _charge_cancel_fee(self, team_id: str, order: Any) -> None:
        """Bill a cancellation, when the week charges for them.

        The fee goes to the venue, like every other exchange fee, and lands in
        total_fees_paid so it shows up in the team's TCA.
        """
        if not self.scenario.flag("cancellation_fees"):
            return
        fee = (config.CANCEL_FEE_PER_ORDER
               + config.CANCEL_FEE_PER_SHARE * max(0, order.remaining))
        if fee <= 0:
            return
        portfolio = self.portfolios.get(team_id)
        if portfolio is None:
            return
        portfolio.cash -= fee
        portfolio.total_fees_paid += fee
        self.exchange_revenue += fee

    def latency_for(self, team_id: str | None) -> float:
        """Outbound delay in seconds for one connection (0 = immediate).

        Observers and the teacher are never delayed — the dashboard must show
        the truth as it happens.
        """
        if not team_id or not self.scenario.flag("latency_enabled"):
            return 0.0
        if team_id in self.teacher_clients:
            return 0.0
        portfolio = self.portfolios.get(team_id)
        if portfolio is None or portfolio.role == "observer":
            return 0.0
        return limits.latency_seconds(
            config.config_for_team(team_id, "latency_ms"))

    def position_limit_for(self, team_id: str) -> int:
        """Per-symbol position cap for one bot (0 = unlimited).

        The base value is the week scenario's override or the config default;
        Phase 2 upgrades raise it for teams that bought the increase.
        """
        return config.config_for_team(team_id, "position_limit")

    def _bidask_marks(self) -> dict[str, tuple]:
        """symbol → (best_bid, best_ask) for conservative marking, or {}."""
        if not config.CONSERVATIVE_MARKS:
            return {}
        return {
            sym: (book.best_bid(), book.best_ask())
            for sym, book in self.books.items()
        }

    async def _try_risk_shield(self, portfolio: Portfolio, nw: float) -> bool:
        """Spend the team's margin-call insurance instead of liquidating.

        The `risk_shield` upgrade is a one-time policy, held per TEAM (not per
        bot): the first book of the team to breach maintenance consumes it. The
        position is left completely untouched and the team gets
        RISK_SHIELD_GRACE_TICKS to fix it themselves — without a grace period a
        waiver would buy exactly one tick, since the next maintenance check
        would find the same book and the shield would already be spent. The
        roster records "used", so a restart cannot resurrect it and the second
        margin call is real.

        Returns True when the liquidation was waived.
        """
        team = config.team_of(portfolio.team_id)
        if not team or not upgrades.shield_active(team):
            return False
        if not upgrades.consume(team, "risk_shield"):
            return False        # roster write failed — liquidate rather than
                                # silently hand out a free pass
        portfolio.shield_until_tick = self.tick + config.RISK_SHIELD_GRACE_TICKS
        logger.warning("RISK SHIELD spent by %s (%s) net_worth=%.2f",
                       team, portfolio.team_id, nw)
        await self._broadcast(SessionEvent(
            event="RISK_SHIELD",
            message=(f"{portfolio.team_id} hit maintenance margin — "
                     f"{team}'s margin-call insurance waived the liquidation. "
                     f"{config.RISK_SHIELD_GRACE_TICKS} ticks to fix it; the "
                     f"policy is now spent."),
            data={"team_id": portfolio.team_id, "team": team,
                  "net_worth": round(nw, 2),
                  "grace_ticks": config.RISK_SHIELD_GRACE_TICKS},
        ))
        ws = self.clients.get(portfolio.team_id)
        if ws:
            await self._send(ws, portfolio.to_message(self.ref_prices))
        return True

    async def _liquidate(self, portfolio: Portfolio, nw: float) -> None:
        """Force-flatten a team below maintenance: close every position at
        market (through the mark by LIQUIDATION_PENALTY) and bar trading."""
        portfolio.liquidated = True
        for sym, qty in list(portfolio.positions.items()):
            if not qty:
                continue
            ref = self.ref_prices.get(sym, portfolio.avg_cost.get(sym, 0.0))
            # Longs are sold below the mark, shorts covered above it.
            px = ref * (1 - config.LIQUIDATION_PENALTY) if qty > 0 \
                 else ref * (1 + config.LIQUIDATION_PENALTY)
            if config.is_future(sym):
                # Closing a future settles the variation at the penalty price;
                # there is no notional to credit.
                portfolio.settle_future(sym, px)
            else:
                portfolio.cash += qty * px
                portfolio.realized_pnl += (
                    px - portfolio.avg_cost.get(sym, px)) * qty
            portfolio.positions[sym] = 0
        # Pull the team's resting orders off every book.
        for book in self.books.values():
            book.cancel_team_orders(portfolio.team_id)
        logger.warning("LIQUIDATED %s  net_worth=%.2f < maintenance", portfolio.team_id, nw)
        await self._broadcast(SessionEvent(
            event="LIQUIDATION",
            message=(f"{portfolio.team_id} fell below maintenance margin "
                     f"and was force-liquidated"),
            data={"team_id": portfolio.team_id, "net_worth": round(nw, 2)},
        ))
        ws = self.clients.get(portfolio.team_id)
        if ws:
            await self._send(ws, portfolio.to_message(self.ref_prices))

    # ------------------------------------------------------------------
    # Teacher controls (called interactively from CLI or programmatically)
    # ------------------------------------------------------------------

    async def open_session(self) -> None:
        """Open the trading session — all connected bots activate.

        Idempotent: a second START while open (or opening) is a no-op.
        With OPENING_AUCTION_TICKS > 0, START first enters a pre-open:
        limit orders rest without matching, indicative price/imbalance
        broadcasts each tick, then one single-price cross opens the market.
        """
        if self.session_open or self.auction_phase:
            logger.info("open_session ignored — session already open/opening")
            return
        if config.OPENING_AUCTION_TICKS > 0:
            self.auction_phase = "preopen"
            self.auction_ticks_left = config.OPENING_AUCTION_TICKS
            for book in self.books.values():
                book.auction_mode = True
            # Shares are granted at the PRE-open: the opening auction only
            # means something if participants have inventory to sell into it.
            await self._grant_starting_shares()
            logger.info("━━━  PRE-OPEN  ━━━  opening cross in %d ticks",
                        self.auction_ticks_left)
            await self._broadcast(SessionEvent(
                event="SESSION_PREOPEN",
                message=(f"Pre-open: limit orders only — the opening "
                         f"auction crosses in {self.auction_ticks_left} "
                         f"ticks"),
                data={"symbols": list(self.books.keys()),
                      "ticks": self.auction_ticks_left},
            ))
            return
        await self._complete_open()

    async def _grant_starting_shares(self) -> None:
        """Give every trading participant the session's starting shares.

        Runs at most once per session, whichever path (pre-open or direct
        open) reaches it first. Observers never trade — granting them
        shares only distorts the visible share supply.
        """
        if self._session_granted:
            return
        self._session_granted = True
        n = config.STARTING_SHARES_PER_SYMBOL
        if n <= 0:
            return
        for portfolio in self.portfolios.values():
            if portfolio.role == "observer":
                continue
            for sym in self.books:
                # Futures are contracts, not shares — nothing to grant.
                if config.is_future(sym):
                    continue
                portfolio.positions[sym] = portfolio.positions.get(sym, 0) + n
                if sym not in portfolio.avg_cost:
                    portfolio.avg_cost[sym] = self.ref_prices.get(sym, 0.0)
            ws = self.clients.get(portfolio.team_id)
            if ws:
                await self._send(ws, portfolio.to_message(self.ref_prices))
        logger.info("Starting shares distributed: %d per symbol to %d participants",
                    n, len(self.portfolios))

    async def _run_opening_cross(self) -> None:
        """Cross every book at its clearing price, then open the market."""
        results: dict[str, dict] = {}
        for sym, book in self.books.items():
            book.auction_mode = False
            trades = book.auction_execute()
            if trades:
                results[sym] = {"price": trades[-1].price,
                                "volume": sum(t.quantity for t in trades)}
            for trade in trades:
                await self._process_trade(trade)
        self.auction_phase = None
        if results:
            logger.info("OPENING CROSS  %s",
                        "  ".join(f"{s} {r['volume']}@{r['price']:.2f}"
                                  for s, r in results.items()))
        await self._complete_open()
        await self._broadcast(SessionEvent(
            event="AUCTION_RESULT",
            message="Opening auction complete — continuous trading begins",
            data={"phase": "open", "symbols": results},
        ))

    async def _run_closing_cross(self) -> None:
        """Cross every book once, then finish the deferred close."""
        results: dict[str, dict] = {}
        for sym, book in self.books.items():
            book.auction_mode = False
            trades = book.auction_execute()
            if trades:
                results[sym] = {"price": trades[-1].price,
                                "volume": sum(t.quantity for t in trades)}
            for trade in trades:
                await self._process_trade(trade)
        if results:
            logger.info("CLOSING CROSS  %s",
                        "  ".join(f"{s} {r['volume']}@{r['price']:.2f}"
                                  for s, r in results.items()))
        await self._broadcast(SessionEvent(
            event="AUCTION_RESULT",
            message="Closing auction complete",
            data={"phase": "close", "symbols": results},
        ))
        # auction_phase is still "preclose" here ON PURPOSE: close_session
        # only opens the pre-close window when the phase is None, so this
        # call falls through to the real close (which resets the phase).
        await self.close_session(persist=self._pending_close_persist)

    async def _complete_open(self) -> None:
        self.session_open = True
        self.session_open_tick = self.tick
        self.ssr_active.clear()       # a new day clears the short-sale rule
        self._last_season_save = time.time()
        self._start_recording()
        for engine in self.price_engines.values():
            engine.session_open = engine.market_price

        await self._grant_starting_shares()

        # First snapshot of the session: the baseline every season return is
        # measured from (taken after the share grant, so the free inventory
        # is not counted as profit).
        self.snapshot_equity(force=True)
        self._capture_sod()

        # Publish the week's calendar. Timing is announced up front; direction
        # is resolved only when each event fires.
        self.calendar.reset()
        await self._announce_calendar()

        # IPOs this week. Issuance is primary-market business: every venue
        # loads the same scenario, so without a gate each student venue
        # would independently book, price and mint the same deal — a bot on
        # two venues would be allocated (and pay) twice. The primary runs
        # the full lifecycle; a secondary venue tracks the deal silently
        # and simply LISTS the security at list-tick (no book, no shares,
        # no cash) so the name trades everywhere — arbitrage, not the
        # issuer, keeps the venues' prints coherent.
        self.ipos = {}
        self.ipo_issuance = config.is_primary_venue()
        for ev in self.scenario.events:
            if ev.get("kind") != "ipo":
                continue
            deal = ipo_mod.IPOOffering.from_event(ev)
            if deal is None or deal.symbol in self.books:
                continue           # already listed in an earlier session
            if deal.symbol in self.listed_ipos:
                continue           # done in a prior season session (durable)
            self.ipos[deal.symbol] = deal
            if not self.ipo_issuance:
                logger.info("IPO %s: secondary venue — will list at t%d "
                            "without issuance", deal.symbol, deal.list_tick)
                continue
            logger.info("IPO ANNOUNCED  %s (%s): %d shares, "
                        "$%.2f–%.2f, book t%d–t%d, lists t%d",
                        deal.symbol, deal.name, deal.shares, deal.range_lo,
                        deal.range_hi, deal.open_tick, deal.close_tick,
                        deal.list_tick)
            await self._broadcast(SessionEvent(
                event="IPO_ANNOUNCE",
                message=(f"IPO: {deal.name} ({deal.symbol}) — "
                         f"{deal.shares:,} shares at "
                         f"${deal.range_lo:.2f}–{deal.range_hi:.2f}; book "
                         f"opens t{deal.open_tick}, lists t{deal.list_tick}"),
                data=deal.to_public(),
            ))

        logger.info("━━━  SESSION OPEN  ━━━  week %d (%s)",
                    self.scenario.week, self.scenario.label)
        await self._broadcast(SessionEvent(
            event="SESSION_OPEN",
            message="Trading session is now OPEN. Bots activate!",
            data={"symbols": list(self.books.keys()), "tick": self.tick,
                  "starting_shares": config.STARTING_SHARES_PER_SYMBOL,
                  "week": self.scenario.week,
                  "scenario": self.scenario.to_dict()},
        ))

    def _capture_sod(self) -> None:
        """Freeze the start-of-day baseline the EOD reconciliation diffs against.

        Taken at the completed open — AFTER the share grant — so the day's
        report attributes only what the day itself did.
        """
        self.midsession_grants = {}
        self.sod = {}
        for bot, pf in self.portfolios.items():
            if pf.role == "observer":
                continue
            st = self.part_stats[bot]
            self.sod[bot] = {
                "positions": dict(pf.positions),
                "cash": pf.cash,
                "net_worth": pf.net_worth(self.ref_prices),
                "realized": pf.realized_pnl,
                "fees": pf.total_fees_paid,
                "rebates": pf.total_rebates_earned,
                "carry": pf.total_carry_paid,
                "shares_bought": st["shares_bought"],
                "shares_sold": st["shares_sold"],
                "volume": st["volume"],
                "trade_count": st["trade_count"],
            }
        self.sod_market_shares = sum(
            b._total_volume for b in self.books.values())
        self.sod_money = {
            "dividends_net": self.dividends_net,
            "interest_credited": self.interest_credited,
            "seat_debits": self.seat_debits,
            "exchange_revenue": self.exchange_revenue,
            "ipo_proceeds": self.ipo_proceeds,
        }
        self.sod_ipo_issued = dict(self.ipo_issued)

    async def _write_eod_report(self) -> None:
        """The end-of-day reconciliation: positions, flows, P&L, identities.

        Three identities must hold, or the engine minted/destroyed something:

          1. Σ shares bought = Σ shares sold = market volume — every share
             bought was sold by someone, and the venue counted each print once.
          2. Σ EOD positions − Σ SOD positions = mid-session grants plus
             IPO issuance, per symbol — trades only MOVE inventory; only
             grants and primary-market allocations create it.
          3. Per team: ΔNW = Δrealized + Δunrealized − Δfees + Δrebates
             − Δcarry + other cash flows (dividends/interest/variation) —
             reported per team so an attribution gap is visible.
        """
        rows = []
        tot_bought = tot_sold = 0
        sod_pos_by_sym: dict[str, int] = {}
        eod_pos_by_sym: dict[str, int] = {}
        for bot, pf in self.portfolios.items():
            if pf.role == "observer":
                continue
            base = self.sod.get(bot) or {
                "positions": {}, "cash": pf.starting_cash, "net_worth":
                pf.starting_cash, "realized": 0.0, "fees": 0.0,
                "rebates": 0.0, "carry": 0.0, "shares_bought": 0,
                "shares_sold": 0, "volume": 0.0, "trade_count": 0}
            st = self.part_stats[bot]
            bought = st["shares_bought"] - base["shares_bought"]
            sold = st["shares_sold"] - base["shares_sold"]
            tot_bought += bought
            tot_sold += sold
            for sym, q in base["positions"].items():
                sod_pos_by_sym[sym] = sod_pos_by_sym.get(sym, 0) + q
            for sym, q in pf.positions.items():
                eod_pos_by_sym[sym] = eod_pos_by_sym.get(sym, 0) + q
            eod_nw = pf.net_worth(self.ref_prices)
            rows.append({
                "bot": bot, "role": pf.role,
                "sod_positions": {k: v for k, v in
                                  base["positions"].items() if v},
                "eod_positions": {k: v for k, v in pf.positions.items() if v},
                "shares_bought": bought, "shares_sold": sold,
                "share_volume": bought + sold,
                "notional_volume": round(st["volume"] - base["volume"], 2),
                "trades": st["trade_count"] - base["trade_count"],
                "sod_net_worth": round(base["net_worth"], 2),
                "eod_net_worth": round(eod_nw, 2),
                "day_pnl": round(eod_nw - base["net_worth"], 2),
                "realized_delta": round(pf.realized_pnl - base["realized"], 2),
                "eod_unrealized": round(pf.unrealized_pnl(self.ref_prices), 2),
                "fees_delta": round(pf.total_fees_paid - base["fees"], 2),
                "rebates_delta": round(
                    pf.total_rebates_earned - base["rebates"], 2),
                "carry_delta": round(pf.total_carry_paid - base["carry"], 2),
            })
        rows.sort(key=lambda r: -r["day_pnl"])

        market_shares = sum(
            b._total_volume for b in self.books.values()) - self.sod_market_shares

        # Identity 1: buys = sells = the venue's own count of shares printed.
        volume_ok = (tot_bought == tot_sold == market_shares)
        # Identity 2: inventory is conserved up to mid-session grants.
        symbols = set(sod_pos_by_sym) | set(eod_pos_by_sym)
        pos_breaks = {}
        for sym in symbols:
            issued = (self.ipo_issued.get(sym, 0)
                      - getattr(self, "sod_ipo_issued", {}).get(sym, 0))
            drift = (eod_pos_by_sym.get(sym, 0) - sod_pos_by_sym.get(sym, 0)
                     - self.midsession_grants.get(sym, 0) - issued)
            if drift:
                pos_breaks[sym] = drift
        # Identity 3: money conservation. Every dollar of team-cash change
        # is explained by named flows; anything else was minted or burned.
        money_base = getattr(self, "sod_money", None) or {
            "dividends_net": 0.0, "interest_credited": 0.0,
            "seat_debits": 0.0, "exchange_revenue": 0.0,
            "ipo_proceeds": 0.0}
        money_base.setdefault("ipo_proceeds", 0.0)
        cash_delta = 0.0
        carry_delta = 0.0
        for bot, pf in self.portfolios.items():
            if pf.role == "observer":
                continue
            base = self.sod.get(bot)
            if base is None:
                # Bot funded mid-session: its funding is not a day flow —
                # diff against its own funded starting cash.
                cash_delta += pf.cash - pf.starting_cash
                carry_delta += pf.total_carry_paid
            else:
                cash_delta += pf.cash - base["cash"]
                carry_delta += pf.total_carry_paid - base["carry"]
        explained = (
            (money_base["exchange_revenue"] - self.exchange_revenue)  # fees out
            + (self.dividends_net - money_base["dividends_net"])
            + (self.interest_credited - money_base["interest_credited"])
            - carry_delta
            - (self.seat_debits - money_base["seat_debits"])
            - (self.ipo_proceeds - money_base["ipo_proceeds"]))
        cash_gap = round(cash_delta - explained, 2)
        cash_ok = abs(cash_gap) <= 0.01 * max(1, len(rows))

        checks = {
            "cash_conservation": {
                "ok": cash_ok,
                "sum_team_cash_delta": round(cash_delta, 2),
                "explained_by_flows": round(explained, 2),
                "unexplained_gap": cash_gap,
                "flows": {
                    "fees_to_venue": round(
                        self.exchange_revenue
                        - money_base["exchange_revenue"], 2),
                    "dividends_net": round(
                        self.dividends_net - money_base["dividends_net"], 2),
                    "interest_credited": round(
                        self.interest_credited
                        - money_base["interest_credited"], 2),
                    "carry_burned": round(carry_delta, 2),
                    "seat_capital_in_flight": round(
                        self.seat_debits - money_base["seat_debits"], 2),
                    "ipo_proceeds_to_issuer": round(
                        self.ipo_proceeds - money_base["ipo_proceeds"], 2),
                },
            },
            "volume_identity": {
                "ok": volume_ok,
                "sum_shares_bought": tot_bought,
                "sum_shares_sold": tot_sold,
                "exchange_market_volume": market_shares,
            },
            "position_conservation": {
                "ok": not pos_breaks,
                "midsession_grants": dict(self.midsession_grants),
                "ipo_issued": {k: v - getattr(self, "sod_ipo_issued", {})
                               .get(k, 0) for k, v in self.ipo_issued.items()
                               if v - getattr(self, "sod_ipo_issued", {})
                               .get(k, 0)},
                "breaks": pos_breaks,     # symbol → unexplained share drift
            },
        }
        report = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "week": self.scenario.week,
            "label": self.scenario.label,
            "tick": self.tick,
            "trades": self.trade_count,
            "checks": checks,
            "teams": rows,
        }

        # REPORTS_DIR so containers can point at the mounted volume; the
        # port in the name so venues closing in the same second (multi-venue
        # weeks) never interleave writes into one corrupt file.
        out_dir = pathlib.Path(
            os.environ.get("REPORTS_DIR", str(pathlib.Path("data") / "reports")))
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_p{config.PORT}"
        (out_dir / f"eod_{stamp}.json").write_text(
            json.dumps(report, indent=2) + "\n")

        status = ("PASS" if volume_ok and not pos_breaks and cash_ok
                  else "FAIL")
        logger.info(
            "EOD RECON %s  bought=%d sold=%d market=%d  pos_breaks=%s  "
            "cash_gap=$%.2f  → %s",
            status, tot_bought, tot_sold, market_shares,
            pos_breaks or "none", cash_gap, out_dir / f"eod_{stamp}.json")
        for teacher in self.teacher_clients:
            tws = self.clients.get(teacher)
            if tws:
                await self._send(tws, SessionEvent(
                    event="EOD_REPORT",
                    message=(f"EOD reconciliation {status}: "
                             f"{tot_bought} bought = {tot_sold} sold = "
                             f"{market_shares} printed"),
                    data={"checks": checks, "teams": rows[:20]},
                ))

    async def teacher_announce_ipo(self, params: dict) -> tuple[bool, str]:
        """Launch an ad-hoc deal mid-session (teacher command).

        Ticks are relative to session open, the same clock the scenario
        events use — a window already in the past simply opens on the next
        tick. Primary venue only, like every other issuance path.
        """
        if not self.session_open:
            return False, "no session open"
        if not config.is_primary_venue():
            return False, "IPOs are announced at the primary venue only"
        deal = ipo_mod.IPOOffering.from_event(
            {"symbol": params.get("symbol"),
             "name": params.get("name", params.get("symbol")),
             "shares": params.get("shares"),
             "offer_range": [params.get("range_lo"), params.get("range_hi")],
             "window": [params.get("open_tick"), params.get("close_tick")],
             "tick": params.get("list_tick")})
        if deal is None:
            return False, ("params required: symbol, shares, range_lo, "
                           "range_hi, open_tick, close_tick, list_tick")
        if (deal.shares <= 0 or deal.range_lo <= 0
                or deal.range_hi < deal.range_lo
                or not deal.open_tick < deal.close_tick <= deal.list_tick):
            return False, "shares/range/window make no sense"
        if (deal.symbol in self.books or deal.symbol in self.ipos
                or deal.symbol in self.listed_ipos):
            return False, f"{deal.symbol} already exists or already dealt"
        self.ipos[deal.symbol] = deal
        logger.info("IPO ANNOUNCED (teacher)  %s: %d shares, $%.2f–%.2f",
                    deal.symbol, deal.shares, deal.range_lo, deal.range_hi)
        await self._broadcast(SessionEvent(
            event="IPO_ANNOUNCE",
            message=(f"IPO: {deal.name} ({deal.symbol}) — "
                     f"{deal.shares:,} shares at "
                     f"${deal.range_lo:.2f}–{deal.range_hi:.2f}; book "
                     f"opens t{deal.open_tick}, lists t{deal.list_tick}"),
            data=deal.to_public(),
        ))
        return True, f"{deal.symbol} announced"

    async def teacher_cancel_ipo(self, symbol: str) -> tuple[bool, str]:
        """Pull a deal (teacher command). Only before pricing — once cash
        has moved the deal is history, not an intention."""
        deal = self.ipos.get(symbol)
        if deal is None:
            return False, f"no live IPO for {symbol!r}"
        if deal.state not in ("announced", "open"):
            return False, (f"{symbol} is already {deal.state} — "
                           f"cash has moved, cannot cancel")
        del self.ipos[symbol]
        logger.info("IPO CANCELLED  %s (was %s)", symbol, deal.state)
        await self._broadcast(SessionEvent(
            event="IPO_CANCELLED",
            message=f"The {deal.name} ({symbol}) offering has been withdrawn.",
            data=dict(deal.to_public(), state="cancelled"),
        ))
        return True, f"{symbol} cancelled"

    async def _advance_ipos(self) -> None:
        rel = self.tick - self.session_open_tick
        if not getattr(self, "ipo_issuance", True):
            # Secondary venue: no book, no allocation, no minting — just
            # list the name at list-tick so it trades here too. The listing
            # anchor is the range midpoint; the primary's print and the
            # arbitrageurs converge the venues within a few ticks.
            for deal in list(self.ipos.values()):
                if deal.state != "listed" and rel >= deal.list_tick:
                    deal.offer_price = round(
                        (deal.range_lo + deal.range_hi) / 2, 2)
                    await self._list_ipo(deal)
            return
        for deal in list(self.ipos.values()):
            if deal.state == "announced" and rel >= deal.open_tick:
                deal.state = "open"
                logger.info("IPO BOOK OPEN  %s until t%d",
                            deal.symbol, deal.close_tick)
                await self._broadcast(SessionEvent(
                    event="IPO_OPEN",
                    message=(f"{deal.symbol} book is OPEN — subscribe with "
                             f"quantity and max price "
                             f"(${deal.range_lo:.2f}–{deal.range_hi:.2f}) "
                             f"before t{deal.close_tick}"),
                    data=deal.to_public(),
                ))
            elif deal.state == "open" and rel >= deal.close_tick:
                await self._price_ipo(deal)
            elif deal.state == "priced" and rel >= deal.list_tick:
                await self._list_ipo(deal)

    async def _price_ipo(self, deal) -> None:
        """Close the book: price, allocate pro-rata, debit the cash."""
        deal.price_and_allocate(
            lambda bot: self.portfolios[bot].cash
            if bot in self.portfolios else 0.0)
        offer = deal.offer_price
        placed = 0
        for bot, qty in deal.allocations.items():
            pf = self.portfolios.get(bot)
            if pf is None:
                continue
            cost = qty * offer
            pf.cash -= cost
            pf.positions[deal.symbol] = (
                pf.positions.get(deal.symbol, 0) + qty)
            pf.avg_cost[deal.symbol] = offer
            self.ipo_proceeds += cost
            self.ipo_allocations[bot] = (
                self.ipo_allocations.get(bot, 0) + qty)
            placed += qty
            ws = self.clients.get(bot)
            if ws:
                await self._send(ws, pf.to_message(self.ref_prices))
        self.ipo_issued[deal.symbol] = (
            self.ipo_issued.get(deal.symbol, 0) + placed)
        # Durable from the moment money moves: cash and positions are
        # committed above, so a restart between pricing and listing must
        # still restore the security (and must never re-run the deal).
        self.listed_ipos[deal.symbol] = {
            "name": deal.name,
            "offer_price": offer,
            "week": self.scenario.week,
            "listed": False,
        }
        subscribed = sum(q for q, _ in deal.subs.values())
        logger.info("IPO PRICED  %s at $%.2f — %d subscribed, %d placed "
                    "(%d bidders)", deal.symbol, offer, subscribed, placed,
                    len(deal.allocations))
        await self._broadcast(SessionEvent(
            event="IPO_PRICED",
            message=(f"{deal.symbol} priced at ${offer:.2f} — "
                     f"{subscribed:,} shares subscribed for "
                     f"{deal.shares:,} offered; lists t{deal.list_tick}"),
            data=deal.to_public(),
        ))

    def _register_listed_security(self, sym: str, name: str,
                                  offer: float, market_price: float) -> None:
        """Register an IPO'd security into the live venue.

        Shared by listing day (market starts AT the offer) and season
        restore (market resumes at the last checkpointed price). The
        fundamental is redrawn from the deterministic pop, so every process
        that ever lists `sym` agrees on where the truth sits.
        """
        true_value = round(offer * ipo_mod.pop_factor(sym), 2)
        import plugins.securities.defaults as securities
        self.registry.register_security(
            sym, name, "equity", true_value, "#f0abfc",
            securities.make_fundamental(sym, sigma=0.7))
        self.registry.prices[sym] = true_value
        self.books[sym] = OrderBook(sym, fee_rate=config.FEE_RATE,
                                    tick_size=config.TICK_SIZE,
                                    stp_key=self._stp_key)
        engine = PriceEngine(sym, true_value)
        engine.market_price = market_price
        engine.session_open = market_price   # SSR/session-band baseline
        self.price_engines[sym] = engine
        self.ref_prices[sym] = market_price

    async def _list_ipo(self, deal) -> None:
        """Create the security, book and engine; trading starts NOW.

        The market price starts AT the offer while the fundamental sits at
        the (hidden) true value: the tick-capped engine walks the price
        toward the truth over the next minutes, and the first prints decide
        who captured the pop.
        """
        sym = deal.symbol
        offer = deal.offer_price or deal.range_lo
        self._register_listed_security(sym, deal.name, offer, offer)
        deal.state = "listed"
        rec = self.listed_ipos.setdefault(
            sym, {"name": deal.name, "offer_price": offer,
                  "week": self.scenario.week})
        rec["listed"] = True
        logger.info("IPO LISTED  %s at $%.2f offer (true value hidden)",
                    sym, offer)
        await self._broadcast(SessionEvent(
            event="IPO_LISTED",
            message=(f"{sym} is now trading — offered at ${offer:.2f}. "
                     f"Where it belongs is yours to discover."),
            data=deal.to_public(),
        ))
        await self._broadcast(self._make_snapshot(sym, self.books[sym]))

    async def _handle_ipo_subscribe(
        self, ws: Any, msg: IPOSubscribe, team_id: str
    ) -> None:
        """An indication of interest — from a bot or the portal relay."""
        bot = msg.team_id
        if team_id in self.teacher_clients:
            # Portal path: the dashboard verified the team token; the
            # exchange re-validates that the bot belongs to that team.
            if bot not in upgrades.team_bots(msg.team):
                await self._send(ws, SessionEvent(
                    event="IPO_SUB_RESULT",
                    message=f"{bot!r} is not one of {msg.team}'s bots",
                    data={"ok": False, "request_id": msg.request_id,
                          "error": f"{bot!r} is not one of your bots"}))
                return
        elif bot != team_id:
            await self._send(ws, ErrorMsg(
                code="TEAM_MISMATCH",
                message="team_id does not match your authenticated identity"))
            return

        deal = self.ipos.get(msg.symbol)
        if deal is None:
            ok, detail = False, f"no IPO for {msg.symbol!r}"
        else:
            px = msg.max_price if msg.max_price > 0 else deal.range_hi
            ok, detail = deal.subscribe(bot, msg.quantity, px)
        if team_id in self.teacher_clients:
            await self._send(ws, SessionEvent(
                event="IPO_SUB_RESULT", message=detail,
                data={"ok": ok, "request_id": msg.request_id,
                      "detail": detail,
                      **({} if ok else {"error": detail})}))
        elif ok:
            await self._send(ws, SessionEvent(
                event="IPO_SUB_RESULT", message=detail,
                data={"ok": True, "symbol": msg.symbol}))
        else:
            await self._reject(ws, bot, ErrorMsg(
                code="IPO_REJECTED", message=detail))

    async def close_session(self, persist: bool = True) -> None:
        """Close the session and broadcast the final leaderboard.

        `persist` writes the season checkpoint. The teacher's `end_session`
        command is the explicit "bank this week" action; plain
        `close_session` keeps working as it always has.
        """
        # Closing auction: mirror of the pre-open. The first close request
        # freezes continuous matching for CLOSING_AUCTION_TICKS — orders
        # rest into the closing book, the indicative cross broadcasts — and
        # advance_tick runs the cross, then finishes the close for real.
        if (config.CLOSING_AUCTION and self.session_open
                and self.auction_phase is None):
            self.auction_phase = "preclose"
            self.auction_ticks_left = config.CLOSING_AUCTION_TICKS
            self._pending_close_persist = persist
            for book in self.books.values():
                book.auction_mode = True
            # Inject held MOC orders into the closing books as always-
            # eligible limits: an MOC takes the clearing price, whatever
            # it is, so its limit is far beyond any plausible cross.
            for sym, pending in self.moc_orders.items():
                book = self.books.get(sym)
                ref = self.ref_prices.get(sym) or 0.0
                if book is None or ref <= 0:
                    continue
                for entry in pending.values():
                    px = ref * (1.5 if entry["side"] == "buy" else 0.5)
                    book.place_order(team_id=entry["team_id"],
                                     side=entry["side"], price=px,
                                     quantity=entry["quantity"])
            self.moc_orders.clear()
            logger.info("━━━  PRE-CLOSE  ━━━  closing cross in %d ticks",
                        self.auction_ticks_left)
            await self._broadcast(SessionEvent(
                event="SESSION_PRECLOSE",
                message=(f"Closing auction: limit orders only — the market "
                         f"closes on the cross in "
                         f"{self.auction_ticks_left} ticks"),
                data={"ticks": self.auction_ticks_left},
            ))
            return

        was_open = self.session_open
        if was_open:
            try:
                await self._write_eod_report()
            except Exception:
                logger.exception("EOD report failed — session close continues")
        self.session_open = False
        self.auction_phase = None
        self.auction_ticks_left = 0
        self._session_granted = False
        for book in self.books.values():
            book.auction_mode = False
        self.stop_orders.clear()      # stops are day orders
        self.moc_orders.clear()
        if was_open:
            self.snapshot_equity(force=True)
            self.sessions_played += 1
        logger.info("━━━  SESSION CLOSED  ━━━  trades=%d", self.trade_count)
        await self._broadcast(SessionEvent(
            event="SESSION_CLOSED",
            message="Trading session is now CLOSED. Flatten positions.",
            data={"tick": self.tick, "total_trades": self.trade_count,
                  "week": self.scenario.week},
        ))
        await self._broadcast(self._build_leaderboard())
        self._stop_recording()
        if persist and self.season_persists:
            self.save_season()

    async def end_session(self) -> None:
        """Close the session and explicitly bank the season state."""
        await self.close_session(persist=True)
        logger.info("Season checkpointed → %s", persistence.SEASON_PATH)
        await self._broadcast(SessionEvent(
            event="SEASON_SAVED",
            message=f"Week {self.scenario.week} banked to the season file",
            data={"week": self.scenario.week,
                  "sessions_played": self.sessions_played},
        ))

    # ------------------------------------------------------------------
    # Season persistence and scoring
    # ------------------------------------------------------------------

    @property
    def season_persists(self) -> bool:
        """Whether season state is read from and written to disk.

        Defaults to "only when a week is configured", so plain local play
        (`make exchange` with no env vars) stays ephemeral exactly as it was
        before the season system existed.
        """
        mode = config.SEASON_PERSIST
        if mode in ("true", "1", "yes"):
            return True
        if mode in ("false", "0", "no"):
            return False
        return self.scenario.week > 0

    def load_season(self) -> bool:
        """Restore portfolios, tick, revenue and equity history from disk.

        Called once at startup. Returns True if a season file was loaded.
        Positions come back as they were, so a team that closed a week short
        NVDA opens the next week short NVDA — the whole point of a season.
        """
        if not self.season_persists:
            return False
        state = persistence.load()
        if not state:
            return False
        try:
            for tid, raw in (state.get("portfolios") or {}).items():
                self.portfolios[tid] = persistence.portfolio_from_dict(
                    Portfolio, raw)
            self.equity_history = {
                tid: [(int(t), float(v)) for t, v in hist]
                for tid, hist in (state.get("equity_history") or {}).items()
            }
            self.tick = int(state.get("tick", 0))
            self.exchange_revenue = float(state.get("exchange_revenue", 0.0))
            self.sessions_played = int(state.get("sessions_played", 0))
            ipo_state = state.get("ipo") or {}
            self.ipo_issued = {str(k): int(v) for k, v in
                               (ipo_state.get("issued") or {}).items()}
            self.ipo_proceeds = float(ipo_state.get("proceeds", 0.0))
            self.ipo_allocations = {str(k): int(v) for k, v in
                                    (ipo_state.get("allocations") or {}).items()}
            self.listed_ipos = {}
            for sym, raw in (ipo_state.get("listed") or {}).items():
                rec = {"name": str(raw.get("name", sym)),
                       "offer_price": float(raw.get("offer_price", 0.0)),
                       "week": int(raw.get("week", 0)),
                       "listed": bool(raw.get("listed", True))}
                self.listed_ipos[str(sym)] = rec
                if str(sym) in self.books:
                    continue
                # Re-register the security so the positions students carried
                # out of the IPO week stay tradeable and marked. Trading
                # resumes at the last checkpointed price, not the offer.
                last = float(raw.get("last_price") or rec["offer_price"])
                self._register_listed_security(
                    str(sym), rec["name"], rec["offer_price"], last)
                logger.info("Season restore: relisted %s at $%.2f "
                            "(IPO week %d)", sym, last, rec["week"])
        except (TypeError, ValueError) as exc:
            logger.warning("Season file malformed (%s) — starting fresh", exc)
            return False
        self._season_dirty = True
        logger.info(
            "Season restored: %d portfolios, tick %d, %d sessions played",
            len(self.portfolios), self.tick, self.sessions_played,
        )
        return True

    def save_season(self) -> bool:
        """Checkpoint the season to disk (no-op when persistence is off)."""
        if not self.season_persists:
            return False
        ok = persistence.save(persistence.build_state(self))
        self._last_season_save = time.time()
        if ok:
            logger.debug("Season checkpointed (tick %d)", self.tick)
        return ok

    async def new_season(self) -> None:
        """Wipe all season state and start over.

        Deletes the season file, clears portfolios and equity history, resets
        revenue and the tick counter. Destructive and teacher-only — the
        dashboard puts it behind a confirmation dialog.
        """
        if self.session_open:
            await self.close_session()
        persistence.wipe()
        for tid in list(self.portfolios):
            for book in self.books.values():
                book.cancel_team_orders(tid)
        self.portfolios.clear()
        self.equity_history.clear()
        self.exchange_revenue = 0.0
        self.trade_log.clear()
        self.trade_count = 0
        self.part_stats.clear()
        self.tick = 0
        self.sessions_played = 0
        self._last_equity_snapshot_tick = 0
        self._season_dirty = True
        self.calendar.reset()
        # The primary market resets with the season: delist every IPO'd
        # security and forget the deals, or the durable done-once guard
        # would block the fresh season's IPO weeks forever.
        for sym in list(self.listed_ipos):
            self.books.pop(sym, None)
            self.price_engines.pop(sym, None)
            self.ref_prices.pop(sym, None)
            self.registry.unregister_security(sym)
        self.listed_ipos.clear()
        self.ipos.clear()
        self.ipo_issued.clear()
        self.ipo_allocations.clear()
        self.ipo_proceeds = 0.0
        logger.warning("━━━  NEW SEASON  ━━━  all season state wiped")
        await self._broadcast(SessionEvent(
            event="NEW_SEASON",
            message="A new season has started — all portfolios and scores reset",
            data={"week": self.scenario.week},
        ))
        await self._broadcast(self._build_leaderboard())

    def set_scenario(self, scen: Any) -> None:
        """Install a week's rule set: config overrides plus its calendar.

        The single seam for changing week, so the calendar can never end up
        out of step with the active scenario.
        """
        self.scenario.restore()
        self.scenario = scen
        self.scenario.apply()
        self.calendar.load(scen.events)
        self.calendar.reset()
        self._season_dirty = True

    async def set_week(self, week: int) -> None:
        """Switch the live rule set to another week's scenario."""
        try:
            scen = scenario_mod.load_week(week)
        except (OSError, ValueError) as exc:
            logger.error("Cannot load week %s: %s", week, exc)
            await self._broadcast(SessionEvent(
                event="WEEK_CHANGE_FAILED",
                message=f"No scenario file for week {week}",
                data={"week": week},
            ))
            return
        self.set_scenario(scen)
        logger.info("WEEK → %d (%s)", scen.week, scen.label)
        logger.info("  %s", scen.flag_summary())
        await self._broadcast(SessionEvent(
            event="WEEK_CHANGED",
            message=f"Week {scen.week}: {scen.label}",
            data=scen.to_dict(),
        ))
        await self._announce_calendar()
        await self._broadcast(self._build_leaderboard())

    def snapshot_equity(self, force: bool = False) -> None:
        """Append one equity point per team, at most every SNAPSHOT_TICKS.

        Only weeks with scoring_counts contribute, so week 1 stays paper.
        """
        if not self.scenario.scoring_counts:
            return
        every = max(1, config.SEASON_SNAPSHOT_TICKS)
        if not force and (self.tick - self._last_equity_snapshot_tick) < every:
            return
        self._last_equity_snapshot_tick = self.tick
        marks = self._bidask_marks()
        for tid, p in self.portfolios.items():
            if p.role == "observer":
                continue
            hist = self.equity_history.setdefault(tid, [])
            hist.append((self.tick, round(p.net_worth(self.ref_prices, marks), 4)))
            if len(hist) > persistence.EQUITY_HISTORY_MAX:
                del hist[:len(hist) - persistence.EQUITY_HISTORY_MAX]
        self._season_dirty = True

    async def inject_shock(self, shock_id: str, params: dict | None = None) -> None:
        """Apply a registered shock and notify all clients.

        The move is applied as a ramp with overshoot (see exchange/calendar.py)
        rather than an instant step, so momentum strategies have something real
        to catch. Set SHOCK_RAMP_TICKS=1 to restore the old instant behaviour.
        """
        try:
            result = self.registry.apply_shock(shock_id, params or {})
        except KeyError:
            logger.error("Unknown shock: %r", shock_id)
            return
        # Route the move through the ramp scheduler. apply_shock has already
        # written the target into registry.prices; the ramp walks the engine's
        # fair value there over the next few ticks.
        for sym, new_price in result.prices.items():
            engine = self.price_engines.get(sym)
            if engine is None:
                self.ref_prices[sym] = new_price
                continue
            start = engine.fair_value
            pct = (new_price / start - 1.0) if start > 0 else 0.0
            if self.calendar.ramp_ticks > 1 and abs(pct) > 1e-9:
                self.calendar.start_ramp(sym, start, pct, label=shock_id)
                # Hold the anchor at the pre-shock level; the ramp advances it.
                self.registry.prices[sym] = start
                if sym in self.registry.securities:
                    self.registry.securities[sym]["current_price"] = start
            else:
                engine.update_fair_value(new_price)
                self.ref_prices[sym] = new_price
        logger.info("SHOCK %s → %s", shock_id, result.message)
        await self._broadcast(SessionEvent(
            event="SHOCK",
            message=result.message,
            data={
                "shock_id": shock_id,
                "affected": result.affected,
                "prices": result.prices,
            },
        ))

    async def announce_fee_schedule(self, old: dict | None = None) -> None:
        """Re-read this venue's schedule and tell its clients about it.

        The roster is the single source of truth, so the portal writes it and
        then asks the venue (through the dashboard relay, exactly as an
        upgrade purchase travels) to pick it up and announce it. Bots whose
        edge depends on the fee — a cross-venue arbitrageur most of all — get
        the new numbers within a tick instead of discovering them from fills.
        """
        before = old or {"taker": config.TAKER_FEE_RATE,
                         "rebate": config.MAKER_REBATE_RATE}
        after = config.refresh_venue_fees(force=True)
        logger.info("Fee schedule → taker %.1f bps, rebate %.1f bps",
                    after["taker"] * 10_000, after["rebate"] * 10_000)
        await self._broadcast(SessionEvent(
            event="FEE_SCHEDULE",
            message=(f"Venue fee schedule: taker "
                     f"{after['taker'] * 10_000:.1f} bps, maker rebate "
                     f"{after['rebate'] * 10_000:.1f} bps"),
            data={
                "port": config.PORT,
                "taker": after["taker"],
                "rebate": after["rebate"],
                "net": round(after["taker"] - after["rebate"], 8),
                "old": {"taker": before["taker"], "rebate": before["rebate"]},
                "new": {"taker": after["taker"], "rebate": after["rebate"]},
            },
        ))

    async def set_fee_rate(self, rate: float) -> None:
        """Update the fee rate on all order books and notify clients."""
        config.FEE_RATE = rate
        for book in self.books.values():
            book.fee_rate = rate
        logger.info("Fee rate → %.4f", rate)
        await self._broadcast(SessionEvent(
            event="FEE_RATE_CHANGED",
            message=f"Fee rate updated to {rate:.4f} ({rate * 100:.2f}%)",
            data={"fee_rate": rate},
        ))

    async def lift_circuit_breakers(self) -> None:
        """Immediately clear all active halts and resume trading on every symbol."""
        self.circuit_breaker.market_halted = False
        resumed = []
        for symbol in list(self.books.keys()):
            if self.circuit_breaker.is_halted(symbol):
                self.circuit_breaker.resume_symbol(symbol)
                resumed.append(symbol)
        msg = f"All circuit breakers lifted by teacher ({len(resumed)} symbol(s) resumed)"
        logger.info(msg)
        await self._broadcast(SessionEvent(
            event="MARKET_RESUMED",
            message=msg,
            data={"symbols": resumed},
        ))

    # ------------------------------------------------------------------
    # Teacher CLI
    # ------------------------------------------------------------------

    async def _teacher_cli(self) -> None:
        """Read teacher commands from stdin, one per line."""
        _print_cli_help(self.registry.list_shocks())
        loop = asyncio.get_event_loop()

        while True:
            try:
                line: str = await loop.run_in_executor(None, sys.stdin.readline)
            except (EOFError, KeyboardInterrupt):
                break
            if not line:  # EOF (stdin closed)
                logger.info("Teacher CLI: stdin closed, CLI disabled. Ctrl+C to stop server.")
                break

            parts = line.strip().split()
            if not parts:
                continue
            cmd = parts[0].lower()

            if cmd == "open":
                await self.open_session()
                print("✓  Session opened — bots may now trade")

            elif cmd == "close":
                await self.close_session()
                print("✓  Session closed — final leaderboard broadcast")

            elif cmd == "shock":
                if len(parts) < 2:
                    print("Usage:  shock <id>   |   shock list")
                elif parts[1] == "list":
                    print(f"\n  {'ID':<24} {'Label':<22} Category")
                    print("  " + "─" * 58)
                    for s in self.registry.list_shocks():
                        print(f"  {s['id']:<24} {s['label']:<22} {s['category']}")
                    print()
                else:
                    shock_id = parts[1]
                    await self.inject_shock(shock_id)
                    print(f"✓  Shock {shock_id!r} applied")

            elif cmd == "fees":
                if len(parts) < 2:
                    print("Usage:  fees <rate>   (e.g.  fees 0.002)")
                else:
                    try:
                        rate = float(parts[1])
                        if not (0.0 <= rate <= 0.1):
                            print("Rate should be between 0 and 0.1 (10%)")
                        else:
                            await self.set_fee_rate(rate)
                            print(f"✓  Fee rate → {rate:.4f}  ({rate*100:.2f}%)")
                    except ValueError:
                        print("Invalid rate — use a decimal, e.g.  0.002")

            elif cmd == "status":
                self._print_status()

            elif cmd == "help":
                _print_cli_help(self.registry.list_shocks())

            elif cmd in ("quit", "exit", "q"):
                print("Shutting down exchange server...")
                os.kill(os.getpid(), signal.SIGINT)
                break

            else:
                print(f"Unknown command: {cmd!r}   (type 'help')")

    # ------------------------------------------------------------------
    # Circuit-breaker actions
    # ------------------------------------------------------------------

    async def _halt_symbol(self, symbol: str, reason: str, duration: float | None = None) -> None:
        """Halt trading on one symbol for HALT_DURATION_SEC seconds.

        1. Registers the halt with the circuit breaker.
        2. Cancels all resting orders for that symbol.
        3. Broadcasts a TRADING_HALT SessionEvent.
        4. Schedules auto-resume after the halt duration.
        """
        record = self.circuit_breaker.halt_symbol(symbol, reason, duration)
        logger.warning("HALT %s — %s (halt #%d)", symbol, reason, record["halt_num"])
        # Resting orders remain in the book but cannot be hit while halted
        # (new orders are blocked by the is_halted() check in _handle_place_order).

        await self._broadcast(SessionEvent(
            event="TRADING_HALT",
            message=f"Trading halted on {symbol}: {reason}",
            data={
                "symbol":     symbol,
                "reason":     reason,
                "duration":   record["duration"],
                "resume_at":  record["resume_at"],
                "halt_num":   record["halt_num"],
            },
        ))
        self._spawn(self._resume_symbol(symbol, record["duration"]))

    async def _resume_symbol(self, symbol: str, delay: float) -> None:
        """Re-enable trading on a symbol after the halt duration."""
        await asyncio.sleep(delay)
        resumed = self.circuit_breaker.resume_symbol(symbol)
        if resumed:
            logger.info("RESUME %s", symbol)
            await self._broadcast(SessionEvent(
                event="TRADING_RESUMED",
                message=f"Trading resumed on {symbol}",
                data={"symbol": symbol},
            ))

    async def _halt_all_symbols(self, reason: str, level: int) -> None:
        """Apply a market-wide halt (Levels 1-3).

        Level 3 closes the session entirely.
        Levels 1-2 halt all symbols for a fixed duration then auto-resume.
        """
        # Market-wide halt durations scale off the configured per-symbol halt
        # (default 300s → L1 15min, L2 60min, like real market-wide breakers).
        # Config-driven so the headless simulator can shrink them: hardcoded
        # wall-clock sleeps stalled a virtual-clock season for real minutes.
        durations = {1: config.HALT_DURATION_SEC * 3,
                     2: config.HALT_DURATION_SEC * 12}
        if level == 3:
            logger.warning("MARKET HALT L3 — closing session: %s", reason)
            await self.close_session()
            return

        duration = durations.get(level, config.HALT_DURATION_SEC)
        self.circuit_breaker.market_halted = True
        logger.warning("MARKET HALT L%d (%.0f s) — %s", level, duration, reason)
        await self._broadcast(SessionEvent(
            event="MARKET_HALT",
            message=f"Market-wide Level {level} halt: {reason}",
            data={"level": level, "reason": reason, "duration": duration},
        ))
        await asyncio.sleep(duration)
        self.circuit_breaker.market_halted = False
        await self._broadcast(SessionEvent(
            event="MARKET_RESUMED",
            message="Market-wide halt lifted — trading resumed",
            data={"level": level},
        ))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_snapshot(self, symbol: str, book: OrderBook) -> BookSnapshot:
        snap = book.get_snapshot()
        mid = snap["mid_price"]
        spd = snap["spread"]
        ref = self.ref_prices.get(symbol, 0.0)
        entry = self.registry.securities.get(symbol) or {}
        defn = entry.get("defn")
        return BookSnapshot(
            symbol=symbol,
            bids=snap["bids"],
            asks=snap["asks"],
            mid_price=mid if mid is not None else ref,
            spread=spd if spd is not None else 0.0,
            ref_price=ref,
            asset_type=getattr(defn, "asset_type", "equity") or "equity",
        )

    def _build_leaderboard(self) -> Leaderboard:
        traders, brokers = [], []
        exchange_fees = round(self.exchange_revenue, 4)
        lb_marks = self._bidask_marks()   # conservative marks: longs@bid, shorts@ask

        # Cumulative per-participant trade stats, maintained on every fill.
        part_stats = self.part_stats

        for team_id, p in self.portfolios.items():
            live_pos = {sym: qty for sym, qty in p.positions.items() if qty}
            live_avg = {sym: round(c, 4) for sym, c in p.avg_cost.items()
                        if p.positions.get(sym)}
            unreal_by_sym: dict[str, float] = {}
            for sym, qty in live_pos.items():
                ref = self.ref_prices.get(sym, p.avg_cost.get(sym, 0.0))
                unreal_by_sym[sym] = round((ref - p.avg_cost.get(sym, ref)) * qty, 2)

            ps = part_stats[team_id]
            entry: dict = {
                "team_id":         team_id,
                "role":            p.role,
                "cash":            round(p.cash, 2),
                "realized_pnl":    round(p.realized_pnl, 2),
                "unrealized_pnl":  round(p.unrealized_pnl(self.ref_prices), 2),
                "total_fees_paid": round(p.total_fees_paid, 2),
                "total_rebates_earned": round(p.total_rebates_earned, 2),
                "total_carry_paid": round(p.total_carry_paid, 2),
                "liquidated":      p.liquidated,
                "net_worth":       round(p.net_worth(self.ref_prices, lb_marks), 2),
                # Position detail
                "positions":       live_pos,
                "avg_cost":        live_avg,
                "unrealized_by_sym": unreal_by_sym,
                # Activity stats
                "trade_count":     ps["trade_count"],
                "volume":          round(ps["volume"], 2),
                "buy_count":       ps["buy_count"],
                "sell_count":      ps["sell_count"],
                "maker_count":     ps["maker_count"],
            }
            if p.role == "trader":
                traders.append(entry)
            elif p.role == "broker":
                brokers.append(entry)

        traders.sort(key=lambda x: x["net_worth"], reverse=True)
        brokers.sort(key=lambda x: x["net_worth"], reverse=True)

        # Participant summary exposed to the exchange-team detail view
        participants = [
            {
                "team_id":     tid,
                "role":        self.portfolios[tid].role if tid in self.portfolios else "unknown",
                "trade_count": round(ps["trade_count"], 0),
                "volume":      round(ps["volume"], 2),
                "fees_paid":   round(self.portfolios[tid].total_fees_paid, 4)
                               if tid in self.portfolios else 0.0,
                "rebates_earned": round(self.portfolios[tid].total_rebates_earned, 4)
                                  if tid in self.portfolios else 0.0,
            }
            for tid, ps in part_stats.items()
        ]
        participants.sort(key=lambda x: x["volume"], reverse=True)

        return Leaderboard(
            traders=traders,
            brokers=brokers,
            exchange_fees=exchange_fees,
            tick=self.tick,
            participants=participants,
            connected_clients=list(self.clients.keys()),
            season=self._season_block(),
        )

    def _season_block(self) -> dict:
        """Cached season standings.

        _build_leaderboard() runs on every fill, and scoring walks the whole
        equity history, so this is recomputed only when a new snapshot lands
        (every SEASON_SNAPSHOT_TICKS) or the week changes.
        """
        if self._season_dirty or self._season_cache is None:
            self._season_cache = scoring.build_season_block(
                self.equity_history, self.portfolios, self.scenario)
            self._season_dirty = False
        return self._season_cache

    def _print_status(self) -> None:
        lb = self._build_leaderboard()
        w = 66
        print(f"\n  {'─' * w}")
        print(
            f"  Tick {self.tick:>6}   "
            f"Session: {'OPEN  ' if self.session_open else 'CLOSED'}   "
            f"Clients: {len(self.clients):>2}   "
            f"Trades: {self.trade_count:>4}"
        )
        print(f"  {'─' * w}")
        print(f"  {'Team':<22}  {'Role':<8}  {'Net Worth':>14}  {'Fees Paid':>10}")
        print(f"  {'─'*22}  {'─'*8}  {'─'*14}  {'─'*10}")
        for entry in lb.traders + lb.brokers:
            print(
                f"  {entry['team_id']:<22}  {entry['role']:<8}"
                f"  ${entry['net_worth']:>13,.2f}  ${entry['total_fees_paid']:>9,.2f}"
            )
        print(f"\n  Exchange fees collected: ${lb.exchange_fees:,.2f}")
        print(f"  {'─' * w}\n")

    def _spawn(self, coro: Any) -> None:
        """Run a coroutine in the background, keeping a reference to it."""
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _send(self, websocket: Any, message: Any) -> None:
        """Send a message to one client; silently ignore connection errors."""
        await self._send_payload(
            self.ws_to_team.get(websocket), websocket,
            message.model_dump_json())

    async def _send_payload(self, team_id: str | None, websocket: Any,
                            payload: str) -> None:
        """Deliver one serialised message, honouring the team's latency tier.

        A delayed send is fired as a task rather than awaited, so one slow
        tier never holds up the match loop or anyone else's data.
        """
        delay = self.latency_for(team_id)
        if delay > 0:
            self._spawn(self._delayed_send(websocket, payload, delay))
            return
        try:
            await websocket.send(payload)
        except Exception:
            pass

    async def _delayed_send(self, websocket: Any, payload: str,
                            delay: float) -> None:
        """Hold a message for `delay` seconds, then send it."""
        try:
            await asyncio.sleep(delay)
            await websocket.send(payload)
        except Exception:
            pass

    async def _broadcast(
        self, message: Any,
        skip_team: str | None = None,
        skip_ids: set[str] | None = None,
    ) -> None:
        """Send a message to all connected clients."""
        payload = message.model_dump_json()
        self._record(payload)
        exclude = (skip_ids or set())
        if skip_team:
            exclude = exclude | {skip_team}
        coros = [
            self._send_payload(tid, ws, payload)
            for tid, ws in list(self.clients.items())
            if tid not in exclude
        ]
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)

    # ------------------------------------------------------------------
    # Session recording (replay with scripts/replay_session.py)
    # ------------------------------------------------------------------

    def _record(self, payload: str) -> None:
        """Append one broadcast message to the session recording (JSONL)."""
        if self._recorder is None:
            return
        try:
            self._recorder.write(f'{{"ts": {time.time():.3f}, "msg": {payload}}}\n')
        except OSError as exc:
            logger.warning("Recording failed (%s) — disabled", exc)
            self._recorder = None

    def _start_recording(self) -> None:
        if not config.RECORD_SESSIONS:
            return
        os.makedirs(config.SESSIONS_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(config.SESSIONS_DIR, f"session_{stamp}.jsonl")
        self._recorder = open(path, "w")
        logger.info("Recording session → %s", path)

    def _stop_recording(self) -> None:
        if self._recorder:
            self._recorder.close()
            self._recorder = None


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------

def _get_local_ip() -> str:
    """Best-effort LAN IP detection (falls back to localhost)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _print_startup(server: ExchangeServer) -> None:
    local_ip = _get_local_ip()
    symbols = " ".join(server.books)
    shocks = " ".join(s["id"] for s in server.registry.list_shocks())

    print()
    print("=" * 62)
    print("         AlgoArena Exchange Server")
    print("=" * 62)
    print(f"  WebSocket  : ws://{config.HOST}:{config.PORT}")
    print(f"  Local IP   : ws://{local_ip}:{config.PORT}   ← share with students")
    print(f"  Fee rate   : {config.FEE_RATE:.4f}  ({config.FEE_RATE*100:.2f}%)")
    if config.MAKER_TAKER_ENABLED:
        sched = config.resolve_venue_fees()
        net = (sched["taker"] - sched["rebate"]) * 10_000
        print(f"  Schedule   : taker {sched['taker'] * 10_000:.1f} bps  "
              f"rebate {sched['rebate'] * 10_000:.1f} bps  "
              f"net {net:.1f} bps   [{sched['source']}]")
    print(f"  Initial $  : ${config.INITIAL_CASH:,.0f} per team")
    print("-" * 62)
    print(f"  Symbols    : {symbols}")
    print(f"  Shocks     : {shocks[:52]}")
    print("=" * 62)
    print("  Waiting for participants...  Type 'help' for commands.")
    print("=" * 62)
    print()


def _print_cli_help(shocks: list) -> None:
    print("""
Teacher Commands
────────────────────────────────────────────────────────
  open              Open trading session  (activates bots)
  close             Close session + broadcast final leaderboard
  shock <id>        Apply a market shock
  shock list        List all registered shocks
  fees <rate>       Set fee rate  (e.g.  fees 0.002)
  status            Print current standings
  help              Show this message
  quit              Shut down the server
────────────────────────────────────────────────────────
""")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    server = ExchangeServer()

    async with websockets.serve(server.handle_client, config.HOST, config.PORT):
        _print_startup(server)

        if config.SESSION_AUTOOPEN:
            await server.open_session()
            logger.info("SESSION_AUTOOPEN — session opened at startup")

        # TODO Level 6: Compute and broadcast VWAP; implement analytics endpoint

        try:
            await asyncio.gather(
                server._book_snapshot_loop(),
                server._leaderboard_loop(),
                server._price_tick_loop(),
                server._teacher_cli(),
            )
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            pass

    logger.info("Exchange server stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
