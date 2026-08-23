"""
exchange/seats.py — the seat economy: hiring mid-season.

Registration buys a team its opening line-up out of a fixed budget. This module
is the other half: **reinvesting winnings**. During a capital-allocation window
a team that is making money can add a trader seat, a broker desk, or a second
venue, paid for out of the cash its existing bots are holding on the exchange.
A team that is losing money cannot — which is the lesson.

Where things live
-----------------
* The economy constants (budget, licence, minimums, caps) live HERE, and
  `teacher/registration.py` re-exports them. Same reason the upgrade catalog
  lives in `exchange/upgrades.py`: the student template ships `exchange/` and
  never ships `teacher/` (there is a leak check enforcing it), so the engine
  must be able to price and grant a seat with no teacher-side code present.
* The roster (`teacher/teams.json`) is the single source of truth for who
  exists. A new bot's allocated capital goes in the `capital` map, and
  `config.starting_cash_for()` already hands it over the first time that bot
  connects — so the debit and the credit cannot drift apart.
* The exchange does the debit, because it is the only component that can see
  live cash. See `ExchangeServer.purchase_seat`.

A seat is not an upgrade: the money is not spent, it is *moved*. The team's
combined net worth is unchanged the moment a trader seat is bought; what
changes is that a new process can now put it at risk.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

import exchange.config as config
import shared.roster as roster_shape
from exchange.upgrades import _lock, _save_roster, split_cost  # noqa: F401

logger = logging.getLogger(__name__)

# ── The economy (single source of truth) ───────────────────────────────────────
TEAM_BUDGET      = 1_000_000
EXCHANGE_LICENSE =   300_000
MIN_BROKER       =   100_000
MIN_TRADER       =    50_000
MAX_BROKERS      = 2
MAX_TRADERS      = 5
BASE_PORT        = 8765

SEAT_KINDS = ("trader", "broker", "exchange")


SEAT_CATALOG: dict[str, dict] = {
    "trader": {
        "kind": "trader",
        "label": "Trader seat",
        "min_capital": MIN_TRADER,
        "max_per_team": MAX_TRADERS,
        "module": "trader.trader",
        "description": (
            "Another algorithmic seat. Fund it from the cash your existing "
            "bots are sitting on and run a second, different strategy — "
            "diversification is the only free lunch, and one bot cannot be "
            "both a momentum desk and a mean-reversion desk."
        ),
    },
    "broker": {
        "kind": "broker",
        "label": "Broker desk",
        "min_capital": MIN_BROKER,
        "max_per_team": MAX_BROKERS,
        "module": "broker.broker",
        "description": (
            "Another market-making desk. Quoting more names earns more spread "
            "and more maker rebates, and needs more capital to carry the "
            "inventory it accumulates."
        ),
    },
    "exchange": {
        "kind": "exchange",
        "label": "Exchange licence",
        "price": EXCHANGE_LICENSE,
        "max_per_team": 1,
        "module": "exchange.server",
        "description": (
            "Run your own venue: set your own fee schedule, earn the taker "
            "fee net of the rebate on every trade you match, and compete for "
            "flow. A licence fee, not an investment — it buys the right to "
            "charge, not a book."
        ),
    },
}


class SeatError(Exception):
    """Invalid seat purchase — the message is safe to show the student."""


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "team"


def team_slug(team: str, team_cfg: dict) -> str:
    """The bot-id prefix for a team.

    Prefer the prefix its existing bots already use — a team renamed in the
    roster must keep hiring under the same prefix, or `team_of()` stops
    resolving its old bots. Falls back to slugifying the name.
    """
    ids = [*roster_shape.bot_ids_of(team_cfg),
           *list((team_cfg.get("capital") or {}).keys())]
    for bot in ids:
        if not bot:
            continue
        for suffix in ("_trader", "_broker", "_exchange"):
            if suffix in bot:
                return bot.split(suffix)[0]
    return slugify(team)


def all_bot_ids(roster: dict) -> set[str]:
    """Every bot id in the whole roster — new ids must not collide."""
    out: set[str] = set()
    for cfg in roster.values():
        out.update(roster_shape.bot_ids_of(cfg))
        out.update((cfg.get("capital") or {}).keys())
    return out


def seat_counts(team_cfg: dict) -> dict[str, int]:
    """How many of each seat kind a team already holds.

    Counted from the DECLARED seats — the `brokers` list and the `traders`
    list — not from the `capital` map. Counting the capital map meant reading
    a role out of a bot id, which is exactly the guess that let a purchased
    desk go missing in the first place: it made the count look right while the
    desk itself was invisible to every other reader.
    """
    return {
        "trader": len([t for t in (team_cfg.get("traders") or []) if t]),
        "broker": len(roster_shape.broker_ids_of(team_cfg)),
        "exchange": 1 if team_cfg.get("exchange_port") else 0,
    }


def next_bot_id(kind: str, slug: str, roster: dict, hint: str = "") -> str:
    """The next free id for a new seat, e.g. rocket_trader_3.

    `hint` lets a student name the seat (`bot_id: "scalper"` →
    rocket_trader_scalper); it is slugified and ignored if it collides.
    """
    taken = all_bot_ids(roster)
    if kind == "exchange":
        base = f"{slug}_exchange"
        return base if base not in taken else _numbered(base, taken)
    if hint:
        candidate = f"{slug}_{kind}_{slugify(hint)}"
        if candidate not in taken:
            return candidate
    return _numbered(f"{slug}_{kind}", taken)


def _numbered(base: str, taken: set[str]) -> str:
    n = 1
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def next_port(roster: dict, reserved: Iterable[int] = ()) -> int:
    """The lowest exchange port not already claimed.

    The roster is the world as far as the game is concerned, so a port that
    appears in it is taken. `reserved` covers ports that are in use but not in
    the roster — in practice the port the exchange granting the licence is
    itself bound to, which is not in the roster when the teacher started it
    from an env var rather than a roster entry.
    """
    used = {cfg.get("exchange_port") for cfg in roster.values()
            if cfg.get("exchange_port")}
    used |= {int(p) for p in reserved if p}
    port = BASE_PORT
    while port in used:
        port += 1
    return port


def run_command(kind: str, bot_id: str, host: str = "localhost",
                port: int | None = None) -> str:
    """The exact shell line the student runs to bring the new seat online."""
    if kind == "exchange":
        return f"EXCHANGE_PORT={port or BASE_PORT} python -m exchange.server"
    module = SEAT_CATALOG[kind]["module"]
    where = f"EXCHANGE_HOST={host} EXCHANGE_PORT={port or BASE_PORT}"
    return (f"TEAM_ID={bot_id} ARENA_TOKEN=$ARENA_TOKEN {where} "
            f"python -m {module}")


# ---------------------------------------------------------------------------
# Pricing and validation
# ---------------------------------------------------------------------------

def seat_cost(kind: str, capital: int | float | None) -> int:
    """What this seat costs the team, in dollars.

    A trader/broker seat costs exactly the capital being allocated to it — the
    money moves rather than disappearing. An exchange licence has a fixed
    price and buys no book.
    """
    if kind not in SEAT_KINDS:
        raise SeatError(f"Unknown seat kind {kind!r}")
    if kind == "exchange":
        return int(EXCHANGE_LICENSE)
    try:
        amount = int(capital or 0)
    except (TypeError, ValueError):
        raise SeatError("Capital must be a whole number of dollars") from None
    minimum = int(SEAT_CATALOG[kind]["min_capital"])
    if amount < minimum:
        raise SeatError(
            f"A {SEAT_CATALOG[kind]['label'].lower()} needs at least "
            f"${minimum:,} of capital")
    return amount


def check_cap(kind: str, team_cfg: dict) -> None:
    """Raise SeatError if the team is already at its roster cap for `kind`."""
    have = seat_counts(team_cfg).get(kind, 0)
    cap = int(SEAT_CATALOG[kind]["max_per_team"])
    if have >= cap:
        if kind == "exchange":
            raise SeatError("Your team already runs a venue")
        raise SeatError(
            f"Your team already has {have} of a maximum {cap} "
            f"{SEAT_CATALOG[kind]['label'].lower()}s")


def validate(kind: str, capital: int | float | None, team_cfg: dict) -> int:
    """Full pre-flight on a seat request. Returns the cost, or raises."""
    if kind not in SEAT_KINDS:
        raise SeatError(f"Unknown seat kind {kind!r}")
    cost = seat_cost(kind, capital)
    check_cap(kind, team_cfg)
    return cost


# ---------------------------------------------------------------------------
# Roster mutation
# ---------------------------------------------------------------------------

def add_seat(team: str, kind: str, capital: int,
             bot_id_hint: str = "") -> dict:
    """Write a new seat into the roster. Returns the seat description.

    Called by the exchange AFTER every check has passed and immediately before
    the debit, so a failed write cannot leave a team charged for a seat that
    does not exist. Returns {"bot_id", "kind", "capital", "port"}.
    """
    with _lock:
        roster = config._read_roster()
        cfg = roster.get(team)
        if cfg is None:
            raise SeatError(f"Unknown team {team!r}")
        slug = team_slug(team, cfg)
        bot_id = next_bot_id(kind, slug, roster, bot_id_hint)
        port = None

        if kind == "exchange":
            port = next_port(roster, reserved=(config.PORT,))
            cfg["exchange_port"] = port
            cfg["exchange"] = bot_id
            # A new venue competes on price from its first trade, so give it
            # the default schedule explicitly rather than leaving it implicit.
            cfg.setdefault("fees", {
                "taker": round(config.DEFAULT_TAKER_BPS / 10_000.0, 8),
                "rebate": round(config.DEFAULT_REBATE_BPS / 10_000.0, 8),
            })
        elif kind == "trader":
            cfg.setdefault("traders", []).append(bot_id)
            cfg.setdefault("capital", {})[bot_id] = int(capital)
        else:   # broker
            # Writes BOTH `brokers` (the record) and `broker` (= the first
            # desk, for readers that predate the list), migrating a legacy
            # scalar-only entry on the way through.
            roster_shape.with_broker(cfg, bot_id)
            cfg.setdefault("capital", {})[bot_id] = int(capital)

        if not _save_roster(roster):
            raise SeatError("Could not write the roster — tell your teacher")

    logger.info("SEAT %s hired %s (%s) with $%s", team, bot_id, kind,
                f"{capital:,}" if kind != "exchange" else "no book")
    return {"bot_id": bot_id, "kind": kind,
            "capital": 0 if kind == "exchange" else int(capital), "port": port}


def listing(team: str, team_cfg: dict, purchase_window: bool) -> list[dict]:
    """The seat shop as the portal renders it."""
    counts = seat_counts(team_cfg)
    out = []
    for kind, item in SEAT_CATALOG.items():
        have = counts.get(kind, 0)
        cap = int(item["max_per_team"])
        out.append({
            "kind": kind,
            "label": item["label"],
            "description": item["description"],
            "min_capital": item.get("min_capital"),
            "price": item.get("price"),
            "owned": have,
            "max_per_team": cap,
            "available": bool(purchase_window) and have < cap,
        })
    return out
