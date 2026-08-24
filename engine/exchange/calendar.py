"""
exchange/calendar.py — the market event calendar and shock ramps.

Three event kinds, all declared in the week scenario file:

  earnings    {symbol, tick, magnitude_range}   single name, direction random
  econ_print  {tick, magnitude_range}           market-wide
  dividend    {symbol, tick, amount_per_share}  cash settlement

The design decision that makes this teachable: events are **announced in
advance without direction**. Students know an earnings print lands at tick 350
and that it will move NVDA by 3–8%; they do not know which way. Predicting an
event is therefore a strategy (straddle it, size down into it, trade the ramp
after it) rather than a coin flip, and the simulator can price the difference
between knowing the timing and knowing the outcome.

Shock ramps
-----------
Real news does not move a price in one tick. Every price event — calendar or
teacher-injected — is applied as a ramp: the fair value walks to
`overshoot × move` over `ramp_ticks`, then settles back to `move` over half as
many again. Momentum strategies then have something real to catch, and
mean-reversion strategies have the overshoot to fade.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any, Iterable

import exchange.config as config

logger = logging.getLogger(__name__)

EARNINGS = "earnings"
ECON_PRINT = "econ_print"
DIVIDEND = "dividend"
PRICE_KINDS = (EARNINGS, ECON_PRINT)

# How much earlier the priority calendar feed (the `calendar_feed` upgrade)
# hears that an event is imminent, as a multiple of the normal announcement
# lead. 2 = twice as early.
EARLY_LEAD_MULTIPLE = 2


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@dataclass
class CalendarEvent:
    """One scheduled market event."""

    kind: str
    tick: int
    symbol: str | None = None
    magnitude_range: tuple[float, float] = (0.03, 0.06)
    amount_per_share: float = 0.0
    market_wide: bool = False
    # Direction fixed in the scenario file ("up"/"down"); None = random at
    # fire time, which is the normal case.
    forced_direction: str | None = None

    announced: bool = False
    # The early wave, sent only to teams that bought the priority calendar
    # feed. Tracked separately so the normal announcement still fires later
    # for everyone, including them.
    early_announced: bool = False
    fired: bool = False
    # Resolved when the event fires — never announced in advance.
    resolved_pct: float | None = None

    def public(self) -> dict[str, Any]:
        """The version students are allowed to see. No direction, ever."""
        out: dict[str, Any] = {"kind": self.kind, "tick": self.tick}
        if self.symbol:
            out["symbol"] = self.symbol
        if self.market_wide:
            out["market_wide"] = True
        if self.kind == DIVIDEND:
            out["amount_per_share"] = self.amount_per_share
        else:
            out["magnitude_range"] = list(self.magnitude_range)
        return out

    def resolve(self) -> float:
        """Draw the actual signed move. Called once, at fire time."""
        lo, hi = self.magnitude_range
        pct = random.uniform(float(lo), float(hi))
        if self.forced_direction:
            sign = -1.0 if str(self.forced_direction).lower().startswith(
                ("d", "-", "n")) else 1.0
        else:
            sign = random.choice((-1.0, 1.0))
        self.resolved_pct = pct * sign
        return self.resolved_pct


def event_from_dict(raw: dict[str, Any]) -> CalendarEvent | None:
    """Parse one scenario event entry. Returns None if it is unusable."""
    kind = str(raw.get("kind", ""))
    if kind == "ipo":
        return None        # not the calendar's business — server._advance_ipos
    if kind not in (EARNINGS, ECON_PRINT, DIVIDEND):
        logger.warning("Calendar: ignoring unknown event kind %r", kind)
        return None
    try:
        tick = int(raw.get("tick", 0))
    except (TypeError, ValueError):
        return None
    if tick <= 0:
        return None
    mag = raw.get("magnitude_range") or [0.03, 0.06]
    try:
        lo, hi = float(mag[0]), float(mag[1])
    except (TypeError, ValueError, IndexError):
        lo, hi = 0.03, 0.06
    if lo > hi:
        lo, hi = hi, lo
    return CalendarEvent(
        kind=kind,
        tick=tick,
        symbol=raw.get("symbol"),
        magnitude_range=(lo, hi),
        amount_per_share=float(raw.get("amount_per_share", 0.0) or 0.0),
        market_wide=bool(raw.get("market_wide")) or kind == ECON_PRINT,
        forced_direction=raw.get("direction"),
    )


# ---------------------------------------------------------------------------
# Ramps
# ---------------------------------------------------------------------------

@dataclass
class Ramp:
    """A price move being applied gradually to one symbol.

    `start_price` is the fair value when the news broke; `pct` is the total
    move. The path overshoots to `overshoot × pct` and then settles back.
    """

    symbol: str
    start_price: float
    pct: float
    ramp_ticks: int
    settle_ticks: int
    overshoot: float
    elapsed: int = 0
    label: str = ""

    @property
    def total_ticks(self) -> int:
        return self.ramp_ticks + self.settle_ticks

    def fraction(self, elapsed: int) -> float:
        """How much of `pct` has been applied after `elapsed` ticks."""
        if elapsed <= 0:
            return 0.0
        if elapsed >= self.total_ticks:
            return 1.0
        if elapsed <= self.ramp_ticks:
            # Rising leg: 0 → overshoot.
            return self.overshoot * (elapsed / self.ramp_ticks)
        if self.settle_ticks <= 0:
            return 1.0
        # Settling leg: overshoot → 1.0.
        done = (elapsed - self.ramp_ticks) / self.settle_ticks
        return self.overshoot + (1.0 - self.overshoot) * done

    def level(self, elapsed: int) -> float:
        """Absolute fair value after `elapsed` ticks."""
        return max(0.01, self.start_price * (1.0 + self.pct * self.fraction(elapsed)))

    def step(self) -> float:
        """Advance one tick and return the new fair value."""
        self.elapsed += 1
        return self.level(self.elapsed)

    @property
    def done(self) -> bool:
        return self.elapsed >= self.total_ticks


# ---------------------------------------------------------------------------
# The calendar engine
# ---------------------------------------------------------------------------

class MarketCalendar:
    """Holds the week's events, announces them, fires them, and runs ramps."""

    def __init__(
        self,
        events: Iterable[dict[str, Any]] | None = None,
        announce_lead: int | None = None,
        ramp_ticks: int | None = None,
        overshoot: float | None = None,
        settle_ticks: int | None = None,
    ) -> None:
        self.events: list[CalendarEvent] = []
        self.ramps: list[Ramp] = []
        self.announce_lead = (config.CALENDAR_ANNOUNCE_LEAD
                              if announce_lead is None else announce_lead)
        self.ramp_ticks = (config.SHOCK_RAMP_TICKS
                           if ramp_ticks is None else ramp_ticks)
        self.overshoot = (config.SHOCK_OVERSHOOT
                          if overshoot is None else overshoot)
        self._settle_override = settle_ticks
        self.load(events or [])

    # -- setup --------------------------------------------------------

    def load(self, events: Iterable[dict[str, Any]]) -> None:
        """Replace the event list (called on startup and on set_week)."""
        self.events = []
        for raw in events:
            ev = event_from_dict(raw) if isinstance(raw, dict) else None
            if ev is not None:
                self.events.append(ev)
        self.events.sort(key=lambda e: e.tick)

    def reset(self) -> None:
        """Forget every announcement, firing, and ramp (new session/season)."""
        for ev in self.events:
            ev.announced = False
            ev.early_announced = False
            ev.fired = False
            ev.resolved_pct = None
        self.ramps = []

    @property
    def settle_ticks(self) -> int:
        if self._settle_override is not None:
            return self._settle_override
        return max(1, self.ramp_ticks // 2)

    # -- queries ------------------------------------------------------

    def upcoming(self, tick: int, limit: int = 20) -> list[dict[str, Any]]:
        """Public description of events still to come."""
        return [e.public() for e in self.events
                if not e.fired and e.tick >= tick][:limit]

    def all_public(self) -> list[dict[str, Any]]:
        """Public description of every event this week, fired or not."""
        return [e.public() for e in self.events]

    # -- the tick -----------------------------------------------------

    def due_announcements(self, tick: int) -> list[CalendarEvent]:
        """Events entering their announcement window this tick."""
        out = []
        for ev in self.events:
            if ev.announced or ev.fired:
                continue
            if ev.tick - self.announce_lead <= tick:
                ev.announced = True
                out.append(ev)
        return out

    def due_early_announcements(self, tick: int) -> list[CalendarEvent]:
        """Events entering the PRIORITY window — the calendar_feed upgrade.

        The priority window opens at `EARLY_LEAD_MULTIPLE ×` the normal lead,
        so a team that bought the feed is told an event is imminent while the
        rest of the class is still waiting. It is delivery speed, not private
        information: the week's schedule is public either way, and the
        direction is never announced to anybody.

        Marks `early_announced` only, so the normal wave still reaches
        everyone — including the buyers — at the usual time.
        """
        window = int(self.announce_lead * EARLY_LEAD_MULTIPLE)
        out = []
        for ev in self.events:
            if ev.early_announced or ev.announced or ev.fired:
                continue
            if ev.tick - window <= tick:
                ev.early_announced = True
                out.append(ev)
        return out

    def due_events(self, tick: int) -> list[CalendarEvent]:
        """Events whose tick has arrived."""
        out = []
        for ev in self.events:
            if not ev.fired and ev.tick <= tick:
                ev.fired = True
                ev.announced = True
                out.append(ev)
        return out

    def start_ramp(self, symbol: str, start_price: float, pct: float,
                   label: str = "") -> Ramp:
        """Begin applying a price move to one symbol over the ramp period.

        A second ramp on the same symbol replaces the first: the newest news
        supersedes it, measured from wherever the price is now.
        """
        self.ramps = [r for r in self.ramps if r.symbol != symbol]
        ramp = Ramp(
            symbol=symbol, start_price=max(0.01, start_price), pct=pct,
            ramp_ticks=max(1, self.ramp_ticks),
            settle_ticks=self.settle_ticks,
            overshoot=self.overshoot, label=label,
        )
        self.ramps.append(ramp)
        return ramp

    def step_ramps(self) -> dict[str, float]:
        """Advance every active ramp. Returns symbol → new fair value."""
        out: dict[str, float] = {}
        for ramp in list(self.ramps):
            out[ramp.symbol] = ramp.step()
            if ramp.done:
                self.ramps.remove(ramp)
        return out
