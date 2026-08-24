"""
broker/broker.py — AlgoArena market maker (Yahoo Finance price feed only).

What a market maker is for
-------------------------
It supplies the liquidity everyone else trades against, and it earns the
spread for doing so. It does NOT invent prices: it quotes around the market's
own reference and lets order flow move that reference. So this bot's centre is
the venue's mark (BookSnapshot.ref_price), pulled slowly toward the external
Yahoo reference, and there is no randomness in its quoting at all.

Level 1 (implemented):
  - Connects to the exchange, sends a Handshake, and waits for SESSION_OPEN.
  - Pulls reference prices from Yahoo Finance (polled every YAHOO_POLL_INTERVAL s).
  - Quotes around the venue's mark, anchored on Yahoo with a slow half-life.
  - Posts a bid and ask around that centre with a fixed dollar spread.
  - Requotes only when the centre moves past REQUOTE_THRESHOLD_BPS, when a
    side is gone, or when the quote is older than QUOTE_MAX_AGE_SEC.
  - Cancels all quotes and flattens positions on SESSION_CLOSED.
  - Auto-reconnects to the exchange if the connection drops.

Level 2 TODO: Smart Requoting  — requote only when price moves enough.
Level 3 TODO: Volatility-Adjusted Spread — widen in choppy markets.
Level 4 TODO: Inventory Management — skew quotes to shed unwanted exposure.
Level 5 TODO: Toxic Flow Detection — widen against adverse-selection traders.

Run:
    python -m broker.broker
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import websockets
import websockets.exceptions

import broker.config as config
from shared.messages import (
    BookSnapshot,
    CancelOrder,
    ErrorMsg,
    Handshake,
    Leaderboard,
    OrderAck,
    PlaceOrder,
    PortfolioUpdate,
    SessionEvent,
    TradeExecution,
    parse_message,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BrokerState — all mutable runtime data in one place
# ---------------------------------------------------------------------------

@dataclass
class BrokerState:
    """Runtime state for the broker.

    Updated from two threads:
      * Main asyncio thread — processes exchange messages and places orders.
      * yahoo-feed thread  — updates yahoo_prices via yfinance poll.

    Python's GIL makes simple dict gets/sets safe across threads.
    """

    # External reference (Yahoo poll) and the venue's own mark, per symbol.
    yahoo_prices:    dict[str, float] = field(default_factory=dict)
    exchange_prices: dict[str, float] = field(default_factory=dict)
    price_history:   dict[str, deque] = field(default_factory=dict)
    resting_orders:  dict[str, dict]  = field(default_factory=dict)
    positions:       dict[str, int]   = field(default_factory=dict)
    cash:            float            = 0.0
    spread_income:   float            = 0.0
    fills:           int              = 0
    # Centre and wall-clock time of the quote currently resting, per symbol —
    # what the requote decision is made against.
    last_quote_prices: dict[str, float] = field(default_factory=dict)
    last_quote_time:   dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for sym in config.EQUITY_SYMBOLS:
            self.price_history.setdefault(sym, deque(maxlen=100))
            # buy_id / sell_id: exchange order_id; qty: shares still resting
            self.resting_orders.setdefault(sym, {
                "buy_id": None, "buy_qty": 0,
                "sell_id": None, "sell_qty": 0,
                # Every resting level's id (QUOTE_LEVELS per side). buy_id /
                # sell_id remain the touch level for older readers.
                "buy_ids": [], "sell_ids": [],
            })

    # ------------------------------------------------------------------
    # Level 1 (implemented): fixed dollar spread
    # ------------------------------------------------------------------

    def compute_spread(self, symbol: str) -> float:
        """Return the dollar spread to post around the mid price.

        Level 1: BASE_SPREAD, capped at MAX_SPREAD_BPS of the price.

        The cap is there because a spread only means something relative to the
        price: $0.30 is 14 bps of a $220 share but 136 bps of a $22 one, and no
        desk quotes a 1.4%-wide market in a large cap. Left uncapped it also
        made the cheap names the noisiest instruments in the game — the touch
        jumped by most of a spread whenever the best quote was taken.
        """
        spread = config.BASE_SPREAD
        price = self.exchange_prices.get(symbol) or self.yahoo_prices.get(symbol)
        if price and price > 0:
            spread = min(spread, price * config.MAX_SPREAD_BPS / 10_000.0)
        return max(spread, config.MIN_SPREAD_ABS)

        # ═══════════════════════════════════════════
        # LEVEL 3 TODO: Volatility-Adjusted Spread
        #
        # Currently spread is fixed at BASE_SPREAD.
        # Upgrade: use the rolling price history to estimate realised
        # volatility, then widen the spread in choppy markets.
        #
        # Pseudocode:
        #   import numpy as np
        #   hist = list(self.price_history.get(symbol, []))
        #   if len(hist) < config.VOL_WINDOW:
        #       return config.BASE_SPREAD
        #   returns = np.diff(hist[-config.VOL_WINDOW:]) / hist[-config.VOL_WINDOW:-1]
        #   vol = float(np.std(returns))
        #   spread = config.BASE_SPREAD * (1 + config.VOL_MULTIPLIER * vol)
        #   return max(config.MIN_SPREAD, min(config.MAX_SPREAD, spread))
        # ═══════════════════════════════════════════

    def compute_skew(self, symbol: str) -> float:
        """Return a price skew (in $) to apply to both the bid and ask.

        Level 1: always 0 — symmetric quotes.
        """
        return 0.0

        # ═══════════════════════════════════════════
        # LEVEL 4 TODO: Inventory Management
        #
        # Skew quotes in the direction that flattens the net inventory.
        # If long AAPL (pos > 0) → lower prices to attract sellers.
        # If short AAPL (pos < 0) → raise prices to attract buyers.
        #
        # Pseudocode:
        #   net = self.positions.get(symbol, 0)
        #   skew = -net * config.SKEW_FACTOR
        #   max_skew = config.BASE_SPREAD * 2
        #   return max(-max_skew, min(max_skew, skew))
        # ═══════════════════════════════════════════

    def is_toxic(self, trader_id: str) -> bool:
        """True if this counterparty consistently picks off our quotes.

        Level 1: never consider anyone toxic.
        """
        return False

        # ═══════════════════════════════════════════
        # LEVEL 5 TODO: Toxic Flow Detection
        # ═══════════════════════════════════════════


# ---------------------------------------------------------------------------
# BrokerBot — the market maker process
# ---------------------------------------------------------------------------

class BrokerBot:
    """WebSocket market maker that quotes on one AlgoArena exchange.

    Multi-venue (Level 6): set EXCHANGE_URLS=ws://host:8765,ws://host:8766
    and the __main__ entrypoint runs one BrokerBot per venue concurrently —
    the same quoting logic keeps prices in line across exchanges.
    """

    def __init__(self, exchange_url: str | None = None) -> None:
        self.state = BrokerState()
        # The quoting universe: seeded from config, grown by new listings.
        self._quote_symbols: list[str] = list(config.EQUITY_SYMBOLS)
        self.exchange_url = exchange_url or config.EXCHANGE_URL
        self._ws: Any = None
        self._session_open = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # Multi-venue: only one instance polls Yahoo; the rest read its dict.
        self._own_yahoo_feed = True

    # ------------------------------------------------------------------
    # Exchange connection
    # ------------------------------------------------------------------

    async def connect_to_exchange(self) -> None:
        """Open a WebSocket to the exchange and send the Handshake."""
        self._ws = await websockets.connect(self.exchange_url)
        hs = Handshake(team_id=config.TEAM_ID, role="broker", level=1,
                       token=config.ARENA_TOKEN)
        await self._ws.send(hs.model_dump_json())
        logger.info("Connected to %s as %s (broker)", self.exchange_url, config.TEAM_ID)

    async def _send(self, msg: Any) -> None:
        """Send a Pydantic message to the exchange, ignoring a closed connection."""
        try:
            await self._ws.send(msg.model_dump_json())
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Could not send %s — connection closed", type(msg).__name__)

    # ------------------------------------------------------------------
    # Incoming message handlers
    # ------------------------------------------------------------------

    async def listen_to_exchange(self) -> None:
        """Receive and dispatch messages from the exchange until disconnect."""
        async for raw in self._ws:
            try:
                msg = parse_message(json.loads(raw))
            except (KeyError, ValueError) as exc:
                logger.warning("Unrecognised message: %s", exc)
                continue

            if isinstance(msg, BookSnapshot):
                # The venue's own mark is what we quote around. Prefer
                # ref_price: it carries the shared fundamental and survives a
                # one-sided book, whereas mid_price is (mostly) an echo of our
                # own quotes. Older venues send no ref_price — fall back.
                ref = msg.ref_price or msg.mid_price
                if ref and ref > 0:
                    self.state.exchange_prices[msg.symbol] = ref
                    self.state.price_history.setdefault(
                        msg.symbol, deque(maxlen=100)).append(ref)
                # New listings (IPOs) join the quoting universe the moment
                # the venue snapshots them — a market maker's job is to make
                # the first market in a new name. Futures are not equities.
                if (getattr(msg, "asset_type", "equity") == "equity"
                        and msg.symbol not in self._quote_symbols):
                    self._quote_symbols.append(msg.symbol)
                    self.state.resting_orders.setdefault(msg.symbol, {
                        "buy_id": None, "buy_qty": 0,
                        "sell_id": None, "sell_qty": 0,
                        "buy_ids": [], "sell_ids": [],
                    })
            elif isinstance(msg, OrderAck):
                self._on_order_ack(msg)
            elif isinstance(msg, TradeExecution):
                self._on_trade(msg)
            elif isinstance(msg, PortfolioUpdate):
                self._on_portfolio(msg)
            elif isinstance(msg, SessionEvent):
                await self._on_session_event(msg)
            elif isinstance(msg, Leaderboard):
                self._on_leaderboard(msg)
            elif isinstance(msg, ErrorMsg):
                logger.warning("Exchange error [%s]: %s", msg.code, msg.message)

    def _on_order_ack(self, msg: OrderAck) -> None:
        """Record the server-assigned order_id for our resting quote."""
        if msg.team_id != config.TEAM_ID:
            return
        resting = self.state.resting_orders.setdefault(
            msg.symbol, {"buy_id": None, "buy_qty": 0,
                         "sell_id": None, "sell_qty": 0,
                         "buy_ids": [], "sell_ids": []}
        )
        if msg.side == "buy":
            resting.setdefault("buy_ids", []).append(msg.order_id)
            if resting["buy_id"] is None:
                resting["buy_id"] = msg.order_id     # touch level
        else:
            resting.setdefault("sell_ids", []).append(msg.order_id)
            if resting["sell_id"] is None:
                resting["sell_id"] = msg.order_id

    def _on_trade(self, msg: TradeExecution) -> None:
        """Update position and track partial fills."""
        if msg.buyer_id == config.TEAM_ID:
            our_side = "buy"
        elif msg.seller_id == config.TEAM_ID:
            our_side = "sell"
        else:
            return

        resting = self.state.resting_orders.get(msg.symbol, {})
        our_fee = round(msg.fee / 2, 8)

        if our_side == "buy":
            remaining = resting.get("buy_qty", 0) - msg.quantity
            if remaining <= 0:
                resting["buy_id"]  = None
                resting["buy_qty"] = 0
            else:
                resting["buy_qty"] = remaining
            self.state.positions[msg.symbol] = (
                self.state.positions.get(msg.symbol, 0) + msg.quantity
            )
            logger.info("Fill: bought  %d %s @ %.4f (fee=%.4f, remaining=%d)",
                        msg.quantity, msg.symbol, msg.price, our_fee, max(0, remaining))
        else:
            remaining = resting.get("sell_qty", 0) - msg.quantity
            if remaining <= 0:
                resting["sell_id"]  = None
                resting["sell_qty"] = 0
            else:
                resting["sell_qty"] = remaining
            self.state.positions[msg.symbol] = (
                self.state.positions.get(msg.symbol, 0) - msg.quantity
            )
            logger.info("Fill: sold    %d %s @ %.4f (fee=%.4f, remaining=%d)",
                        msg.quantity, msg.symbol, msg.price, our_fee, max(0, remaining))

        self.state.fills += 1

    def _on_portfolio(self, msg: PortfolioUpdate) -> None:
        """Sync cash and positions from the authoritative server copy."""
        self.state.cash          = msg.cash
        self.state.positions     = dict(msg.positions)
        self.state.spread_income = msg.realized_pnl

    async def _on_session_event(self, msg: SessionEvent) -> None:
        if msg.event == "SESSION_PREOPEN":
            # Quote into the opening auction: orders rest without matching,
            # and the cross needs a book to clear against.
            self._session_open = True
            logger.info("Pre-open — quoting into the opening auction")
        elif msg.event == "SESSION_OPEN":
            self._session_open = True
            logger.info("Session open — quoting begins")
        elif msg.event == "SESSION_CLOSED":
            self._session_open = False
            logger.info("Session closed — flattening all positions")
            await self.flatten_all()
        elif msg.event in ("TRADING_HALT", "MARKET_HALT"):
            logger.warning("Market halt: %s", msg.message)
        elif msg.event in ("TRADING_RESUMED", "MARKET_RESUMED"):
            logger.info("Trading resumed: %s", msg.message)
        else:
            logger.info("Session event: %s — %s", msg.event, msg.message)

    def _on_leaderboard(self, msg: Leaderboard) -> None:
        for entry in msg.brokers:
            if entry.get("team_id") == config.TEAM_ID:
                logger.info(
                    "Leaderboard tick=%d | realized=%.2f fills=%d net_worth=%.2f",
                    msg.tick,
                    entry.get("realized_pnl", 0),
                    self.state.fills,
                    entry.get("net_worth", 0),
                )
                break

    # ------------------------------------------------------------------
    # Quoting
    # ------------------------------------------------------------------

    def quote_centre(self, symbol: str) -> float | None:
        """The price to quote around: the venue's mark, anchored on Yahoo.

        A market maker does not invent prices — it quotes around the market's
        own reference and lets order flow move it. So the centre is the venue's
        latest mark (BookSnapshot.ref_price: microprice blended with the shared
        fundamental, plus live trade impact), pulled slowly toward the external
        Yahoo reference when one is available.

        The pull is slow on purpose — REFERENCE_HALFLIFE seconds to close half
        the gap — because Yahoo is a 5-second poll of a different market. Fast
        anchoring would import that feed's staleness as noise; no anchoring at
        all would let the venue drift away from the real security forever.

        There is no randomness anywhere in here. Quotes move when the market
        moves or when the anchor moves, and that is the whole list. (Until
        Aug 2026 this ran a private Ornstein-Uhlenbeck walk with a 200%
        annualized vol, which added a ~0.06% random jump every 0.5s to every
        symbol on every venue independently — pure injected noise that no
        strategy could read and that pulled venues apart.)
        """
        venue = self.state.exchange_prices.get(symbol)
        yahoo = self.state.yahoo_prices.get(symbol)
        if not venue:
            return yahoo or None
        if not yahoo:
            return venue
        # Exponential pull toward the external reference.
        halflife = max(config.REFERENCE_HALFLIFE, config.REQUOTE_INTERVAL_SEC)
        alpha = 1.0 - 0.5 ** (config.REQUOTE_INTERVAL_SEC / halflife)
        return venue + alpha * (yahoo - venue)

    @staticmethod
    def requote_threshold(centre: float) -> float:
        """How far the centre may drift, in dollars, before we requote.

        Scaled with price (REQUOTE_THRESHOLD_BPS) because staleness only means
        anything in relative terms, with REQUOTE_THRESHOLD as an absolute floor
        so we never chase sub-penny noise.
        """
        return max(config.REQUOTE_THRESHOLD,
                   abs(centre) * config.REQUOTE_THRESHOLD_BPS / 10_000.0)

    def needs_requote(self, symbol: str, centre: float) -> bool:
        """True if this symbol's quote is missing, stale, or off the market.

        Requoting on a timer alone means cancelling and replacing a perfectly
        good quote 120 times a minute: it burns the message quota, pays a
        cancel fee every week that charges one, loses queue priority, and
        leaves the book one-sided for a moment on every single cycle. Quote
        only when the market has actually moved past `requote_threshold`, or
        when there is nothing resting to defend.

        Too LOOSE a threshold is its own bug: a resting quote that is allowed
        to drift far from the mark sits behind another desk's, and the touch
        (and every student's chart) jumps between the two whenever one side is
        taken. That is why the threshold is measured in basis points.
        """
        last = self.state.last_quote_prices.get(symbol)
        if last is None:
            return True                        # never quoted this symbol
        resting = self.state.resting_orders.get(symbol, {})
        if not resting.get("buy_qty") or not resting.get("sell_qty"):
            return True                        # a side was filled or pulled
        if abs(centre - last) >= self.requote_threshold(centre):
            return True                        # the market moved
        age = time.time() - self.state.last_quote_time.get(symbol, 0.0)
        return age >= config.QUOTE_MAX_AGE_SEC  # backstop: never rest forever

    async def quote_symbol(self, symbol: str) -> None:
        """Post a fresh bid + ask around the centre, if the market moved.

        Level 1 quoting: symmetric, fixed width, fixed size. The interesting
        parts — widening on volatility (Level 3), skewing on inventory
        (Level 4) — are the student's job; see compute_spread / compute_skew.
        """
        centre = self.quote_centre(symbol)
        if not centre:
            return
        if not self.needs_requote(symbol, centre):
            return

        spread = self.state.compute_spread(symbol)
        skew   = self.state.compute_skew(symbol)
        half   = spread / 2
        # Quote on the penny grid: the venue snaps anyway (buys down, sells
        # up), so snapping here keeps our tracked quote equal to the resting
        # one — otherwise every requote decision compares different prices.
        bid_price = math.floor((centre - half + skew) * 100) / 100
        ask_price = math.ceil((centre + half + skew) * 100) / 100

        if bid_price <= 0 or ask_price <= bid_price:
            logger.debug("Degenerate quote for %s — skipping (centre=%.4f)",
                         symbol, centre)
            return

        await self.cancel_quotes(symbol)

        if not self._session_open:
            return

        # A real book is a LADDER, not a single quote pair: post QUOTE_LEVELS
        # levels per side, each one half-spread further out and bigger than
        # the last (the touch is small and tight; depth is cheap and wide).
        half_step = max(half, 0.01)
        total_size = 0
        for lvl in range(config.QUOTE_LEVELS):
            size = config.QUOTE_SIZE * (lvl + 1)
            lvl_bid = math.floor((bid_price - lvl * half_step) * 100) / 100
            lvl_ask = math.ceil((ask_price + lvl * half_step) * 100) / 100
            if lvl_bid <= 0:
                continue
            await self._send(PlaceOrder(
                team_id=config.TEAM_ID, symbol=symbol, side="buy",
                order_type="limit", price=lvl_bid, quantity=size,
            ))
            await self._send(PlaceOrder(
                team_id=config.TEAM_ID, symbol=symbol, side="sell",
                order_type="limit", price=lvl_ask, quantity=size,
            ))
            total_size += size

        # Track the quote optimistically: the OrderAck fills in the ids we need
        # for cancelling, but the requote decision must not wait for a round
        # trip — otherwise a slow ack means cancel/replacing every cycle again.
        resting = self.state.resting_orders.setdefault(
            symbol, {"buy_id": None, "buy_qty": 0, "sell_id": None,
                     "sell_qty": 0, "buy_ids": [], "sell_ids": []})
        resting["buy_qty"] = total_size
        resting["sell_qty"] = total_size
        self.state.last_quote_prices[symbol] = centre
        self.state.last_quote_time[symbol] = time.time()
        src = "venue+yahoo" if self.state.yahoo_prices.get(symbol) else "venue"
        logger.info("Quoted %s bid=%.4f ask=%.4f centre=%.4f [%s]",
                    symbol, bid_price, ask_price, centre, src)

    async def cancel_quotes(self, symbol: str) -> None:
        """Cancel the resting bid and ask for one symbol."""
        resting = self.state.resting_orders.get(symbol, {})
        for id_key, ids_key, qty_key in (("buy_id", "buy_ids", "buy_qty"),
                                         ("sell_id", "sell_ids", "sell_qty")):
            ids = set(resting.get(ids_key) or [])
            if resting.get(id_key):
                ids.add(resting[id_key])
            for order_id in ids:
                await self._send(CancelOrder(
                    team_id=config.TEAM_ID, order_id=order_id, symbol=symbol,
                ))
            resting[id_key] = None
            resting[ids_key] = []
            resting[qty_key] = 0
        # Nothing is resting, so the next look must quote regardless of moves.
        self.state.last_quote_prices.pop(symbol, None)

    async def cancel_all_quotes(self) -> None:
        """Cancel all resting orders across all symbols."""
        for sym in list(self._quote_symbols):
            try:
                await self.cancel_quotes(sym)
            except Exception:
                logger.exception("Error cancelling quotes for %s", sym)

    async def requote_loop(self) -> None:
        """Wake every REQUOTE_INTERVAL_SEC and requote what has moved.

        The timer is only how often we LOOK; `needs_requote` decides whether
        anything is actually sent, so a quiet market costs no messages.
        """
        # ═══════════════════════════════════════════
        # LEVEL 2 TODO: Smart Requoting
        #
        # The polling loop below reacts at best REQUOTE_INTERVAL_SEC late, and
        # every requote cancels BOTH sides even when only one is wrong.
        # Upgrade it:
        #   * drive requotes from the BookSnapshot handler (event-driven), so
        #     you react on the tick the market moves instead of on a timer
        #   * amend one side at a time — the untouched side keeps its place in
        #     the queue, and the book is never left one-sided
        #   * back off when your own quote is already at the top of the book
        #
        # Measure it: fills per message sent, and how often you are the top of
        # book when a taker arrives.
        # ═══════════════════════════════════════════
        while True:
            await asyncio.sleep(config.REQUOTE_INTERVAL_SEC)
            if not self._session_open:
                continue
            # Stagger the INITIAL book: open at most QUOTE_BURST_SYMBOLS
            # not-yet-quoted symbols per pass, or a fresh connect fires the
            # full ladder for every symbol in one tick and eats the venue's
            # message quota (RATE_LIMITED on the very first look).
            fresh_budget = config.QUOTE_BURST_SYMBOLS
            for sym in list(self._quote_symbols):
                fresh = sym not in self.state.last_quote_prices
                if fresh:
                    if fresh_budget <= 0:
                        continue            # next pass, 0.5s away
                    fresh_budget -= 1
                try:
                    await self.quote_symbol(sym)
                except Exception:
                    logger.exception("requote_loop error for %s", sym)

    # ------------------------------------------------------------------
    # Yahoo Finance price feed
    # ------------------------------------------------------------------

    def start_yahoo_feeds(self) -> None:
        """Poll Yahoo Finance in a background daemon thread.

        yfinance has no WebSocket API, so we poll every YAHOO_POLL_INTERVAL
        seconds.  Returns the last traded price even when markets are closed,
        so this works on weekends and outside trading hours.

        A no-op on the secondary instances of a multi-venue desk: they read the
        primary's dict (see `_run_all_venues`) instead of polling N times.
        """
        if not self._own_yahoo_feed:
            return
        self._loop = asyncio.get_event_loop()
        t = threading.Thread(
            target=self._yahoo_poll_loop,
            args=(config.EQUITY_SYMBOLS,),
            daemon=True,
            name="yahoo-feed",
        )
        t.start()
        logger.info(
            "Yahoo Finance feed started for %s (every %.0f s)",
            config.EQUITY_SYMBOLS, config.YAHOO_POLL_INTERVAL,
        )

    def _yahoo_poll_loop(self, symbols: list[str]) -> None:
        """Runs in the yahoo-feed thread. Never raises — errors are logged."""
        try:
            import yfinance as yf
        except ImportError:
            logger.error("yfinance not installed. Run:  pip install yfinance")
            return

        while True:
            for sym in symbols:
                try:
                    price = float(yf.Ticker(sym).fast_info.last_price)
                    if price > 0:
                        # Reference only. price_history holds the VENUE mark at
                        # requote cadence, which is the series a volatility
                        # estimate wants (see compute_spread, Level 3); a 5s
                        # poll of another market is not.
                        self.state.yahoo_prices[sym] = price
                        logger.debug("Yahoo %s = %.4f", sym, price)
                except Exception as exc:
                    logger.debug("Yahoo fetch failed for %s: %s", sym, exc)
            time.sleep(config.YAHOO_POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Session end
    # ------------------------------------------------------------------

    async def flatten_all(self) -> None:
        """Cancel all open quotes and liquidate all positions at market."""
        await self.cancel_all_quotes()
        for symbol, qty in list(self.state.positions.items()):
            if qty == 0:
                continue
            side = "sell" if qty > 0 else "buy"
            size = abs(qty)
            logger.info("Flattening: %s %d %s at market", side, size, symbol)
            await self._send(PlaceOrder(
                team_id=config.TEAM_ID, symbol=symbol,
                side=side, order_type="market", price=0.0, quantity=size,
            ))

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect, start Yahoo feed, and run quote loops; reconnect on drop."""
        _print_startup()
        self.start_yahoo_feeds()
        retry_delay = 3.0
        while True:
            try:
                await self.connect_to_exchange()
                self._session_open = False
                await asyncio.gather(
                    self.listen_to_exchange(),
                    self.requote_loop(),
                )
            except websockets.exceptions.ConnectionClosed as exc:
                logger.warning("Exchange connection closed: %s — reconnecting in %.0fs",
                               exc, retry_delay)
            except OSError as exc:
                logger.warning("Could not reach exchange: %s — retrying in %.0fs",
                               exc, retry_delay)
            except Exception:
                logger.exception("Unexpected error — reconnecting in %.0fs", retry_delay)
            finally:
                try:
                    await self.cancel_all_quotes()
                except Exception:
                    pass
                if self._ws and not self._ws.closed:
                    await self._ws.close()
            await asyncio.sleep(retry_delay)


# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------

def _print_startup() -> None:
    for name in config.DEPRECATED_ENV:
        logger.warning(
            "%s is set but ignored: the broker no longer runs its own random "
            "walk — quotes follow the venue mark and the Yahoo anchor. See "
            "docs/SEASON_GUIDE.md 'How a price is formed'.", name)
    symbols = ", ".join(config.EQUITY_SYMBOLS)
    lines = [
        "",
        "╔══════════════════════════════════════════════╗",
        "║         AlgoArena — Broker Bot               ║",
        "╠══════════════════════════════════════════════╣",
        f"║  Team:     {config.TEAM_ID:<34}║",
        f"║  Exchange: {config.EXCHANGE_URL:<34}║",
        f"║  Symbols:  {symbols:<34}║",
        f"║  Spread:   ${config.BASE_SPREAD:.2f}  Qty: {config.QUOTE_SIZE:<25}║",
        f"║  Feed:     Yahoo Finance (every {config.YAHOO_POLL_INTERVAL:.0f}s){'':<13}║",
        "╚══════════════════════════════════════════════╝",
        "",
    ]
    print("\n".join(lines))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

async def _run_all_venues() -> None:
    """Run one BrokerBot per exchange in EXCHANGE_URLS (or the single default).

    Quoting the same symbols on every venue is what keeps fragmented markets
    in line — but ONLY if every venue is quoted from the same reference.
    A real multi-venue market maker has one pricing brain and N execution
    gateways, so the instances share their price state (external anchor and
    intraday walk) while keeping per-venue execution state (resting orders,
    positions) separate.
    """
    urls = [u.strip() for u in config.EXCHANGE_URLS if u.strip()]
    if len(urls) <= 1:
        await BrokerBot(urls[0] if urls else None).run()
        return
    logging.getLogger(__name__).info("Multi-venue mode: quoting on %d exchanges", len(urls))
    bots = [BrokerBot(url) for url in urls]
    # One shared pricing brain, N execution gateways: the EXTERNAL reference is
    # shared (there is only one Apple), while each venue's own mark and quote
    # state stay local — a venue's price is its own, and the desk quotes each
    # one around it. Only one instance needs to poll Yahoo.
    for b in bots[1:]:
        b.state.yahoo_prices = bots[0].state.yahoo_prices
        b._own_yahoo_feed = False
    await asyncio.gather(*(b.run() for b in bots))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(_run_all_venues())
