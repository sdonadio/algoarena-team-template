"""
sim/bots.py — bot presets for the season simulator.

These are NOT student code and are deliberately simple: each one is a clean
caricature of a real participant so the report can attribute P&L to a
behaviour ("passive MM", "momentum", "shock predictor") rather than to an
implementation detail.

Every bot implements:
    async def act(self, arena) -> None

and reads market data off the arena (books, reference prices, history).
Orders go through arena.order(), i.e. the real risk-checked entry path.
"""

from __future__ import annotations

import random
import statistics
from typing import TYPE_CHECKING, Any, Callable

import exchange.config as config

if TYPE_CHECKING:
    from exchange.server import Portfolio
    from sim.arena import HeadlessArena, ScheduledShock, SimClient


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class SimBot:
    """Base class for every simulated participant."""

    preset: str = "base"
    role: str = "trader"
    #: Attach this bot to EVERY venue in a multi-venue arena (brokers already
    #: are; set this on bots that need to see all venues at once).
    all_venues: bool = False

    def __init__(
        self,
        team_id: str,
        cash: float = 100_000.0,
        symbols: list[str] | None = None,
        level: int = 6,
        **kwargs: Any,
    ) -> None:
        self.team_id = team_id
        self.cash = cash
        self.level = level
        self._symbols = symbols
        self.errors: list[str] = []
        # Filled in by HeadlessArena._attach_bots()
        self.portfolio: "Portfolio" = None      # type: ignore[assignment]
        self.client: "SimClient" = None         # type: ignore[assignment]
        self.arena: "HeadlessArena" = None      # type: ignore[assignment]
        # Venue membership. Single-venue arenas leave this at [0]; the arena
        # re-points `portfolio`/`client` at the venue it is calling act() for.
        self.venues: list[int] = [0]
        self.portfolios: dict[int, "Portfolio"] = {}
        self.clients: dict[int, "SimClient"] = {}
        self.config = kwargs

    # -- helpers ------------------------------------------------------

    @property
    def act_venues(self) -> list[int]:
        """Venues this bot's act() is called for (see CrossVenueArbitrageur)."""
        return self.venues

    def symbols(self, arena: "HeadlessArena") -> list[str]:
        return self._symbols if self._symbols is not None else arena.symbols

    def position(self, symbol: str) -> int:
        return self.portfolio.positions.get(symbol, 0)

    def centre(self, arena: "HeadlessArena", symbol: str) -> float:
        """The price this bot quotes around.

        A market maker present on several venues has ONE pricing brain — its
        external feed — and N execution gateways, exactly as
        broker/broker.py's `_run_all_venues` shares its price dicts. So on
        multiple venues it quotes off the shared anchor rather than each
        venue's own endogenous reference, which is what stops the venues
        drifting apart in the first place.

        With `split_brain` the shared reference is taken away and each venue
        is quoted off its own endogenous price — what the live broker falls
        back to when it has no external feed (`state.exchange_prices`). That
        is the configuration in which venues were observed drifting tens of
        percent apart, and therefore the one where an arbitrageur earns its keep.
        """
        if len(self.venues) > 1 and not getattr(arena, "split_brain", False):
            return arena.anchor(symbol)
        return arena.ref(symbol)

    async def act(self, arena: "HeadlessArena") -> None:
        """Do this tick's work. Overridden by every preset."""
        raise NotImplementedError

    def ipo_indication(self, deal: dict[str, Any],
                       cash: float) -> tuple[int, float] | None:
        """Primary-market participation: (quantity, max_price) or None.

        Called once per open deal with the PUBLIC deal dict (symbol, shares,
        offer_range, window) and this bot's live cash on the issuing venue.
        The default participant passes — exactly like a student template
        whose on_ipo is still a TODO.
        """
        return None


# ---------------------------------------------------------------------------
# Market makers
# ---------------------------------------------------------------------------

class PassiveMarketMaker(SimBot):
    """Two-sided post-only quotes around the mid, with optional inventory skew.

    Post-only means it never pays the taker fee: its entire edge is the
    spread it captures plus the maker rebate. This is the bot that shows
    whether market making is survivable at the current fee schedule.
    """

    preset = "passive_mm"
    role = "broker"

    def __init__(self, team_id: str, cash: float = 250_000.0,
                 half_spread: float = 0.0025, quote_size: int = 25,
                 requote_every: int = 5, skew: float = 0.0,
                 max_inventory: int = 300, **kwargs: Any) -> None:
        super().__init__(team_id, cash, **kwargs)
        self.half_spread = half_spread
        self.quote_size = quote_size
        self.requote_every = requote_every
        self.skew = skew
        self.max_inventory = max_inventory

    def ipo_indication(self, deal: dict[str, Any],
                       cash: float) -> tuple[int, float] | None:
        # A market maker wants opening inventory in the new name — nobody
        # else has any, and the first desk quoting it earns the wide spread.
        # Modest size at the midpoint: this is stock to quote, not a bet.
        lo, hi = deal["offer_range"]
        mid = (lo + hi) / 2
        qty = min(deal["shares"] // 20, int(cash * 0.10 / mid)) if mid > 0 else 0
        return (qty, mid) if qty > 0 else None

    async def act(self, arena: "HeadlessArena") -> None:
        if arena.server.tick % self.requote_every:
            return
        arena.cancel_all(self)
        for sym in self.symbols(arena):
            # Anchor on the reference price, not the book mid: a real broker
            # quotes off its external feed (see broker/broker.py). Anchoring
            # on the mid would be self-referential — the MM's own quotes ARE
            # the mid — and would leave it quoting stale prices through news.
            mid = self.centre(arena, sym)
            if mid <= 0:
                continue
            pos = self.position(sym)
            # Inventory skew, measured in half-spreads: at full inventory the
            # whole quote shifts by `skew` half-spreads against the position,
            # so the reducing side fills first. Scaling by the half-spread
            # (not by price) is what keeps the quote from crossing.
            shift = 0.0
            if self.skew and self.max_inventory:
                shift = (-self.skew * (pos / self.max_inventory)
                         * self.half_spread * mid)
            centre = mid + shift
            bid = centre * (1 - self.half_spread)
            ask = centre * (1 + self.half_spread)
            # Post-only is a later-week unlock; fall back to plain limits
            # while it is gated, exactly as a student bot would have to.
            otype = "post_only" if arena.allows("post_only_allowed") else "limit"
            if pos < self.max_inventory:
                await arena.order(self, sym, "buy", self.quote_size, bid, otype)
            if pos > -self.max_inventory:
                await arena.order(self, sym, "sell", self.quote_size, ask, otype)


class AggressiveMarketMaker(PassiveMarketMaker):
    """Tight two-sided quotes as plain limit orders.

    Because the orders are not post-only they sometimes cross and pay the
    taker fee — the classic "requoting into the spread" mistake the realism
    review observed in the default broker.
    """

    preset = "aggressive_mm"
    role = "broker"

    def __init__(self, team_id: str, cash: float = 250_000.0,
                 half_spread: float = 0.0008, quote_size: int = 40,
                 requote_every: int = 2, **kwargs: Any) -> None:
        super().__init__(team_id, cash, half_spread=half_spread,
                         quote_size=quote_size, requote_every=requote_every,
                         **kwargs)

    async def act(self, arena: "HeadlessArena") -> None:
        if arena.server.tick % self.requote_every:
            return
        arena.cancel_all(self)
        for sym in self.symbols(arena):
            mid = self.centre(arena, sym)
            if mid <= 0:
                continue
            pos = self.position(sym)
            if pos < self.max_inventory:
                await arena.order(self, sym, "buy", self.quote_size,
                                  mid * (1 - self.half_spread))
            if pos > -self.max_inventory:
                await arena.order(self, sym, "sell", self.quote_size,
                                  mid * (1 + self.half_spread))


class HedgedMarketMaker(PassiveMarketMaker):
    """A passive market maker that hedges its inventory with the index future.

    The whole point of ARENA-10 (week 9): a market maker accumulates a basket
    of single names simply by doing its job, and until now the only way to cut
    that directional risk was to stop quoting. Selling the index against the
    book keeps the spread income and sheds most of the beta — leaving only the
    residual (idiosyncratic) risk, which is what a market maker should be paid
    for taking.

    Hedge sizing: target a futures position that offsets the dollar delta of
    the equity book. `hedge_ratio` scales it, so 0.0 reproduces the unhedged
    market maker exactly and 1.0 is a full delta hedge.
    """

    preset = "hedged_mm"
    role = "broker"

    def __init__(self, team_id: str, cash: float = 250_000.0,
                 hedge_symbol: str = "ARENA10", hedge_ratio: float = 1.0,
                 hedge_every: int = 10, hedge_tolerance: int = 15,
                 **kwargs: Any) -> None:
        super().__init__(team_id, cash, **kwargs)
        self.hedge_symbol = hedge_symbol
        self.hedge_ratio = hedge_ratio
        self.hedge_every = hedge_every
        self.hedge_tolerance = hedge_tolerance

    def quote_symbols(self, arena: "HeadlessArena") -> list[str]:
        """Names to make markets in — never the hedge instrument itself."""
        return [s for s in self.symbols(arena) if s != self.hedge_symbol]

    def equity_delta(self, arena: "HeadlessArena") -> float:
        """Dollar delta of the cash-equity book."""
        return sum(
            self.position(sym) * arena.ref(sym)
            for sym in self.quote_symbols(arena)
        )

    async def act(self, arena: "HeadlessArena") -> None:
        # Quote exactly like the passive market maker, minus the future.
        original = self._symbols
        self._symbols = self.quote_symbols(arena)
        try:
            await super().act(arena)
        finally:
            self._symbols = original

        if not self.hedge_ratio or self.hedge_symbol not in arena.symbols:
            return
        if arena.server.tick % self.hedge_every:
            return
        if not arena.allows("futures_enabled"):
            return

        index = arena.ref(self.hedge_symbol)
        if index <= 0:
            return
        # Short the index against a long book, and vice versa.
        want = int(round(-self.equity_delta(arena) * self.hedge_ratio / index))
        gap = want - self.position(self.hedge_symbol)
        if abs(gap) < self.hedge_tolerance:
            return
        side = "buy" if gap > 0 else "sell"
        ref = (arena.best_ask(self.hedge_symbol) if side == "buy"
               else arena.best_bid(self.hedge_symbol))
        if ref:
            await arena.order(self, self.hedge_symbol, side, abs(gap), ref, "ioc")


# ---------------------------------------------------------------------------
# Directional traders
# ---------------------------------------------------------------------------

class MomentumTrader(SimBot):
    """Moving-average crossover: buy when fast MA > slow MA, else sell."""

    preset = "momentum"
    role = "trader"

    def __init__(self, team_id: str, cash: float = 100_000.0,
                 fast: int = 10, slow: int = 40, size: int = 20,
                 max_position: int = 200, every: int = 10,
                 **kwargs: Any) -> None:
        super().__init__(team_id, cash, **kwargs)
        self.fast = fast
        self.slow = slow
        self.size = size
        self.max_position = max_position
        # A real (student) strategy evaluates on a cadence and trades the
        # CROSSING, not the state. The old preset re-bought every tick a
        # crossover persisted — ~3 fills/tick across 10 symbols, a fee
        # donation machine that distorted every balance reading.
        self.every = every
        self._last_sig: dict[str, int] = {}

    async def act(self, arena: "HeadlessArena") -> None:
        if arena.server.tick % self.every:
            return
        for sym in self.symbols(arena):
            h = arena.prices(sym)
            if len(h) < self.slow:
                continue
            fast = sum(h[-self.fast:]) / self.fast
            slow = sum(h[-self.slow:]) / self.slow
            sig = 1 if fast > slow * 1.0002 else (
                -1 if fast < slow * 0.9998 else 0)
            prev = self._last_sig.get(sym, 0)
            if sig == 0 or sig == prev:
                continue                      # no NEW information — hold
            self._last_sig[sym] = sig
            pos = self.position(sym)
            if sig > 0 and pos < self.max_position:
                ask = arena.best_ask(sym)
                if ask:
                    await arena.order(self, sym, "buy", self.size, ask, "ioc")
            elif sig < 0 and pos > -self.max_position:
                bid = arena.best_bid(sym)
                if bid:
                    await arena.order(self, sym, "sell", self.size, bid, "ioc")


class MeanReversionTrader(SimBot):
    """Z-score reversion: sell rich, buy cheap, relative to a rolling window."""

    preset = "mean_reversion"
    role = "trader"

    def __init__(self, team_id: str, cash: float = 100_000.0,
                 window: int = 60, entry_z: float = 1.5, exit_z: float = 0.3,
                 size: int = 20, max_position: int = 200, every: int = 10,
                 **kwargs: Any) -> None:
        super().__init__(team_id, cash, **kwargs)
        self.window = window
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.size = size
        self.max_position = max_position
        # Same recalibration as MomentumTrader: evaluate on a cadence, enter
        # once per threshold crossing, exit when the z-score reverts — the
        # old preset re-entered every tick |z| stayed elevated.
        self.every = every
        self._in_pos: dict[str, int] = {}     # symbol → held direction

    async def act(self, arena: "HeadlessArena") -> None:
        if arena.server.tick % self.every:
            return
        for sym in self.symbols(arena):
            h = arena.prices(sym)
            if len(h) < self.window:
                continue
            w = h[-self.window:]
            mean = sum(w) / len(w)
            sd = statistics.pstdev(w)
            if sd <= 0:
                continue
            z = (w[-1] - mean) / sd
            pos = self.position(sym)
            held = self._in_pos.get(sym, 0)
            if held and abs(z) <= self.exit_z and pos:
                # Reverted — take the trade off.
                side = "sell" if pos > 0 else "buy"
                ref = arena.best_bid(sym) if side == "sell" else arena.best_ask(sym)
                if ref:
                    await arena.order(self, sym, side, abs(pos), ref, "ioc")
                self._in_pos[sym] = 0
            elif not held and z < -self.entry_z and pos < self.max_position:
                ask = arena.best_ask(sym)
                if ask:
                    await arena.order(self, sym, "buy", self.size, ask, "ioc")
                    self._in_pos[sym] = 1
            elif not held and z > self.entry_z and pos > -self.max_position:
                bid = arena.best_bid(sym)
                if bid:
                    await arena.order(self, sym, "sell", self.size, bid, "ioc")
                    self._in_pos[sym] = -1


class NoiseTrader(SimBot):
    """Uninformed flow: random small market orders. The MM's food supply."""

    preset = "noise"
    role = "trader"

    def __init__(self, team_id: str, cash: float = 100_000.0,
                 trade_prob: float = 0.02, size: int = 10,
                 max_position: int = 150, **kwargs: Any) -> None:
        super().__init__(team_id, cash, **kwargs)
        self.trade_prob = trade_prob
        self.size = size
        self.max_position = max_position

    async def act(self, arena: "HeadlessArena") -> None:
        for sym in self.symbols(arena):
            if random.random() > self.trade_prob:
                continue
            pos = self.position(sym)
            side = random.choice(("buy", "sell"))
            if side == "buy" and pos >= self.max_position:
                side = "sell"
            elif side == "sell" and pos <= -self.max_position:
                side = "buy"
            ref = arena.best_ask(sym) if side == "buy" else arena.best_bid(sym)
            if ref:
                await arena.order(self, sym, side, self.size, ref, "ioc")


class LeveragedPunter(SimBot):
    """The margin call waiting to happen: levered inventory, no exits.

    Audit R5: preset fields were too disciplined to ever trigger the
    week-5 liquidation lesson. A cash-limited TRADER cannot lose 60% of
    net worth on classroom-sized moves — only a margin user can. So the
    punter is a BROKER: it borrows against inventory to hoard one symbol
    at maximum size and never sells. When the week's shock lands, its
    conservatively-marked equity breaches maintenance and the exchange
    carries it out. If it survives, THAT is the finding.
    """

    preset = "leveraged_punter"
    role = "broker"

    def __init__(self, team_id: str, cash: float = 100_000.0,
                 symbol: str | None = None, every: int = 10,
                 clip: int = 150, **kwargs: Any) -> None:
        super().__init__(team_id, cash, **kwargs)
        self.symbol = symbol
        self.every = every
        self.clip = clip

    def _doom(self, arena: "HeadlessArena"):
        """(symbol, wrong_side) against the next scheduled shock, if any."""
        tick = arena.server.tick
        for sh in sorted(getattr(arena, "shocks_all", None)
                         or list(arena.shocks), key=lambda x: x.tick):
            if sh.tick <= tick:
                continue
            sym = sh.symbol or self.symbols(arena)[0]
            # Anti-insider: short an up-shock (unlimited loss), long a dump.
            return sym, ("sell" if sh.pct > 0 else "buy")
        return None, None

    def ipo_indication(self, deal: dict[str, Any],
                       cash: float) -> tuple[int, float] | None:
        # Full size at the top of the range, of course. The winner's curse
        # needs a winner.
        lo, hi = deal["offer_range"]
        qty = int(cash * 0.9 / hi) if hi > 0 else 0
        return (qty, hi) if qty > 0 else None

    async def act(self, arena: "HeadlessArena") -> None:
        if self.portfolio.liquidated:
            return
        if arena.server.tick % self.every:
            return
        sym, side = self._doom(arena)
        if sym is None:
            sym, side = (self.symbol or self.symbols(arena)[0]), "buy"
        # A market-wide event coming? Take the wrong side of the WHOLE
        # market — shorts have unlimited loss, which is the point.
        coming = [sh for sh in arena.shocks if sh.tick > arena.server.tick]
        market_wide = any(sh.symbol is None for sh in coming)
        targets = self.symbols(arena) if market_wide else [sym]
        for t in targets:
            if side == "buy":
                px = arena.best_ask(t)
                px = px * 1.01 if px else None
            else:
                px = arena.best_bid(t)
                px = px * 0.99 if px else None
            if px:
                # Another clip on margin, forever, on the WRONG side of
                # the scheduled move. Only the venue's checks say no.
                await arena.order(self, t, side, self.clip, px, "ioc")


class DoNothing(SimBot):
    """Control group: holds its starting grant and never trades.

    Its net worth is the benchmark every other preset must beat — the only
    honest way to read the standings.
    """

    preset = "control"
    role = "trader"

    async def act(self, arena: "HeadlessArena") -> None:
        return


# ---------------------------------------------------------------------------
# The shock predictor
# ---------------------------------------------------------------------------

class ShockPredictor(SimBot):
    """Positions ahead of scheduled shocks, exits after them.

    Two information levels, so the report can price the edge:

    * insider=True  — knows timing AND direction; buys before an up-shock,
      sells before a down-shock. The upper bound on what foreknowledge is
      worth.
    * insider=False — knows only the TIMING (this is what the public event
      calendar announces from Phase 3 on). It cannot pick a side, so it
      straddles: it goes flat before the event and then trades the ramp
      immediately after, capturing the tail of the move rather than the jump.

    Either way it must pay the same fees as everyone else, which is exactly
    the question: is the edge bigger than the round-trip cost?
    """

    preset = "shock_predictor"
    role = "trader"

    def __init__(self, team_id: str, cash: float = 100_000.0,
                 schedule: list["ScheduledShock"] | None = None,
                 lead: int = 20, post: int = 30,
                 notional: float = 80_000.0, max_shares: int = 400,
                 insider: bool = True, exit_delay: int | None = None,
                 **kwargs: Any) -> None:
        super().__init__(team_id, cash, **kwargs)
        self.schedule = sorted(schedule or [], key=lambda s: s.tick)
        self.lead = lead
        self.post = post
        # Events ramp in over SHOCK_RAMP_TICKS with an overshoot, so exiting
        # on the tick the news breaks captures almost none of the move. Hold
        # through the ramp, then unwind into the overshoot.
        if exit_delay is None:
            import exchange.config as _config
            exit_delay = _config.SHOCK_RAMP_TICKS
        self.exit_delay = max(0, exit_delay)
        # Size the bet in dollars, not shares: a 5% move on a $22 stock and
        # on a $720 stock must be worth the same to the report.
        self.notional = notional
        self.max_shares = max_shares
        self.insider = insider

    def _size(self, arena: "HeadlessArena", symbol: str, n_targets: int) -> int:
        """Shares to hold in `symbol` for a full-conviction bet."""
        px = arena.mid(symbol)
        if px <= 0:
            return 0
        per_symbol = self.notional / max(1, n_targets)
        return max(1, min(self.max_shares, int(per_symbol / px)))

    def _active(self, tick: int) -> "ScheduledShock | None":
        """The shock whose [-lead, +post] window contains this tick."""
        for sh in self.schedule:
            if sh.tick - self.lead <= tick <= sh.tick + self.post:
                return sh
        return None

    def ipo_indication(self, deal: dict[str, Any],
                       cash: float) -> tuple[int, float] | None:
        # The information archetype, in the primary market. The insider
        # variant reads the (deterministic) pop and bids the whole range on
        # deals that will trade up — the upper bound on what knowing the
        # truth is worth. The timing-only variant knows nothing about the
        # draw and subscribes a disciplined mid-range clip, like a fund
        # playing every deal small.
        from exchange.ipo import pop_factor
        lo, hi = deal["offer_range"]
        if hi <= 0:
            return None
        if self.insider:
            pop = pop_factor(deal["symbol"])
            if pop < 1.02:
                return None            # breaks issue — let others win it
            qty = int(cash * 0.5 / hi)
            return (qty, hi) if qty > 0 else None
        qty = int(min(self.notional, cash * 0.2) / hi)
        return (qty, (lo + hi) / 2) if qty > 0 else None

    async def act(self, arena: "HeadlessArena") -> None:
        tick = arena.server.tick
        shock = self._active(tick)
        targets = ([shock.symbol] if shock and shock.symbol
                   else self.symbols(arena))

        if shock is None:
            # Outside every window: stay flat.
            await self._flatten(arena, self.symbols(arena))
            return

        if tick <= shock.tick:
            # Pre-event.
            if not self.insider:
                # Timing only — no side to take. Go flat and wait.
                await self._flatten(arena, self.symbols(arena))
                return
            for sym in targets:
                size = self._size(arena, sym, len(targets))
                await self._target(arena, sym, size if shock.pct > 0 else -size)
            return

        # Post-event: the insider rides the ramp to its overshoot, then
        # unwinds; the timing-only bot trades the ramp it can now see.
        if self.insider:
            if tick <= shock.tick + self.exit_delay:
                return                      # hold through the ramp
            await self._flatten(arena, targets)
        else:
            for sym in targets:
                h = arena.prices(sym)
                if len(h) < 5:
                    continue
                size = self._size(arena, sym, len(targets))
                drift = h[-1] - h[-5]
                await self._target(arena, sym, size if drift > 0 else -size)

    async def _target(self, arena: "HeadlessArena", symbol: str,
                      want: int) -> None:
        """Move the position toward `want` with one aggressive clip."""
        gap = want - self.position(symbol)
        if abs(gap) < max(1, abs(want) // 4):
            return
        side = "buy" if gap > 0 else "sell"
        ref = arena.best_ask(symbol) if side == "buy" else arena.best_bid(symbol)
        if ref:
            await arena.order(self, symbol, side, abs(gap), ref, "ioc")

    async def _flatten(self, arena: "HeadlessArena",
                       symbols: list[str]) -> None:
        for sym in symbols:
            pos = self.position(sym)
            if not pos:
                continue
            side = "sell" if pos > 0 else "buy"
            ref = arena.best_bid(sym) if pos > 0 else arena.best_ask(sym)
            if ref:
                await arena.order(self, sym, side, abs(pos), ref, "ioc")


# ---------------------------------------------------------------------------
# The cross-venue arbitrageur
# ---------------------------------------------------------------------------

class CrossVenueArbitrageur(SimBot):
    """In-process version of trader/arb_trader.py.

    Attached to every venue, it looks for an ask on one exchange that is below
    a bid on another by more than both taker fees, and takes both sides with
    IOC orders. That is the only force in a fragmented market that keeps the
    venues' prices in line — without it a venue's endogenous price wanders off
    the shared anchor and nothing pulls it back.

    The decision itself is the SAME pure function the live bot uses
    (`trader.arb_trader.decide_arb`), so what the report measures here is
    what the class will see.
    """

    preset = "arb"
    role = "trader"
    all_venues = True

    def __init__(self, team_id: str, cash: float = 300_000.0,
                 max_clip: int = 20, min_edge_bps: float = 1.0,
                 position_limit: int | None = None, **kwargs: Any) -> None:
        super().__init__(team_id, cash, **kwargs)
        self.max_clip = max_clip
        self.min_edge_bps = min_edge_bps
        self.position_limit = position_limit
        self.arbs = 0

    @property
    def act_venues(self) -> list[int]:
        """Acts once per tick, not once per venue: it sees them all at once."""
        return self.venues[:1]

    async def act(self, arena: "HeadlessArena") -> None:
        from trader.arb_trader import ArbLimits, Quote, decide_arb

        views = getattr(arena, "views", None)
        root = getattr(arena, "arena", arena)   # VenueView → HeadlessArena
        if not views or len(views) < 2:
            return

        quotes: dict[str, dict[str, Quote]] = {}
        for view in views:
            for sym in self.symbols(root):
                quotes.setdefault(sym, {})[view.name] = Quote(
                    bid=view.best_bid(sym), bid_size=view.best_bid_size(sym),
                    ask=view.best_ask(sym), ask_size=view.best_ask_size(sym),
                )
        by_name = {view.name: view.index for view in views}
        # Every venue charges the same schedule in the simulator; the live bot
        # tracks per-venue fees from the FEE_SCHEDULE event instead.
        taker = config.config_for_team(self.team_id, "taker_fee")
        fees = {name: taker for name in by_name}
        limit = self.position_limit
        if limit is None:
            limit = int(config.config_for_team(self.team_id, "position_limit"))
        limits = ArbLimits(
            position_limit=limit or 10 ** 9,
            max_clip=self.max_clip,
            min_edge_bps=self.min_edge_bps,
            positions={name: dict(self.portfolios[idx].positions)
                       for name, idx in by_name.items()},
        )

        for plan in decide_arb(quotes, fees, limits):
            buy_v, sell_v = by_name[plan.buy_venue], by_name[plan.sell_venue]
            await root.order(self, plan.symbol, "buy", plan.quantity,
                             plan.buy_price, "ioc", venue=buy_v)
            await root.order(self, plan.symbol, "sell", plan.quantity,
                             plan.sell_price, "ioc", venue=sell_v)
            root.arb_edges_bps.append(plan.edge_bps)
            self.arbs += 1


# ---------------------------------------------------------------------------
# Registry + population builder
# ---------------------------------------------------------------------------

BOT_PRESETS: dict[str, Callable[..., SimBot]] = {
    "passive_mm": PassiveMarketMaker,
    "aggressive_mm": AggressiveMarketMaker,
    "hedged_mm": HedgedMarketMaker,
    "momentum": MomentumTrader,
    "mean_reversion": MeanReversionTrader,
    "noise": NoiseTrader,
    "control": DoNothing,
    "leveraged_punter": LeveragedPunter,
    "shock_predictor": ShockPredictor,
    "arb": CrossVenueArbitrageur,
}

# The default balance-testing field: two market makers of opposite style,
# one of each signal trader, uninformed flow, a control, and both flavours
# of shock predictor so the report can compare them directly.
DEFAULT_POPULATION: list[tuple[str, dict]] = [
    ("passive_mm", {"team_id": "mm_passive", "skew": 0.4}),
    ("aggressive_mm", {"team_id": "mm_aggressive"}),
    ("momentum", {"team_id": "momentum_1"}),
    ("mean_reversion", {"team_id": "reversion_1"}),
    ("noise", {"team_id": "noise_1"}),
    ("noise", {"team_id": "noise_2"}),
    ("control", {"team_id": "control"}),
    ("leveraged_punter", {"team_id": "punter"}),
    ("shock_predictor", {"team_id": "predictor_insider", "insider": True}),
    ("shock_predictor", {"team_id": "predictor_timing", "insider": False}),
]


def make_population(
    spec: list[tuple[str, dict]] | None = None,
    schedule: list["ScheduledShock"] | None = None,
    symbols: list[str] | None = None,
) -> list[SimBot]:
    """Instantiate a bot population.

    Shock predictors receive the shock schedule; everyone else is blind to it.
    """
    bots: list[SimBot] = []
    for preset, kwargs in (spec or DEFAULT_POPULATION):
        cls = BOT_PRESETS[preset]
        kwargs = dict(kwargs)
        if preset == "shock_predictor":
            kwargs.setdefault("schedule", schedule or [])
        if symbols is not None:
            kwargs.setdefault("symbols", symbols)
        bots.append(cls(**kwargs))
    return bots
