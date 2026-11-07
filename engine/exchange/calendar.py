"""
exchange/calendar.py — the market event calendar and shock ramps.

Three event kinds, all declared in the week scenario file:

  earnings    {symbol, tick, magnitude_range}   single name, direction random
  econ_print  {tick, magnitude_range}           market-wide
  dividend    {symbol, tick, amount_per_share}  cash settlement
  flash       {symbol?, tick, magnitude_range, revert_ticks}
              UNANNOUNCED short-lived dislocation (see "Flashes" below)

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

Flashes
-------
A flash is the one event kind that is NOT announced: it never appears in the
CALENDAR feed, the upcoming list or the public scenario. At its tick the
symbol's fair value jumps by ±m% in one tick and walks back to where it was
over `revert_ticks` ticks; a FLASH session event goes out as it happens
(data: symbol, pct, revert_ticks, tick). The house market maker quotes
around the venue mark, so for a few seconds its quotes sit at the dislocated
level: a bot that trades against the flash on the next message and exits on
the revert profits; a bot still chewing through its backlog does not.
Week files may list flashes one by one, or use the expansion form

    {"kind": "flash", "every": 170, "start": 60, "end": 2900,
     "jitter": 40, "avoid": 40, "magnitude_range": [0.006, 0.012],
     "revert_ticks": 3, "symbols": [...optional...], "seed": optional}

which places one flash per `every` ticks (± `jitter`), skipping anything
within `avoid` ticks of the week's other events, with the symbol, side and
size drawn from a dedicated RNG. That RNG is seeded from "seed", else the
FLASH_SEED env var, else the global RNG at load time — reproducible in the
simulator (which seeds the global RNG) and unknowable to students live.
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
FLASH = "flash"
PRICE_KINDS = (EARNINGS, ECON_PRINT)

# Flash defaults. 0.6–1.2% in one tick stays far inside every collar week 7
# runs: LULD_BAND_PCT 0.08 (orders > 8% from the mark), the 5%/60 s velocity
# halt and the 25% session band. The server also skips a flash on a symbol
# that is halted, mid-ramp, or already close to the velocity threshold.
FLASH_MAGNITUDE_RANGE = (0.006, 0.012)
FLASH_REVERT_TICKS = 3
FLASH_AVOID_TICKS = 40

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
    # Flash only: ticks to walk back to the pre-flash level.
    revert_ticks: int = FLASH_REVERT_TICKS
    # Flash only, set by the expansion form: the move pre-drawn from the
    # flash RNG (so firing never touches the global RNG) and a uniform
    # [0, 1) used to pick the symbol among those eligible at fire time.
    preset_pct: float | None = None
    pick: float | None = None
    symbols: tuple[str, ...] | None = None

    @property
    def unannounced(self) -> bool:
        """True for kinds that are never published ahead of time (flash)."""
        return self.kind == FLASH

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
        if self.preset_pct is not None:
            self.resolved_pct = self.preset_pct
            return self.resolved_pct
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
    if kind not in (EARNINGS, ECON_PRINT, DIVIDEND, FLASH):
        logger.warning("Calendar: ignoring unknown event kind %r", kind)
        return None
    try:
        tick = int(raw.get("tick", 0))
    except (TypeError, ValueError):
        return None
    if tick <= 0:
        return None
    default_mag = FLASH_MAGNITUDE_RANGE if kind == FLASH else (0.03, 0.06)
    mag = raw.get("magnitude_range") or list(default_mag)
    try:
        lo, hi = float(mag[0]), float(mag[1])
    except (TypeError, ValueError, IndexError):
        lo, hi = default_mag
    if lo > hi:
        lo, hi = hi, lo
    if kind == FLASH:
        symbols = raw.get("symbols")
        return CalendarEvent(
            kind=FLASH, tick=tick, symbol=raw.get("symbol"),
            magnitude_range=(lo, hi),
            forced_direction=raw.get("direction"),
            revert_ticks=_revert_ticks(raw),
            symbols=tuple(str(x) for x in symbols) if symbols else None,
        )
    return CalendarEvent(
        kind=kind,
        tick=tick,
        symbol=raw.get("symbol"),
        magnitude_range=(lo, hi),
        amount_per_share=float(raw.get("amount_per_share", 0.0) or 0.0),
        market_wide=bool(raw.get("market_wide")) or kind == ECON_PRINT,
        forced_direction=raw.get("direction"),
    )


def _revert_ticks(raw: dict[str, Any]) -> int:
    try:
        return max(1, int(raw.get("revert_ticks", FLASH_REVERT_TICKS)))
    except (TypeError, ValueError):
        return FLASH_REVERT_TICKS


def is_flash_series(raw: Any) -> bool:
    """True for the expansion form: {"kind": "flash", "every": N, ...}."""
    return (isinstance(raw, dict) and str(raw.get("kind", "")) == FLASH
            and raw.get("every") is not None and raw.get("tick") is None)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return sum(ord(c) * 31 ** i for i, c in enumerate(str(value))) % (2 ** 32)


def flash_seed(raw: dict[str, Any]) -> int:
    """Seed of a flash series' RNG.

    "seed" in the week file pins it exactly (tests, rehearsals). Otherwise
    FLASH_SEED (env) combined with this venue's port, so venues sharing one
    .env still flash INDEPENDENTLY — a flash on one venue only is also a
    cross-venue mispricing. With neither, the global RNG at load time:
    seeded (reproducible) in the simulator, OS-random live, so nobody can
    replay the week file to learn when the flashes come.
    """
    if raw.get("seed") not in (None, ""):
        return _as_int(raw["seed"])
    env = getattr(config, "FLASH_SEED", "")
    if env not in (None, ""):
        return (_as_int(env) * 1_000_003 + int(getattr(config, "PORT", 0))) % (2 ** 32)
    return random.getrandbits(32)


def expand_flash_series(raw: dict[str, Any], busy_ticks: Iterable[int],
                        seed: int | None = None) -> list[CalendarEvent]:
    """Expand {"kind": "flash", "every": N, ...} into individual flashes.

    One flash per `every` ticks from `start` up to `end`, each jittered by
    up to ±`jitter`, and dropped when it lands within `avoid` ticks of a
    tick in `busy_ticks` (the week's scheduled events) or overlaps the
    previous flash. Side, size and the symbol pick are drawn here from a
    dedicated RNG, so the series is a pure function of (raw, busy, seed).
    """
    try:
        every = max(1, int(raw.get("every")))
        start = max(1, int(raw.get("start", every)))
        end = int(raw.get("end", 3000))
        jitter = max(0, int(raw.get("jitter", every // 4)))
        avoid = max(0, int(raw.get("avoid", FLASH_AVOID_TICKS)))
    except (TypeError, ValueError):
        logger.warning("Calendar: unusable flash series %r", raw)
        return []
    mag = raw.get("magnitude_range") or list(FLASH_MAGNITUDE_RANGE)
    try:
        lo, hi = sorted((float(mag[0]), float(mag[1])))
    except (TypeError, ValueError, IndexError):
        lo, hi = FLASH_MAGNITUDE_RANGE
    symbols = raw.get("symbols")
    symbols = tuple(str(x) for x in symbols) if symbols else None
    revert = _revert_ticks(raw)
    rng = random.Random(flash_seed(raw) if seed is None else seed)
    busy = sorted(int(t) for t in busy_ticks)
    out: list[CalendarEvent] = []
    last: int | None = None
    base = start
    while base <= end:
        tick = base + (rng.randint(-jitter, jitter) if jitter else 0)
        # Draw everything for this slot even if it is dropped, so moving
        # one scheduled event does not reshuffle every later flash.
        pct = rng.uniform(lo, hi) * rng.choice((-1.0, 1.0))
        pick = rng.random()
        base += every
        if tick < 1 or tick > end:
            continue
        if any(abs(tick - b) <= avoid for b in busy):
            continue
        if last is not None and tick - last <= revert + 1:
            continue
        out.append(CalendarEvent(
            kind=FLASH, tick=tick, magnitude_range=(lo, hi),
            revert_ticks=revert, preset_pct=pct, pick=pick, symbols=symbols,
        ))
        last = tick
    return out


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
    # Where the path ends, as a fraction of `pct`: 1.0 for news (the move
    # sticks), 0.0 for a flash (the price walks back to start_price).
    end_fraction: float = 1.0
    kind: str = "news"

    @property
    def total_ticks(self) -> int:
        return self.ramp_ticks + self.settle_ticks

    def fraction(self, elapsed: int) -> float:
        """How much of `pct` has been applied after `elapsed` ticks."""
        if elapsed <= 0:
            return 0.0
        if elapsed >= self.total_ticks:
            return self.end_fraction
        if elapsed <= self.ramp_ticks:
            # Rising leg: 0 → overshoot.
            return self.overshoot * (elapsed / self.ramp_ticks)
        if self.settle_ticks <= 0:
            return self.end_fraction
        # Settling leg: overshoot → end_fraction (1.0 unless a flash).
        done = (elapsed - self.ramp_ticks) / self.settle_ticks
        return self.overshoot + (self.end_fraction - self.overshoot) * done

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
        series = []
        for raw in events:
            if is_flash_series(raw):
                series.append(raw)
                continue
            ev = event_from_dict(raw) if isinstance(raw, dict) else None
            if ev is not None:
                self.events.append(ev)
        if series:
            busy = [e.tick for e in self.events]
            for raw in series:
                self.events.extend(expand_flash_series(raw, busy))
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
                if not e.fired and e.tick >= tick
                and not e.unannounced][:limit]

    def all_public(self) -> list[dict[str, Any]]:
        """Public description of every announced event this week, fired or
        not. Flashes are never listed — they are visible only as they fire."""
        return [e.public() for e in self.events if not e.unannounced]

    def flashes(self) -> list[CalendarEvent]:
        """This week's flash events (teacher / simulator use only)."""
        return [e for e in self.events if e.kind == FLASH]

    # -- the tick -----------------------------------------------------

    def due_announcements(self, tick: int) -> list[CalendarEvent]:
        """Events entering their announcement window this tick."""
        out = []
        for ev in self.events:
            if ev.announced or ev.fired or ev.unannounced:
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
            if (ev.early_announced or ev.announced or ev.fired
                    or ev.unannounced):
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

    def start_flash(self, symbol: str, start_price: float, pct: float,
                    revert_ticks: int, label: str = "") -> Ramp:
        """Begin a flash: the full move on the next step, then a straight
        walk back to `start_price` over `revert_ticks` ticks."""
        self.ramps = [r for r in self.ramps if r.symbol != symbol]
        ramp = Ramp(
            symbol=symbol, start_price=max(0.01, start_price), pct=pct,
            ramp_ticks=1, settle_ticks=max(1, int(revert_ticks)),
            overshoot=1.0, label=label, end_fraction=0.0, kind=FLASH,
        )
        self.ramps.append(ramp)
        return ramp

    def has_ramp(self, symbol: str) -> bool:
        """True while a news ramp or a flash is driving `symbol`."""
        return any(r.symbol == symbol for r in self.ramps)

    def step_ramps(self) -> dict[str, float]:
        """Advance every active ramp. Returns symbol → new fair value."""
        out: dict[str, float] = {}
        for ramp in list(self.ramps):
            out[ramp.symbol] = ramp.step()
            if ramp.done:
                self.ramps.remove(ramp)
        return out
