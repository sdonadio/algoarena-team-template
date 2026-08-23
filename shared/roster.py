"""
shared/roster.py — readers for the roster file's shape.

The roster (`teacher/teams.json`) is the single source of truth for who exists,
and it is read by the engine, the teacher tools, the launcher, and the student
template. This module holds the accessors that hide its history, so a schema
quirk needs fixing in one place rather than in nine.

It lives in `shared/` because that is the only package every other one already
depends on: `exchange/`, `teacher/`, `scripts/`, and `sim/` can all import it,
and it imports nothing but the standard library.

The broker field
----------------
A team may run up to `MAX_BROKERS` desks, but the original schema recorded the
broker as a **scalar**:

    {"broker": "rocket_broker", "traders": ["rocket_trader_1", ...]}

so a second desk had nowhere to go. It was written into the `capital` map and
became invisible to `team_for_bot` (rejecting it at handshake on an
AUTH_REQUIRED deployment), to the launcher, and to the portal — a desk a team
had paid for and could not connect.

The schema now carries a **list**, with the scalar kept as its first element so
older rosters and any reader that was missed keep working:

    {"broker": "rocket_broker",
     "brokers": ["rocket_broker", "rocket_broker_2"], ...}

`broker_ids_of` is the one way to read it. It never infers a desk from the
`capital` map: guessing a bot's role from its id would turn a typo into a
silently mis-roled participant.
"""

from __future__ import annotations

__all__ = ["broker_ids_of", "bot_ids_of", "with_broker"]


def broker_ids_of(team_cfg: dict) -> list[str]:
    """Every broker desk id in one roster entry, in seat order.

    Handles all three shapes: the `brokers` list, the legacy `broker` scalar
    alone, and both together (the normal case, where the scalar repeats
    `brokers[0]`). Order follows the list when it is present, because that is
    the authoritative record of who was hired when.
    """
    if not isinstance(team_cfg, dict):
        return []
    out: list[str] = []
    for bot in team_cfg.get("brokers") or []:
        if bot and bot not in out:
            out.append(bot)
    legacy = team_cfg.get("broker")
    if legacy and legacy not in out:
        out.append(legacy)
    return out


def bot_ids_of(team_cfg: dict, include_exchange: bool = True) -> list[str]:
    """Every bot id a roster entry declares, in display order.

    Exchange (optional), then broker desks, then trader seats. Ids that appear
    only in the `capital` map are NOT included: capital is an allocation, not a
    declaration of a seat, and a bot that exists only there has no role.
    """
    if not isinstance(team_cfg, dict):
        return []
    out: list[str] = []
    if include_exchange and team_cfg.get("exchange"):
        out.append(team_cfg["exchange"])
    for bot in broker_ids_of(team_cfg):
        if bot not in out:
            out.append(bot)
    for bot in team_cfg.get("traders") or []:
        if bot and bot not in out:
            out.append(bot)
    return out


def with_broker(team_cfg: dict, bot_id: str) -> list[str]:
    """Add a broker desk to a roster entry in place; return the new list.

    Writes BOTH fields every time: `brokers` is the record, and `broker` stays
    equal to the first desk so a reader that predates the list still resolves
    the team's main desk. Creating `brokers` from the legacy scalar is part of
    the same write, so a roster is migrated the first time it is touched.
    """
    desks = broker_ids_of(team_cfg)
    if bot_id not in desks:
        desks.append(bot_id)
    team_cfg["brokers"] = desks
    team_cfg["broker"] = desks[0]
    return desks
