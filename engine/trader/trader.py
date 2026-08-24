"""
trader/trader.py — AlgoArena trading bot template.

HOW IT WORKS
============

1. Connect & authenticate
   TraderBot opens a WebSocket to the exchange and sends a Handshake.
   You will immediately receive BookSnapshot messages for every symbol
   so your strategy has live data before the first tick fires.

2. Receive market data
   Two streams arrive continuously while the session runs:

   BookSnapshot  — the current order book for one symbol:
                   top bids, top asks, mid price, bid-ask spread.
                   MarketData stores every snapshot.

   TradeExecution — a confirmed trade (yours or someone else's).
                    Useful for tracking momentum and order flow.

3. Generate a trading signal
   Every TICK_INTERVAL_SEC the trading loop calls
   Strategy.generate_signal(market, portfolio).  It returns either
   None (do nothing this tick) or a Signal with symbol, side, qty,
   and a limit price.

4. Risk check
   Before any order leaves the bot, RiskManager.check_order() reviews it.
   Level 1: always approves.  Level 4: you implement position limits,
   cash checks, and a daily-loss halt.

5. Place the order
   If the signal passes risk, a PlaceOrder message goes to the exchange.
   The exchange replies with OrderAck (confirmed) or ErrorMsg (rejected).

6. Track P&L
   After every fill, the exchange sends a PortfolioUpdate containing your
   current cash, open positions, realised P&L, and net worth.
   Portfolio.apply_server_update() keeps your local copy in sync.

7. Session end
   When the teacher fires SESSION_CLOSED, the bot automatically flattens
   all open positions with market orders so your score is locked at
   fair-value prices.

WHERE TO FOCUS
==============
Your competitive edge lives entirely in Strategy.generate_signal().

Start by running Level 1 as-is — it just proves connectivity.  Once you
see fills coming through, comment out the Level 1 block and uncomment one
of the provided options (A–E).  Options A (momentum) and B (mean reversion)
are solid Level 3 starting points.  Everything above that is up to you.

Run:
    python -m trader.trader
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import websockets
import websockets.exceptions
from rich.console import Console
from rich.table import Table

import trader.config as config
from shared.messages import (
    BookSnapshot,
    ErrorMsg,
    Handshake,
    Leaderboard,
    OrderAck,
    PlaceOrder,
    PortfolioUpdate,
    SessionEvent,
    Signal,
    TradeExecution,
    parse_message,
)

logger = logging.getLogger(__name__)
_console = Console()


# ─────────────────────────────────────────────────────────────────────────────
# MarketData — live order book and price history
# ─────────────────────────────────────────────────────────────────────────────

class MarketData:
    """Live market state, updated continuously from exchange messages.

    All query methods return None (or 0.0) when data is unavailable, so
    your strategy can safely call them from the very first tick without
    guarding against KeyErrors.
    """

    def __init__(self) -> None:
        # symbol → {"bids": [[price, qty], ...], "asks": [...],
        #            "mid": float, "spread": float}
        self._books: dict[str, dict] = {}

        # Rolling mid-price history, max 200 ticks per symbol (oldest first).
        # Used by momentum and mean-reversion strategies.
        self._price_history: dict[str, deque] = {}

        # Recent confirmed trades — useful for flow analysis and VWAP.
        self._recent_trades: list[TradeExecution] = []

    # ------------------------------------------------------------------
    # Ingest exchange messages
    # ------------------------------------------------------------------

    def update_book(self, snapshot: BookSnapshot) -> None:
        """Store the latest order book for a symbol."""
        self._books[snapshot.symbol] = {
            "bids":   snapshot.bids,
            "asks":   snapshot.asks,
            "mid":    snapshot.mid_price,
            "spread": snapshot.spread,
        }
        if snapshot.mid_price > 0:
            hist = self._price_history.setdefault(snapshot.symbol, deque(maxlen=200))
            hist.append(snapshot.mid_price)

    def update_trade(self, execution: TradeExecution) -> None:
        """Record a confirmed trade (ours or a counterparty's)."""
        self._recent_trades.append(execution)
        if len(self._recent_trades) > 500:
            self._recent_trades = self._recent_trades[-500:]

    # ------------------------------------------------------------------
    # Query interface used by Strategy
    # ------------------------------------------------------------------

    def symbols(self) -> list[str]:
        """All symbols for which we have received at least one book update."""
        return list(self._books.keys())

    def mid_price(self, symbol: str) -> float | None:
        """Current mid price ((best_bid + best_ask) / 2), or None."""
        book = self._books.get(symbol)
        return book["mid"] if book else None

    def best_bid(self, symbol: str) -> float | None:
        """Highest resting bid price, or None if the book is empty."""
        book = self._books.get(symbol)
        if not book or not book["bids"]:
            return None
        return book["bids"][0][0]

    def best_ask(self, symbol: str) -> float | None:
        """Lowest resting ask price, or None if the book is empty."""
        book = self._books.get(symbol)
        if not book or not book["asks"]:
            return None
        return book["asks"][0][0]

    def spread(self, symbol: str) -> float | None:
        """Current bid-ask spread in dollars, or None."""
        book = self._books.get(symbol)
        return book["spread"] if book else None

    def prices(self, symbol: str) -> list[float]:
        """Recent mid prices, oldest first.  Up to 200 values.

        This is the primary input for momentum and mean-reversion signals.
        The list grows longer as ticks accumulate — check len() before
        computing moving averages.
        """
        return list(self._price_history.get(symbol, []))

    def order_book_imbalance(self, symbol: str) -> float:
        """Signed measure of whether buyers or sellers dominate.

        Returns a float in [-1, +1]:
          +1  → all volume is on the bid (strong buy pressure)
          -1  → all volume is on the ask (strong sell pressure)
           0  → balanced book or no data

        Formula: (bid_volume - ask_volume) / (bid_volume + ask_volume)
        """
        book = self._books.get(symbol)
        if not book:
            return 0.0
        bid_vol = sum(qty for _, qty in book["bids"])
        ask_vol = sum(qty for _, qty in book["asks"])
        total = bid_vol + ask_vol
        return (bid_vol - ask_vol) / total if total else 0.0

    # ═══════════════════════════════════════════
    # LEVEL 6 TODO: VWAP
    #
    # Implement vwap(symbol, window_sec) -> float | None
    # Volume-Weighted Average Price over recent confirmed trades.
    # A price above VWAP suggests bullish momentum; below suggests bearish.
    #
    # Hint — _recent_trades is a list[TradeExecution].
    # Each TradeExecution has: symbol, price, quantity.
    # You'll need to attach a timestamp when storing trades.
    #
    # Pseudocode:
    #   import time
    #   cutoff = time.time() - window_sec
    #   relevant = [t for t in self._recent_trades
    #               if t.symbol == symbol and t.timestamp >= cutoff]
    #   if not relevant:
    #       return None
    #   total_value = sum(t.price * t.quantity for t in relevant)
    #   total_qty   = sum(t.quantity for t in relevant)
    #   return total_value / total_qty
    # ═══════════════════════════════════════════


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio — local P&L tracker, synced from server
# ─────────────────────────────────────────────────────────────────────────────

class Portfolio:
    """Local mirror of the exchange's authoritative portfolio record.

    The exchange is always the source of truth.  Call apply_server_update()
    whenever a PortfolioUpdate arrives to keep this object in sync.

    can_buy / can_sell are fast local pre-checks.  The exchange will still
    reject orders that fail its own validation — these are just early filters
    to avoid unnecessary round-trips.
    """

    def __init__(self) -> None:
        self.team_id: str = config.TEAM_ID
        self.cash: float = config.STARTING_CASH
        self.positions: dict[str, int] = {}
        self.realized_pnl: float = 0.0
        self.total_fees_paid: float = 0.0
        self._unrealized_pnl: float = 0.0
        self._net_worth: float = config.STARTING_CASH

    def apply_server_update(self, update: PortfolioUpdate) -> None:
        """Sync all fields from an authoritative PortfolioUpdate message."""
        self.cash = update.cash
        self.positions = dict(update.positions)
        self.realized_pnl = update.realized_pnl
        self.total_fees_paid = update.total_fees_paid
        self._unrealized_pnl = update.unrealized_pnl
        self._net_worth = update.net_worth

    def net_worth(self, market: MarketData) -> float:  # noqa: ARG002
        """Current net worth — cash + mark-to-market value of all positions."""
        return self._net_worth

    def unrealized_pnl(self, market: MarketData) -> float:  # noqa: ARG002
        """Current unrealised P&L (marked to exchange reference prices)."""
        return self._unrealized_pnl

    def can_buy(self, symbol: str, qty: int, price: float) -> bool:
        """True if we have enough cash to buy qty shares at price.

        Includes a small buffer for transaction fees (≈0.1%).
        """
        return self.cash >= price * qty * 1.001

    def can_sell(self, symbol: str, qty: int) -> bool:
        """True if we hold at least qty shares of symbol (no short selling)."""
        return self.positions.get(symbol, 0) >= qty

    def log_status(self, market: MarketData) -> None:
        """Print a formatted portfolio snapshot to the terminal."""
        table = Table(title=f"Portfolio — {self.team_id}", show_lines=False)
        table.add_column("Symbol", style="bold")
        table.add_column("Qty",       justify="right")
        table.add_column("Mid $",     justify="right")
        table.add_column("Mkt Value", justify="right")

        for sym, qty in sorted(self.positions.items()):
            if qty == 0:
                continue
            mid = market.mid_price(sym) or 0.0
            table.add_row(sym, str(qty), f"{mid:.2f}", f"{qty * mid:,.2f}")

        _console.print(table)

        real_c = "green" if self.realized_pnl >= 0 else "red"
        unr_c = "green" if self._unrealized_pnl >= 0 else "red"
        _console.print(
            f"Cash: [bold]${self.cash:,.2f}[/bold]  "
            f"Net Worth: [bold]${self._net_worth:,.2f}[/bold]  "
            f"Realized: [{real_c}]{self.realized_pnl:+,.2f}[/{real_c}]  "
            f"Unrealized: [{unr_c}]{self._unrealized_pnl:+,.2f}[/{unr_c}]  "
            f"Fees paid: {self.total_fees_paid:.2f}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# RiskManager — pre-flight checks before every order
# ─────────────────────────────────────────────────────────────────────────────

class RiskManager:
    """Gates every order before it is sent to the exchange.

    Level 1: all orders pass.
    Level 4: implement real limits using the TODO block below.

    check_order returns (approved: bool, reason: str).
    When approved=False the reason is logged and the order is dropped.
    """

    def check_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        portfolio: Portfolio,
        market: MarketData,
    ) -> tuple[bool, str]:
        """Approve or reject an order before it leaves the bot."""

        # Level 1: unconditionally approve every order.
        return True, "ok"

        # ═══════════════════════════════════════════
        # LEVEL 4 TODO: Real Risk Checks
        #
        # Implement the three guards below in order.
        # Delete the `return True, "ok"` line above first.
        #
        # ── Check 1: Position size limit ──────────────────────────────────
        # current = portfolio.positions.get(symbol, 0)
        # new_pos = current + qty if side == "buy" else current - qty
        # if abs(new_pos) > config.MAX_POSITION_SIZE:
        #     return False, (
        #         f"position limit: {abs(new_pos)} shares would exceed "
        #         f"MAX_POSITION_SIZE={config.MAX_POSITION_SIZE}"
        #     )
        #
        # ── Check 2: Cash / inventory sufficiency ─────────────────────────
        # if side == "buy" and not portfolio.can_buy(symbol, qty, price):
        #     return False, f"insufficient cash ({portfolio.cash:.2f})"
        # if side == "sell" and not portfolio.can_sell(symbol, qty):
        #     return False, f"insufficient position ({portfolio.positions.get(symbol, 0)} held)"
        #
        # ── Check 3: Daily loss halt ───────────────────────────────────────
        # total_loss = -(portfolio.realized_pnl + portfolio._unrealized_pnl)
        # if total_loss >= config.MAX_DAILY_LOSS:
        #     return False, (
        #         f"daily loss limit reached: ${total_loss:,.0f} >= "
        #         f"MAX_DAILY_LOSS=${config.MAX_DAILY_LOSS:,.0f}"
        #     )
        #
        # return True, "ok"
        # ═══════════════════════════════════════════

    def check_stop_loss(
        self,
        symbol: str,
        entry_price: float,
        current_price: float,
        side: str,
    ) -> bool:
        """Return True if this position should be closed to limit losses.

        Level 1: never triggers.
        """
        return False

        # ═══════════════════════════════════════════
        # LEVEL 4 TODO: Stop-Loss Check
        #
        # Close a position automatically when it has lost more than
        # STOP_LOSS_PCT of its entry value.
        #
        # For a long position (we bought — side == "buy"):
        #   The price fell below our entry.
        #   loss_pct = (entry_price - current_price) / entry_price
        #   return loss_pct >= config.STOP_LOSS_PCT
        #
        # For a short position (we sold — side == "sell"):
        #   The price rose above our entry.
        #   loss_pct = (current_price - entry_price) / entry_price
        #   return loss_pct >= config.STOP_LOSS_PCT
        # ═══════════════════════════════════════════


# ─────────────────────────────────────────────────────────────────────────────
# Strategy — YOUR COMPETITIVE EDGE LIVES HERE
# ─────────────────────────────────────────────────────────────────────────────

class Strategy:
    """
    ════════════════════════════════════════════════════════════════════════
    THIS IS WHERE YOU BUILD YOUR EDGE.
    ════════════════════════════════════════════════════════════════════════

    Implement generate_signal() to return a Signal when you want to trade,
    or None to sit out this tick.

    A Signal needs:  symbol, side ("buy"/"sell"), quantity, price (limit).

    The Level 1 starter below proves your bot is wired up correctly.
    Comment it out and enable one of the options below once you're ready
    to compete.
    """

    def generate_signal(
        self, market: MarketData, portfolio: Portfolio
    ) -> Signal | None:
        """Return a trading signal for this tick, or None."""

        # ── LEVEL 1: Connectivity test ────────────────────────────────────────
        #
        # This is NOT a real strategy.  It fires randomly 10% of ticks and
        # places a single-share order just to confirm end-to-end connectivity:
        #   connect → receive book → place order → get fill → see P&L update.
        #
        # Buys at the best ask and sells at the best bid so the order always
        # crosses the spread and fills immediately.
        #
        # Once you see "Fill: bought/sold" in the logs, comment this block
        # out and replace it with a real strategy from the options below.
        # ─────────────────────────────────────────────────────────────────────
        if random.random() < 0.7:
            return None                           # idle 70% of ticks (~1 order/1.5s)

        candidates = [s for s in market.symbols() if market.best_ask(s) is not None]
        if not candidates:
            return None

        symbol = random.choice(candidates)

        # Buy if flat (lift the ask), sell if long (hit the bid)
        if portfolio.positions.get(symbol, 0) == 0:
            ask = market.best_ask(symbol)
            return Signal(symbol=symbol, side="buy", quantity=1, price=ask)
        elif portfolio.can_sell(symbol, 1):
            bid = market.best_bid(symbol)
            if bid is None:
                return None
            return Signal(symbol=symbol, side="sell", quantity=1, price=bid)
        return None
        # ─────────────────────────────────────────────────────────────────────

        # ── OPTION A: Momentum (Level 3) ──────────────────────────────────────
        #
        # Theory: when the short-term moving average crosses above the long-term
        # one, the asset is trending up → buy.  The reverse → sell.
        #
        # Parameters: short window = 5, long window = 20, buffer = 0.3%
        # to filter out noise and avoid whipsaw trades.
        #
        # for symbol in market.symbols():
        #     hist = market.prices(symbol)
        #     if len(hist) < 20:
        #         continue                          # not enough history yet
        #
        #     short_ma = sum(hist[-5:])  / 5
        #     long_ma  = sum(hist[-20:]) / 20
        #     mid      = market.mid_price(symbol)
        #
        #     if short_ma > long_ma * 1.003:        # uptrend → buy
        #         if portfolio.can_buy(symbol, 5, mid):
        #             return Signal(symbol=symbol, side="buy",  quantity=5, price=mid)
        #
        #     elif short_ma < long_ma * 0.997:      # downtrend → sell
        #         if portfolio.can_sell(symbol, 5):
        #             return Signal(symbol=symbol, side="sell", quantity=5, price=mid)
        # return None
        # ─────────────────────────────────────────────────────────────────────

        # ── OPTION B: Mean Reversion (Level 3) ───────────────────────────────
        #
        # Theory: when a price has moved unusually far from its average,
        # it tends to snap back.  Use a z-score to measure "how unusual."
        #
        #   z = (current_price - mean_20) / std_20
        #   z < -1.5 → oversold → buy (expect bounce)
        #   z >  1.5 → overbought → sell (expect drop)
        #
        # for symbol in market.symbols():
        #     hist = market.prices(symbol)
        #     if len(hist) < 20:
        #         continue
        #
        #     window = hist[-20:]
        #     mean   = sum(window) / 20
        #     var    = sum((x - mean) ** 2 for x in window) / 19  # sample var
        #     if var == 0:
        #         continue
        #     z   = (market.mid_price(symbol) - mean) / var ** 0.5
        #     mid = market.mid_price(symbol)
        #
        #     if z < -1.5 and portfolio.can_buy(symbol, 5, mid):   # oversold
        #         return Signal(symbol=symbol, side="buy",  quantity=5, price=mid)
        #     if z >  1.5 and portfolio.can_sell(symbol, 5):       # overbought
        #         return Signal(symbol=symbol, side="sell", quantity=5, price=mid)
        # return None
        # ─────────────────────────────────────────────────────────────────────

        # ── OPTION C: Order Book Imbalance (Level 6) ──────────────────────────
        #
        # Theory: when buy orders heavily outnumber sell orders, price pressure
        # is upward.  Trade in the direction of imbalance before it moves.
        #
        #   imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume)
        #   imbalance ∈ [-1, +1]
        #
        # for symbol in market.symbols():
        #     imb = market.order_book_imbalance(symbol)
        #     mid = market.mid_price(symbol)
        #     if mid is None:
        #         continue
        #
        #     if imb >  0.4 and portfolio.can_buy(symbol, 5, mid):  # buyers dominate
        #         return Signal(symbol=symbol, side="buy",  quantity=5, price=mid,
        #                       confidence=imb)
        #     if imb < -0.4 and portfolio.can_sell(symbol, 5):      # sellers dominate
        #         return Signal(symbol=symbol, side="sell", quantity=5, price=mid,
        #                       confidence=abs(imb))
        # return None
        # ─────────────────────────────────────────────────────────────────────

        # ── OPTION D: Pairs / Relative Value (Level 6) ────────────────────────
        #
        # Theory: if one asset has lagged the broader market, it should
        # eventually catch up.  Buy the laggard, sell the leader.
        #
        # syms = [s for s in market.symbols() if market.mid_price(s) is not None]
        # if not syms:
        #     return None
        # avg = sum(market.mid_price(s) for s in syms) / len(syms)
        # if avg == 0:
        #     return None
        #
        # for symbol in syms:
        #     mid   = market.mid_price(symbol)
        #     ratio = mid / avg
        #
        #     if ratio < 0.95 and portfolio.can_buy(symbol, 5, mid):   # lagging
        #         return Signal(symbol=symbol, side="buy",  quantity=5, price=mid,
        #                       confidence=min(1.0, (0.95 - ratio) * 20))
        #     if ratio > 1.05 and portfolio.can_sell(symbol, 5):       # leading
        #         return Signal(symbol=symbol, side="sell", quantity=5, price=mid,
        #                       confidence=min(1.0, (ratio - 1.05) * 20))
        # return None
        # ─────────────────────────────────────────────────────────────────────

        # ── OPTION F: Multi-Venue Smart Order Routing (Level 6) ──────────────
        #
        # Theory: when the same symbol trades on several exchanges, quotes
        # drift apart. Route each order to the venue with the best price —
        # or capture the difference outright (buy cheap venue, sell rich one).
        #
        # LEVEL 6 TODO: connect this bot to a second exchange (see how
        # EXCHANGE_URLS works in broker/config.py for the pattern), keep a
        # book per venue, and before sending any order compare the venues'
        # best bid/ask. What is your edge net of taker fees on both legs?
        # When is it better to rest post_only on one venue instead?
        # ─────────────────────────────────────────────────────────────────────

        # ── OPTION E: Your Own Strategy (Level 3+) ────────────────────────────
        #
        # This is intentionally open-ended.  Ideas to get you started:
        #
        #   Technical indicators (all computable from market.prices(symbol)):
        #     RSI, MACD, Bollinger Bands, ATR, Volume-Profile
        #
        #   Machine learning (train offline, predict live):
        #     from sklearn.linear_model import LogisticRegression
        #     Store the trained model in __init__; call .predict() here.
        #     Features: returns, spread, imbalance, recent-trade counts.
        #
        #   Event-driven (react to teacher's shock events):
        #     Store SessionEvent messages in __init__.
        #     When you see "SHOCK: flash_crash", tighten risk or flatten.
        #
        #   Market making (compete with the broker!):
        #     Post bids and asks, earn the spread.
        #     You'll need to track your resting orders like BrokerBot does.
        # ─────────────────────────────────────────────────────────────────────

        # ── OPTION G: Trading the event calendar (Level 5) ────────────────────
        #
        # From week 3 the exchange runs a market calendar. Scheduled earnings,
        # economic prints, and dividends are ANNOUNCED IN ADVANCE — you are
        # told the tick and the size range, but never the direction. Two ways
        # to see them:
        #
        #   1. SessionEvent  event="CALENDAR"        (at session open, and
        #      again shortly before each event; see _handle_session_event)
        #   2. HTTP          GET /api/calendar       on the dashboard host
        #
        # When an event fires you get event="CALENDAR_EVENT" carrying the
        # resolved `pct`. Crucially the move does NOT land in one tick: it
        # ramps over ~15 ticks, overshoots by ~30%, then settles back.
        #
        # LEVEL 5 TODO: decide what to do with foreknowledge of TIMING alone.
        # Options to think through, in order of difficulty:
        #   - Risk-off: flatten before an event so you are not the one holding
        #     the position when it moves. What does that cost you in missed P&L?
        #   - Trade the ramp: once CALENDAR_EVENT tells you the direction, the
        #     move still has most of its path left. How late is too late, once
        #     you pay the taker fee both ways?
        #   - Fade the overshoot: the ramp peaks above the final level. Can you
        #     detect the turn without curve-fitting one event?
        # Measure each idea in the simulator before you trust it — the season
        # report's shock-attribution table exists for exactly this.
        # ─────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# TraderBot — WebSocket client that ties everything together
# ─────────────────────────────────────────────────────────────────────────────

class TraderBot:
    """Connect to the exchange, receive data, trade, track P&L.

    Composes MarketData, Portfolio, RiskManager, and Strategy.
    You should only need to edit Strategy; the plumbing here stays fixed.
    """

    def __init__(self) -> None:
        self.market    = MarketData()
        self.portfolio = Portfolio()
        self.risk      = RiskManager()
        self.strategy  = Strategy()

        self._ws: Any = None
        self._session_open = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open a WebSocket to the exchange and send the Handshake."""
        self._ws = await websockets.connect(config.EXCHANGE_URL)
        hs = Handshake(team_id=config.TEAM_ID, role="trader", level=1,
                       token=config.ARENA_TOKEN)
        await self._ws.send(hs.model_dump_json())
        logger.info("Connected to %s as %s (trader)", config.EXCHANGE_URL, config.TEAM_ID)

    async def _send(self, msg: Any) -> None:
        """Send a Pydantic message to the exchange."""
        try:
            await self._ws.send(msg.model_dump_json())
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Could not send %s — connection closed", type(msg).__name__)

    # ------------------------------------------------------------------
    # Incoming message dispatcher
    # ------------------------------------------------------------------

    async def listen(self) -> None:
        """Receive exchange messages and route them to the right handlers.

        Runs until the WebSocket closes.
        """
        async for raw in self._ws:
            try:
                msg = parse_message(json.loads(raw))
            except (KeyError, ValueError) as exc:
                logger.warning("Unrecognised message: %s", exc)
                continue

            if isinstance(msg, BookSnapshot):
                self.market.update_book(msg)

            elif isinstance(msg, TradeExecution):
                self.market.update_trade(msg)
                self._on_trade(msg)

            elif isinstance(msg, PortfolioUpdate):
                self.portfolio.apply_server_update(msg)
                logger.debug("Portfolio: net_worth=%.2f", msg.net_worth)

            elif isinstance(msg, SessionEvent):
                await self._on_session_event(msg)

            elif isinstance(msg, OrderAck):
                logger.debug("Ack: %s %d %s @ %.4f",
                             msg.side, msg.quantity, msg.symbol, msg.price)

            elif isinstance(msg, ErrorMsg):
                logger.warning("Exchange error [%s]: %s", msg.code, msg.message)

            elif isinstance(msg, Leaderboard):
                self._on_leaderboard(msg)

    def _on_trade(self, msg: TradeExecution) -> None:
        """Log fills that belong to our team."""
        if msg.buyer_id == config.TEAM_ID:
            logger.info("Fill: bought %d %s @ %.4f (fee=%.4f)",
                        msg.quantity, msg.symbol, msg.price, msg.fee / 2)
        elif msg.seller_id == config.TEAM_ID:
            logger.info("Fill: sold   %d %s @ %.4f (fee=%.4f)",
                        msg.quantity, msg.symbol, msg.price, msg.fee / 2)

    async def _on_session_event(self, msg: SessionEvent) -> None:
        logger.info("Session event: %s — %s", msg.event, msg.message)
        if msg.event == "SESSION_PREOPEN":
            # Limit orders may rest into the opening auction. Marketable
            # types are rejected by the venue until the cross.
            self._session_open = True
            _console.print("[yellow]◌  Pre-open — auction builds[/yellow]")
        elif msg.event == "SESSION_OPEN":
            self._session_open = True
            _console.print("[bold green]▶  Session open — trading begins[/bold green]")
        elif msg.event == "SESSION_CLOSED":
            self._session_open = False
            _console.print("[bold red]■  Session closed — flattening positions[/bold red]")
            await self.flatten_all_positions()
        else:
            # Shocks, calendar announcements, dividends, week changes and
            # liquidations all arrive here as SessionEvents. The ones worth
            # storing:
            #
            #   CALENDAR        msg.data["events"]  — upcoming events: kind,
            #                   symbol, tick, magnitude_range. NO direction.
            #   CALENDAR_EVENT  msg.data["pct"]     — an event just fired; the
            #                   move ramps in over data["ramp_ticks"].
            #   DIVIDEND        msg.data["amount_per_share"] — cash settled,
            #                   price marked down ex-dividend.
            #   SHOCK           a teacher-injected move (also ramped).
            #
            # LEVEL 5 TODO: keep the announced calendar somewhere your Strategy
            # can read it (see OPTION G in Strategy.generate_signal) and decide
            # how your risk should change as an event approaches.
            _console.print(f"[yellow]Session: {msg.event} — {msg.message}[/yellow]")

    def _on_leaderboard(self, msg: Leaderboard) -> None:
        """Log our ranking each time the leaderboard is broadcast."""
        for entry in msg.traders:
            if entry.get("team_id") == config.TEAM_ID:
                _console.print(
                    f"[cyan]Tick {msg.tick}[/cyan]  "
                    f"rank=#{entry.get('rank', '?')}  "
                    f"net_worth=${entry.get('net_worth', 0):,.2f}  "
                    f"realized_pnl={entry.get('realized_pnl', 0):+,.2f}"
                )
                break

    # ------------------------------------------------------------------
    # Trading loop
    # ------------------------------------------------------------------

    async def trading_loop(self) -> None:
        """Every TICK_INTERVAL_SEC: signal → risk check → place order.

        The session must be open before any order is sent.
        Strategy exceptions are caught so a buggy signal never crashes the bot.
        """
        while True:
            await asyncio.sleep(config.TICK_INTERVAL_SEC)

            if not self._session_open:
                continue

            # Ask the strategy what to do this tick.
            try:
                signal = self.strategy.generate_signal(self.market, self.portfolio)
            except Exception:
                logger.exception("Strategy raised an exception — skipping tick")
                continue

            if signal is None:
                continue

            # Gate the signal through risk management.
            approved, reason = self.risk.check_order(
                signal.symbol, signal.side, signal.quantity, signal.price,
                self.portfolio, self.market,
            )
            if not approved:
                logger.info("Order blocked [%s]: %s", signal.symbol, reason)
                continue

            await self._place_order(signal)

    async def _place_order(self, signal: Signal) -> None:
        """Send a limit order to the exchange."""
        await self._send(PlaceOrder(
            team_id=config.TEAM_ID,
            symbol=signal.symbol,
            side=signal.side,
            order_type="limit",
            price=signal.price,
            quantity=signal.quantity,
        ))
        logger.debug("Placed: %s %d %s @ %.4f",
                     signal.side, signal.quantity, signal.symbol, signal.price)

    # ------------------------------------------------------------------
    # Session end
    # ------------------------------------------------------------------

    async def flatten_all_positions(self) -> None:
        """Close every open position with a market order.

        Called automatically on SESSION_CLOSED.  Market orders ensure we
        exit even if the book is thin — the exchange cancels any unfilled
        remainder rather than leaving it resting.
        """
        for symbol, qty in list(self.portfolio.positions.items()):
            if qty == 0:
                continue
            side = "sell" if qty > 0 else "buy"
            size = abs(qty)
            logger.info("Flattening: %s %d %s at market", side, size, symbol)
            await self._send(PlaceOrder(
                team_id=config.TEAM_ID,
                symbol=symbol,
                side=side,
                order_type="market",
                price=0.0,          # ignored by exchange for market orders
                quantity=size,
            ))

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect to the exchange and run, reconnecting automatically on drop."""
        _print_startup()
        retry_delay = 3.0
        while True:
            try:
                await self.connect()
                self._session_open = False   # reset gate on each reconnect
                self.market = MarketData()   # clear stale book state
                # listen() ends when the connection drops — by RAISING on an
                # abnormal drop, but by RETURNING when the venue closes
                # cleanly (a restarting exchange sends a close frame, so the
                # `async for` just ends). gather()-ing it with the infinite
                # trading loop therefore hung forever on clean closes and the
                # bot never reconnected: the trading loop must die with the
                # connection.
                listener = asyncio.create_task(self.listen())
                trading = asyncio.create_task(self.trading_loop())
                try:
                    await listener
                finally:
                    trading.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await trading
                logger.warning(
                    "Exchange closed the connection — reconnecting in %.0fs",
                    retry_delay)
            except websockets.exceptions.ConnectionClosed as exc:
                logger.warning("Exchange connection closed: %s — reconnecting in %.0fs", exc, retry_delay)
            except OSError as exc:
                logger.warning("Could not reach exchange: %s — retrying in %.0fs", exc, retry_delay)
            except Exception:
                logger.exception("Unexpected error — reconnecting in %.0fs", retry_delay)
            finally:
                if self._ws and not self._ws.closed:
                    await self._ws.close()
            await asyncio.sleep(retry_delay)


# ─────────────────────────────────────────────────────────────────────────────
# Startup banner
# ─────────────────────────────────────────────────────────────────────────────

def _print_startup() -> None:
    _console.print()
    _console.print("[bold cyan]╔══════════════════════════════════════════════╗[/bold cyan]")
    _console.print("[bold cyan]║       AlgoArena — Trader Bot                 ║[/bold cyan]")
    _console.print("[bold cyan]╠══════════════════════════════════════════════╣[/bold cyan]")
    _console.print(
        f"[bold cyan]║[/bold cyan]  Team:     [bold]{config.TEAM_ID:<34}[/bold][bold cyan]║[/bold cyan]"
    )
    _console.print(
        f"[bold cyan]║[/bold cyan]  Exchange: {config.EXCHANGE_URL:<34}[bold cyan]║[/bold cyan]"
    )
    # Cash is deliberately not printed here: the exchange grants each bot its
    # registered allocation at connect (the local STARTING_CASH is only a
    # fallback), and printing the wrong number confused every first-timer.
    _console.print(
        f"[bold cyan]║[/bold cyan]  Cash:     set at connect"
        f"  Tick: {config.TICK_INTERVAL_SEC:.1f}s"
        f"{'':>8}[bold cyan]║[/bold cyan]"
    )
    _console.print("[bold cyan]╚══════════════════════════════════════════════╝[/bold cyan]")
    _console.print()


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(TraderBot().run())
