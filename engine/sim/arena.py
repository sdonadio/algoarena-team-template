"""
sim/arena.py — headless arena: the real ExchangeServer, no network.

Design
------
* ExchangeServer is instantiated directly. Portfolios and "connections" are
  created by hand (SimClient stands in for a websocket), so no websockets
  server, no auth, no sleeps.
* One simulated tick = one call to `server.advance_tick()` — the exact same
  code path the live server runs once per second — followed by one `act()`
  call per bot.
* Scheduled shocks shift BOTH registry.prices and the PriceEngine fair value
  so the move persists instead of mean-reverting away instantly.

Everything a bot needs to observe is read straight off the server objects
(books, ref_prices). Orders always go in as PlaceOrder models through
`_handle_place_order`, so order entry exercises every real risk check.

Multi-venue mode (`venues=N`)
----------------------------
N ExchangeServers run side by side, exactly as N team exchanges do in class.
They share the fundamental path (the registry's walk is deterministic in
(symbol, tick) and idempotent per tick) but each forms its own price
endogenously from its own book, so the venues can still disagree — by how
much is the thing worth measuring.

Bots see one venue at a time through a `VenueView`, which is a drop-in for
the arena's own bot-facing API. Before each `act()` the bot's `portfolio` and
`client` are re-pointed at that venue, so every existing preset works per
venue with no changes. Traders are split round-robin; brokers quote every
venue (one shared pricing brain, mirroring broker/broker.py's
`_run_all_venues`); the arbitrageur is attached everywhere and acts once.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import tempfile
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import exchange.config as config
import exchange.persistence as persistence
from exchange.server import ExchangeServer, Portfolio
from shared.messages import ErrorMsg, PlaceOrder

if TYPE_CHECKING:
    from sim.bots import SimBot


# ---------------------------------------------------------------------------
# Fake connection
# ---------------------------------------------------------------------------

class SimClient:
    """Stand-in for a client websocket.

    Keeps only a small tail of messages (a season generates millions) plus a
    cumulative count of rejection codes, which is what balance testing
    actually needs: "how often did the position limit bite?"
    """

    def __init__(self, keep: int = 32) -> None:
        self.sent: deque[str] = deque(maxlen=keep)
        self.errors: dict[str, int] = {}

    async def send(self, data: str) -> None:
        self.sent.append(data)
        if '"type":"error"' in data:
            try:
                code = ErrorMsg.model_validate_json(data).code
            except Exception:
                return
            self.errors[code] = self.errors.get(code, 0) + 1


# ---------------------------------------------------------------------------
# Scheduled market events
# ---------------------------------------------------------------------------

@dataclass
class ScheduledShock:
    """A price shock fired at a known tick.

    Two flavours:

    * synthetic — `pct` is a fraction (-0.08 = down 8%) applied to `symbol`,
      or to every listed symbol when symbol is None. This is what the random
      schedule produces: a clean, reproducible magnitude for balance testing.
    * registered — `shock_id` names a real plugin from
      plugins/shocks/defaults.py. The arena calls the actual shock function,
      so `--shock-type flash_crash` behaves exactly as the teacher's shock
      button does (per-symbol magnitudes, sector logic and all).
    """

    tick: int
    pct: float = 0.0
    symbol: str | None = None
    label: str = ""
    shock_id: str | None = None

    def describe(self) -> str:
        if self.label:
            return self.label
        if self.shock_id:
            return f"{self.shock_id} @ t{self.tick}"
        what = self.symbol or "MARKET"
        return f"{what} {self.pct:+.1%} @ t{self.tick}"


# ---------------------------------------------------------------------------
# Per-venue bot-facing view
# ---------------------------------------------------------------------------

class VenueView:
    """One venue, presented with the same API a bot sees on the arena itself.

    A bot never needs to know whether it is running single- or multi-venue:
    `act(view)` gets prices, order entry and gates for its own venue. The
    cross-venue arbitrageur is the exception — it reaches for `view.views` to
    see every venue at once, which is exactly the extra information a real
    arbitrageur pays for.
    """

    def __init__(self, arena: "HeadlessArena", index: int) -> None:
        self.arena = arena
        self.index = index

    # -- identity -----------------------------------------------------
    @property
    def name(self) -> str:
        return f"venue{self.index + 1}"

    @property
    def views(self) -> list["VenueView"]:
        return self.arena.views

    # -- passthrough --------------------------------------------------
    @property
    def server(self) -> Any:
        return self.arena.servers[self.index]

    @property
    def symbols(self) -> list[str]:
        return self.arena.symbols

    @property
    def scenario(self) -> Any:
        return self.arena.scenario

    @property
    def split_brain(self) -> bool:
        return self.arena.split_brain

    def allows(self, flag: str) -> bool:
        return self.arena.allows(flag)

    # -- market data --------------------------------------------------
    def ref(self, symbol: str) -> float:
        return self.arena.ref(symbol, self.index)

    def mid(self, symbol: str) -> float:
        return self.arena.mid(symbol, self.index)

    def best_bid(self, symbol: str) -> float | None:
        return self.arena.best_bid(symbol, self.index)

    def best_ask(self, symbol: str) -> float | None:
        return self.arena.best_ask(symbol, self.index)

    def best_bid_size(self, symbol: str) -> int:
        return self.arena.best_size(symbol, "bid", self.index)

    def best_ask_size(self, symbol: str) -> int:
        return self.arena.best_size(symbol, "ask", self.index)

    def anchor(self, symbol: str) -> float:
        return self.arena.anchor(symbol)

    def prices(self, symbol: str) -> list[float]:
        return self.arena.prices(symbol, self.index)

    # -- order entry --------------------------------------------------
    async def order(self, bot: "SimBot", symbol: str, side: str, quantity: int,
                    price: float, order_type: str = "limit") -> None:
        await self.arena.order(bot, symbol, side, quantity, price, order_type,
                               venue=self.index)

    def cancel_all(self, bot: "SimBot", symbol: str | None = None) -> int:
        return self.arena.cancel_all(bot, symbol, venue=self.index)


# ---------------------------------------------------------------------------
# The arena
# ---------------------------------------------------------------------------

class HeadlessArena:
    """Runs a population of bots against a real ExchangeServer at full speed."""

    def __init__(
        self,
        bots: list["SimBot"],
        shocks: list[ScheduledShock] | None = None,
        symbols: list[str] | None = None,
        seed: int | None = None,
        history_len: int = 240,
        scenario: Any = None,
        upgrades: dict[str, list[str]] | None = None,
        fire_shocks: bool = True,
        venues: int = 1,
        split_brain: bool = False,
    ) -> None:
        if seed is not None:
            random.seed(seed)

        self.seed = seed
        self.n_venues = max(1, int(venues))
        # See SimBot.centre(): market makers quote every venue off one shared
        # anchor unless this is set, in which case each venue is quoted off its
        # own endogenous price and the venues are free to drift.
        self.split_brain = bool(split_brain)
        self.bots: list[SimBot] = list(bots)
        self.shocks: list[ScheduledShock] = sorted(
            shocks or [], key=lambda s: s.tick
        )
        self.fired_shocks: list[ScheduledShock] = []
        # False when the exchange's own calendar engine applies the moves
        # (--weeks mode): the schedule is then used only for attribution
        # windows and for what the shock predictor is allowed to know.
        self.fire_shocks = fire_shocks

        # Recording off: the simulator is not a class session.
        self._orig_record = config.RECORD_SESSIONS
        config.RECORD_SESSIONS = False

        # Latency off: the tiers are wall-clock delays and the headless arena
        # has no wall clock, so honouring them would just sleep. Quotas and
        # cancellation fees ARE simulated — they are tick-based and matter for
        # balance. See docs/ROADMAP.md "Deviations".
        self._orig_latency = (config.LATENCY_MS_DEFAULT,
                              config.LATENCY_MS_COLOCATED)
        config.LATENCY_MS_DEFAULT = 0.0
        config.LATENCY_MS_COLOCATED = 0.0

        # Halts must not sleep wall-clock time on a virtual clock: a single
        # market-wide L1 halt is 3× this value in asyncio.sleep, which used
        # to stall a "season in a second" for real minutes. 0.05s keeps the
        # halt observable (it fires, blocks orders, resumes) without waiting.
        self._orig_halt = config.HALT_DURATION_SEC
        config.HALT_DURATION_SEC = 0.05

        # Isolate season persistence AND the roster: the simulator must never
        # read or overwrite real class state.
        self._orig_season_path = persistence.SEASON_PATH
        self._orig_roster_path = config.ROSTER_PATH
        self._sim_season_dir = tempfile.mkdtemp(prefix="algoarena_sim_")
        persistence.SEASON_PATH = os.path.join(
            self._sim_season_dir, "season.json")
        config.ROSTER_PATH = self._write_sim_roster(bots, upgrades or {})

        # Season persistence is single-file, so N venues writing the same
        # checkpoint would interleave portfolios that only exist per venue.
        # Multi-venue runs are always ephemeral.
        self._orig_season_persist = config.SEASON_PERSIST
        if self.n_venues > 1:
            config.SEASON_PERSIST = "false"

        # Every venue shares the one module-level registry. That is safe
        # because ArenaRegistry.tick_prices is idempotent per tick: N venues
        # calling it take ONE step of the shared fundamental, and all of them
        # see the same prices — one piece of news, many exchanges.
        self.servers = [ExchangeServer() for _ in range(self.n_venues)]
        self.server = self.servers[0]
        # Run under a specific week's rule set when one is supplied, so the
        # simulator exercises the same gates the students will hit.
        if scenario is not None:
            for srv in self.servers:
                srv.set_scenario(scenario)
        self.scenario = self.server.scenario
        self.views = [VenueView(self, i) for i in range(self.n_venues)]
        self._reset_prices()

        # Default listing excludes the index future unless the week enables
        # it — otherwise every bot would quote a contract it cannot trade.
        if symbols is None:
            symbols = [
                s for s in self.server.books
                if not config.is_future(s) or self.scenario.flag("futures_enabled")
            ]
        self.symbols: list[str] = list(symbols)
        unknown = [s for s in self.symbols if s not in self.server.books]
        if unknown:
            raise ValueError(f"Unlisted symbols requested: {unknown}")
        # Symbols on the books RIGHT NOW: a caller-restricted universe stays
        # restricted to these; only securities listed later (IPOs) join it.
        self._preexisting: set[str] = {
            s for srv in self.servers for s in srv.books}

        # Reference-price history per venue per symbol, for signal bots.
        # `history` stays the venue-1 dict so single-venue callers are unchanged.
        self.histories: list[dict[str, deque[float]]] = [
            {sym: deque(maxlen=history_len) for sym in self.symbols}
            for _ in range(self.n_venues)
        ]
        self.history: dict[str, deque[float]] = self.histories[0]
        # Cross-venue mid gap per symbol, sampled every tick (coherence).
        self.mid_gaps: dict[str, list[float]] = {sym: [] for sym in self.symbols}
        # Every tick-to-tick reference-price move, as (tick, symbol, |move|).
        # This is the price-quality series: a venue whose mid teleports shows
        # up here as a fat tail, and there is no other way to see it.
        self.price_moves: list[tuple[int, str, float]] = []
        # Arb edge captured by every arbitrageur, in basis points.
        self.arb_edges_bps: list[float] = []

        # Net-worth path per bot, one entry per tick (shock attribution).
        self.equity: dict[str, list[float]] = {b.team_id: [] for b in self.bots}
        self.ticks_run = 0

        self._attach_bots()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _write_sim_roster(
        self, bots: list["SimBot"], granted: dict[str, list[str]]
    ) -> str:
        """Write a throwaway roster so upgrades resolve through the real path.

        Each bot is its own one-seat team, which lets the ROI comparison grant
        an upgrade to exactly one participant and have `config_for_team()`
        pick it up exactly as it would in a class.
        """
        roster = {}
        for bot in bots:
            keys = granted.get(bot.team_id) or []
            entry: dict[str, Any] = {"color": "#22d3a0"}
            if bot.role == "broker":
                entry["brokers"] = [bot.team_id]
                entry["broker"] = bot.team_id
                entry["traders"] = []
            else:
                entry["traders"] = [bot.team_id]
            if keys:
                entry["upgrades"] = {k: True for k in keys}
            roster[bot.team_id] = entry
        path = os.path.join(self._sim_season_dir, "teams.json")
        with open(path, "w") as f:
            json.dump(roster, f, indent=2)
        return path

    def _reset_prices(self) -> None:
        """Rewind the global registry to base prices.

        The registry is a module-level singleton, so consecutive runs in one
        process (e.g. --weeks) would otherwise inherit the previous season's
        closing prices.
        """
        reg = self.server.registry
        reg.reset_tick()
        for sec_id, entry in reg.securities.items():
            base = entry["defn"].base_price
            entry["current_price"] = base
            reg.prices[sec_id] = base
        for srv in self.servers:
            srv.ref_prices = dict(reg.prices)
            for sym, engine in srv.price_engines.items():
                base = reg.prices.get(sym, engine.fair_value)
                engine.fair_value = base
                engine.market_price = base
                engine.impact_buffer = 0.0
                engine.session_open = base

    def _venues_for(self, bot: "SimBot", trader_index: int) -> list[int]:
        """Which venues a bot is a member of.

        Single venue: everyone on venue 1. Multi-venue: brokers quote every
        exchange (one pricing brain, N gateways — see broker/broker.py), bots
        that declare `all_venues` are attached everywhere, and traders are
        split round-robin so each venue has real flow.
        """
        if self.n_venues == 1:
            return [0]
        if bot.role == "broker" or getattr(bot, "all_venues", False):
            return list(range(self.n_venues))
        return [trader_index % self.n_venues]

    def _attach_bots(self) -> None:
        """Create a Portfolio + SimClient per bot per venue, as a handshake would.

        A bot attached to several venues posts capital at each of them, so its
        starting cash is SPLIT rather than multiplied — total capital in the
        game is the same however many venues are running.
        """
        trader_index = 0
        for bot in self.bots:
            venues = self._venues_for(bot, trader_index)
            if bot.role != "broker" and not getattr(bot, "all_venues", False):
                trader_index += 1
            bot.venues = venues
            bot.portfolios = {}
            bot.clients = {}
            per_venue_cash = bot.cash / len(venues)
            for v in venues:
                p = Portfolio(
                    team_id=bot.team_id, role=bot.role, level=bot.level,
                    cash=per_venue_cash,
                )
                self.servers[v].portfolios[bot.team_id] = p
                client = SimClient()
                self.servers[v].clients[bot.team_id] = client
                bot.portfolios[v] = p
                bot.clients[v] = client
            # Default view: the bot's primary venue.
            bot.portfolio = bot.portfolios[venues[0]]
            bot.client = bot.clients[venues[0]]
            bot.arena = self

    # ------------------------------------------------------------------
    # Bot-facing market data
    # ------------------------------------------------------------------

    def ref(self, symbol: str, venue: int = 0) -> float:
        """Current reference (mark) price on one venue."""
        return self.servers[venue].ref_prices.get(symbol, 0.0)

    def anchor(self, symbol: str) -> float:
        """The exogenous price every venue shares — a broker's external feed."""
        return float(self.server.registry.prices.get(symbol, 0.0))

    def mid(self, symbol: str, venue: int = 0) -> float:
        """Book mid when two-sided, else the reference price."""
        book = self.servers[venue].books.get(symbol)
        if book is not None:
            m = book.mid_price()
            if m:
                return m
        return self.ref(symbol, venue)

    def best_bid(self, symbol: str, venue: int = 0) -> float | None:
        book = self.servers[venue].books.get(symbol)
        return book.best_bid() if book else None

    def best_ask(self, symbol: str, venue: int = 0) -> float | None:
        book = self.servers[venue].books.get(symbol)
        return book.best_ask() if book else None

    def best_size(self, symbol: str, side: str, venue: int = 0) -> int:
        """Displayed size at the touch — what an arbitrageur can actually lift."""
        book = self.servers[venue].books.get(symbol)
        if book is None:
            return 0
        snap = book.get_snapshot()
        levels = snap.get("bids" if side == "bid" else "asks") or []
        return int(levels[0][1]) if levels else 0

    def prices(self, symbol: str, venue: int = 0) -> list[float]:
        """Reference-price history for one venue, oldest first."""
        return list(self.histories[venue].get(symbol, ()))

    # ------------------------------------------------------------------
    # Bot-facing order entry
    # ------------------------------------------------------------------

    async def order(
        self,
        bot: "SimBot",
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        order_type: str = "limit",
        venue: int | None = None,
    ) -> None:
        """Submit one order through the real order-entry path."""
        qty = int(quantity)
        if qty < 1:
            return
        qty = min(qty, config.MAX_ORDER_SIZE)
        v = bot.venues[0] if venue is None else venue
        msg = PlaceOrder(
            team_id=bot.team_id, symbol=symbol, side=side,
            order_type=order_type, price=round(max(price, 0.01), 2),
            quantity=qty,
        )
        await self.servers[v]._handle_place_order(
            bot.clients[v], msg, bot.team_id)

    def cancel_all(self, bot: "SimBot", symbol: str | None = None,
                   venue: int | None = None) -> int:
        """Pull a bot's resting orders (one symbol, or every symbol)."""
        v = bot.venues[0] if venue is None else venue
        srv = self.servers[v]
        books = ([srv.books[symbol]] if symbol else list(srv.books.values()))
        return sum(b.cancel_team_orders(bot.team_id) for b in books)

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    async def open_session(self) -> None:
        for srv in self.servers:
            await srv.open_session()
            # Weeks with an opening auction enter a pre-open instead of
            # opening directly — drain it so the sim starts on a real open,
            # exactly as the live exchange would after N wall-clock ticks.
            guard = 0
            while srv.auction_phase == "preopen" and guard < 1000:
                await srv.advance_tick()
                guard += 1

    async def close_session(self) -> None:
        for srv in self.servers:
            await srv.close_session()
            # Weeks with a closing auction DEFER the close behind a
            # pre-close window; without draining it the session never
            # closes and the week's scores are never banked.
            guard = 0
            while srv.auction_phase == "preclose" and guard < 1000:
                await srv.advance_tick()
                guard += 1

    async def run(self, ticks: int) -> None:
        """Advance the game `ticks` times, acting every bot on every tick."""
        for _ in range(ticks):
            await self.step()

    async def step(self) -> None:
        """One tick: prices advance, scheduled shocks fire, bots act."""
        for srv in self.servers:
            await srv.advance_tick()
        self.ticks_run += 1
        tick = self.server.tick
        self._absorb_new_listings()
        self._collect_ipo_indications()

        for v, hist in enumerate(self.histories):
            for sym in hist:
                hist[sym].append(self.ref(sym, v))
        self._sample_price_moves(tick)
        if self.n_venues > 1:
            self._sample_coherence()

        while self.shocks and self.shocks[0].tick <= tick:
            await self._fire(self.shocks.pop(0))

        single = self.n_venues == 1
        for bot in self.bots:
            for v in (bot.venues if not single else (0,)):
                if v not in bot.act_venues:
                    continue
                # Re-point the bot at this venue: every preset reads
                # self.portfolio / self.client, so nothing else changes.
                bot.portfolio = bot.portfolios[v]
                bot.client = bot.clients[v]
                if bot.portfolio.liquidated:
                    continue
                try:
                    await bot.act(self if single else self.views[v])
                except Exception as exc:        # a bad bot must not stop the sim
                    bot.errors.append(f"t{tick}: {type(exc).__name__}: {exc}")
            bot.portfolio = bot.portfolios[bot.venues[0]]
            bot.client = bot.clients[bot.venues[0]]

        marks = [srv._bidask_marks() for srv in self.servers]
        for bot in self.bots:
            self.equity[bot.team_id].append(sum(
                bot.portfolios[v].net_worth(self.servers[v].ref_prices, marks[v])
                for v in bot.venues
            ))

    def _absorb_new_listings(self) -> None:
        """Make a mid-session listing (an IPO) visible to every bot.

        The symbol universe was snapshotted at construction; a security the
        venue creates later must join the tradeable list and grow price
        history on each venue, or no sim bot could ever quote or trade it.
        """
        for v, srv in enumerate(self.servers):
            hist = self.histories[v]
            for sym in srv.books:
                if sym in hist or sym in self._preexisting:
                    continue
                if config.is_future(sym) and not self.scenario.flag(
                        "futures_enabled"):
                    continue
                maxlen = (next(iter(hist.values())).maxlen
                          if hist else 240)
                hist[sym] = deque(maxlen=maxlen)
                if sym not in self.symbols:
                    self.symbols.append(sym)
                self.mid_gaps.setdefault(sym, [])

    def _collect_ipo_indications(self) -> None:
        """Poll every bot once per open deal on the issuing venue.

        Uses the same public surface a student sees: the deal dict and the
        bot's own cash. A bot that returns None passed; resubmission is
        allowed by the engine but the sim asks each bot only once.
        """
        for v, srv in enumerate(self.servers):
            if not getattr(srv, "ipo_issuance", True):
                continue
            for deal in srv.ipos.values():
                if deal.state != "open":
                    continue
                for bot in self.bots:
                    if v not in bot.venues or bot.team_id in deal.subs:
                        continue
                    try:
                        ind = bot.ipo_indication(
                            deal.to_public(), bot.portfolios[v].cash)
                    except Exception as exc:   # a bad bot must not stop the sim
                        bot.errors.append(
                            f"ipo:{deal.symbol}: {type(exc).__name__}: {exc}")
                        continue
                    if not ind:
                        continue
                    qty, px = ind
                    deal.subscribe(bot.team_id, int(qty), float(px))

    def _sample_price_moves(self, tick: int) -> None:
        """Record this tick's |price move| per venue per symbol."""
        for hist in self.histories:
            for sym in self.symbols:
                h = hist.get(sym)
                if h is None or len(h) < 2 or h[-2] <= 0:
                    continue
                self.price_moves.append((tick, sym, abs(h[-1] / h[-2] - 1.0)))

    def event_ticks(self) -> list[int]:
        """Ticks on which a price event broke — scheduled shocks and calendar.

        Both sources matter: `--shocks N` fires from the arena's own schedule,
        while `--weeks` leaves the firing to the exchange's calendar engine.
        """
        ticks = {sh.tick for sh in (self.fired_shocks + self.shocks)}
        for srv in self.servers:
            cal = getattr(srv, "calendar", None)
            for ev in getattr(cal, "events", ()):  # scenario weeks
                ticks.add(int(ev.tick))
        return sorted(ticks)

    def price_quality(self, pre: int = 2, post: int = 45) -> dict[str, Any]:
        """Distribution of tick-to-tick price moves OUTSIDE event windows.

        Shocks and calendar prints are *supposed* to move a price several
        percent in a few ticks, so measuring them would hide the thing worth
        measuring: how violently the price moves when nothing has happened.
        A real large-cap moves ~0.01-0.02% per half-second; a p95 in whole
        percent means the venue is teleporting rather than discovering.
        """
        windows = [(t - pre, t + post) for t in self.event_ticks()]

        def quiet(tick: int) -> bool:
            return not any(lo <= tick <= hi for lo, hi in windows)

        rows = [(m, sym, t) for t, sym, m in self.price_moves if quiet(t)]
        if not rows:
            return {"samples": 0, "median": 0.0, "p95": 0.0, "max": 0.0,
                    "max_symbol": None, "max_tick": None,
                    "windows": len(windows)}
        moves = sorted(m for m, _, _ in rows)
        worst = max(rows)

        def pct(q: float) -> float:
            return moves[min(len(moves) - 1, int(q * len(moves)))]

        return {
            "samples": len(moves),
            "median": pct(0.50),
            "p95": pct(0.95),
            "max": worst[0],
            "max_symbol": worst[1],
            "max_tick": worst[2],
            "windows": len(windows),
        }

    def _sample_coherence(self) -> None:
        """Record the cross-venue mid gap per symbol for this tick."""
        for sym in self.symbols:
            mids = [self.mid(sym, v) for v in range(self.n_venues)]
            mids = [m for m in mids if m > 0]
            if len(mids) < 2:
                continue
            centre = sum(mids) / len(mids)
            if centre <= 0:
                continue
            self.mid_gaps[sym].append((max(mids) - min(mids)) / centre)

    def coherence(self) -> dict[str, Any]:
        """How far apart the venues are, and what the arbitrageurs captured.

        `max_gap` is the widest END-OF-RUN gap between any two venues' mids
        for a single symbol, as a fraction of the mid. A coherent fragmented
        market keeps this inside a couple of spreads; the 70% gaps observed
        before there was an arbitrageur are what this number exists to catch.
        """
        finals: dict[str, float] = {}
        for sym in self.symbols:
            mids = [m for m in (self.mid(sym, v)
                                for v in range(self.n_venues)) if m > 0]
            if len(mids) < 2:
                continue
            centre = sum(mids) / len(mids)
            if centre > 0:
                finals[sym] = (max(mids) - min(mids)) / centre
        avg_path = [g for gaps in self.mid_gaps.values() for g in gaps]
        worst = max(finals, key=lambda s: finals[s]) if finals else None
        return {
            "venues": self.n_venues,
            "final_gaps": finals,
            "max_gap": finals.get(worst, 0.0) if worst else 0.0,
            "max_gap_symbol": worst,
            "mean_gap": (sum(avg_path) / len(avg_path)) if avg_path else 0.0,
            "arbs": len(self.arb_edges_bps),
            "avg_edge_bps": (sum(self.arb_edges_bps) / len(self.arb_edges_bps))
                            if self.arb_edges_bps else 0.0,
        }

    async def _fire(self, shock: ScheduledShock) -> None:
        """Apply a scheduled shock to prices AND fair values.

        Shifting the PriceEngine fair value (not just the market price) is
        what makes the move persist: the impact buffer decays back to the new
        fair value instead of to the pre-shock level.

        With fire_shocks=False the move is left to the exchange's calendar
        engine and this only records the event for attribution.
        """
        if not self.fire_shocks:
            self.fired_shocks.append(shock)
            return
        if shock.shock_id:
            self._fire_registered(shock.shock_id)
            self.fired_shocks.append(shock)
            return
        targets = [shock.symbol] if shock.symbol else list(self.symbols)
        for sym in targets:
            if sym not in self.server.books:
                continue
            # The anchor moves once — it is one piece of news — but every
            # venue's own fair value has to be shifted with it.
            new_price = max(0.01, self.ref(sym) * (1.0 + shock.pct))
            self.server.registry.prices[sym] = new_price
            if sym in self.server.registry.securities:
                self.server.registry.securities[sym]["current_price"] = new_price
            for v, srv in enumerate(self.servers):
                venue_price = max(0.01, self.ref(sym, v) * (1.0 + shock.pct))
                engine = srv.price_engines.get(sym)
                if engine:
                    engine.update_fair_value(venue_price)
                srv.ref_prices[sym] = venue_price
        self.fired_shocks.append(shock)

    def _fire_registered(self, shock_id: str) -> None:
        """Fire a REAL shock plugin, then push the move into every venue.

        The plugin runs exactly once (it reads the shared anchor, and running
        it per venue would compound the move), and the per-symbol percentage
        it produced is then applied to each venue's own fair value — one piece
        of news reaching N exchanges.

        Applied as a step rather than through the calendar's ramp, matching
        the synthetic path above; --weeks mode is where ramps are exercised.
        """
        reg = self.server.registry
        before = dict(reg.prices)
        try:
            result = reg.apply_shock(shock_id, {})
        except KeyError:
            raise ValueError(f"Unknown shock {shock_id!r}") from None
        for sym, new_price in result.prices.items():
            prev = before.get(sym) or 0.0
            if prev <= 0 or sym not in self.server.books:
                continue
            pct = new_price / prev - 1.0
            for srv in self.servers:
                venue_price = max(0.01, srv.ref_prices.get(sym, prev) * (1.0 + pct))
                engine = srv.price_engines.get(sym)
                if engine:
                    engine.update_fair_value(venue_price)
                srv.ref_prices[sym] = venue_price

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def net_worth(self, team_id: str) -> float:
        total = 0.0
        for srv in self.servers:
            p = srv.portfolios.get(team_id)
            if p is not None:
                total += p.net_worth(srv.ref_prices, srv._bidask_marks())
        return total

    def window_pnl(self, team_id: str, start: int, end: int) -> float:
        """Net-worth change over ticks [start, end] (1-based tick numbers)."""
        curve = self.equity.get(team_id) or []
        if not curve:
            return 0.0
        lo = max(0, min(start - 1, len(curve) - 1))
        hi = max(0, min(end - 1, len(curve) - 1))
        return curve[hi] - curve[lo]

    def allows(self, flag: str) -> bool:
        """Whether the active week unlocks a mechanic (bots adapt on this)."""
        return self.scenario.flag(flag)

    def close(self) -> None:
        """Restore any global state the arena changed."""
        config.RECORD_SESSIONS = self._orig_record
        (config.LATENCY_MS_DEFAULT,
         config.LATENCY_MS_COLOCATED) = self._orig_latency
        config.HALT_DURATION_SEC = self._orig_halt
        persistence.SEASON_PATH = self._orig_season_path
        config.ROSTER_PATH = self._orig_roster_path
        config.SEASON_PERSIST = self._orig_season_persist
        shutil.rmtree(self._sim_season_dir, ignore_errors=True)

    def summary(self) -> dict[str, Any]:
        """Everything the report needs, as plain data.

        Per-bot figures are summed over the venues that bot is a member of, so
        a broker quoting three exchanges reads as one desk — which is what it
        is. Venue-level totals (fills, exchange revenue) are summed too.
        """
        marks = [srv._bidask_marks() for srv in self.servers]
        rows = []
        for bot in self.bots:
            venues = bot.venues
            ps = [bot.portfolios[v] for v in venues]
            stats = [self.servers[v].part_stats.get(bot.team_id) or {}
                     for v in venues]
            curve = self.equity.get(bot.team_id) or []
            rejects: dict[str, int] = {}
            for v in venues:
                for code, n in bot.clients[v].errors.items():
                    rejects[code] = rejects.get(code, 0) + n
            rows.append({
                "team_id": bot.team_id,
                "preset": bot.preset,
                "role": bot.role,
                "venues": len(venues),
                "starting_cash": sum(p.starting_cash for p in ps),
                # Equity at the end of tick 1, i.e. AFTER the session-open
                # share grant. Measuring return from starting_cash instead
                # would credit every bot with the free grant as "profit".
                "start_equity": (curve[0] if curve
                                 else sum(p.starting_cash for p in ps)),
                "net_worth": sum(
                    p.net_worth(self.servers[v].ref_prices, marks[v])
                    for v, p in zip(venues, ps)),
                "realized_pnl": sum(p.realized_pnl for p in ps),
                "unrealized_pnl": sum(
                    p.unrealized_pnl(self.servers[v].ref_prices)
                    for v, p in zip(venues, ps)),
                "fees": sum(p.total_fees_paid for p in ps),
                "rebates": sum(p.total_rebates_earned for p in ps),
                "carry": sum(p.total_carry_paid for p in ps),
                "liquidated": any(p.liquidated for p in ps),
                "trades": sum(st.get("trade_count", 0) for st in stats),
                "volume": sum(st.get("volume", 0.0) for st in stats),
                "maker_count": sum(st.get("maker_count", 0) for st in stats),
                "rejects": rejects,
                "bot_errors": list(bot.errors[:3]),
                # Only shock predictors have this; the report uses it to label
                # timing-only versus timing+direction.
                "insider": getattr(bot, "insider", None),
            })
        return {
            "ticks": self.ticks_run,
            "trades": sum(srv.trade_count for srv in self.servers),
            "exchange_revenue": sum(srv.exchange_revenue for srv in self.servers),
            "seed": self.seed,
            "bots": rows,
            "shocks": list(self.fired_shocks),
            "prices": {s: self.server.ref_prices.get(s, 0.0)
                       for s in self.symbols},
            "coherence": self.coherence() if self.n_venues > 1 else None,
            "price_quality": self.price_quality(),
        }
