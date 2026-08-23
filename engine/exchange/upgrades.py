"""
exchange/upgrades.py — the purchasable-upgrade catalog and purchase engine.

Teams spend capital during a purchase window (weeks 4 and 7) to buy a
permanent edge: a bigger position limit, better margin terms, a better fee
tier, and — from Phase 4 — a bigger message quota and colocation. The cost is
debited pro-rata from the team's bots' cash held by the exchange, and the
proceeds go to the hosting exchange team, because colocation and market data
are venue revenue in reality.

Where things live
-----------------
* WHAT an upgrade does — `exchange/config.py: UPGRADE_EFFECTS`, read per
  check by `config.config_for_team()`.
* WHAT it costs and how it reads in the shop — `CATALOG` here.
* Ownership — the roster (`teacher/teams.json`), under each team's
  `"upgrades": {"fee_tier": true}`. The roster is the single source of truth
  so a restarted exchange keeps every purchase.

`teacher/registration.py` re-exports CATALOG as UPGRADE_CATALOG: the economy
module is the teacher-facing entry point, but the table itself must live on
the engine side because the student template ships `exchange/` and never
ships `teacher/`.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading

import exchange.config as config
import shared.roster as roster_shape

logger = logging.getLogger(__name__)

# ── Prices ────────────────────────────────────────────────────────────────────
# Repriced in Phase 10 off a 42-season ROI study. The launch prices (15–25% of
# the ~$650k a team allocates to bot cash) were a trap: the shop looked like a
# decision and was actually a tax.
#
# What the study measured, per session:
#   fee_tier        +$5.6k for the highest-churn desk — the only consistently
#                   valuable upgrade. Its value scales with your OWN volume and
#                   comes mostly through the maker rebate, not the taker cut.
#   everything else never binds for a classroom-scale field. Nobody hits a
#                   1,000-share cap, a quota, or a maintenance call often
#                   enough for the old prices to pay back over eight sessions.
#
# So fee_tier is priced where a busy desk breaks even in a handful of sessions,
# and the rest are priced as OPTIONALITY: cheap enough to be a real choice,
# never so cheap that buying the whole shop is correct.
PRICE_POSITION_LIMIT =  30_000
PRICE_MARGIN_PLUS    =  40_000
PRICE_FEE_TIER       =  40_000
PRICE_ORDER_QUOTA    =  30_000
PRICE_COLOCATION     =  50_000
# Phase 10 additions: a data product, an insurance policy, and a report.
PRICE_CALENDAR_FEED  =  35_000
PRICE_RISK_SHIELD    =  45_000
PRICE_ANALYTICS_PRO  =  25_000


CATALOG: dict[str, dict] = {
    "position_limit": {
        "key": "position_limit",
        "label": "Position limit increase",
        "price": PRICE_POSITION_LIMIT,
        "description": (
            "Raises your per-symbol position cap to 2,000 shares. Lets a "
            "working signal be sized up — and lets a broken one hurt more. "
            "Worthless unless you are actually hitting the cap: check your "
            "rejected orders before you buy."
        ),
        "effect": "per-symbol limit → 2,000 shares",
        "roles": ("trader", "broker"),
    },
    "margin_plus": {
        "key": "margin_plus",
        "label": "Prime brokerage terms",
        "price": PRICE_MARGIN_PLUS,
        "description": (
            "Borrow against 65% of long inventory instead of 50%, at a "
            "discounted financing rate. Market-maker economics: more "
            "inventory capacity per dollar of capital. Pays only if you "
            "actually run out of buying power."
        ),
        "effect": "margin haircut 0.50 → 0.65, cheaper carry on borrowed cash",
        "roles": ("broker",),
    },
    "fee_tier": {
        "key": "fee_tier",
        "label": "Volume fee tier",
        "price": PRICE_FEE_TIER,
        "description": (
            "Taker fee cut from 0.15% to 0.12% and maker rebate raised from "
            "0.10% to 0.12%. Measured at about +$5.6k a session for the "
            "busiest desk in the field, most of it from the rebate — so it is "
            "worth roughly your own volume times 2 bps. Work out your "
            "break-even before buying; a quiet desk never gets it back."
        ),
        "effect": "taker fee → 0.12%, maker rebate → 0.12%",
        "roles": ("trader", "broker"),
    },
    "order_quota": {
        "key": "order_quota",
        "label": "Message quota increase",
        "price": PRICE_ORDER_QUOTA,
        "description": (
            "Doubles your order and cancel allowance per tick. Only matters "
            "once quotas go live in week 7, and only if you are being "
            "throttled — watch for QUOTA rejections first."
        ),
        "effect": "order/cancel rate limit doubled",
        "roles": ("trader", "broker"),
    },
    "colocation": {
        "key": "colocation",
        "label": "Colocation",
        "price": PRICE_COLOCATION,
        "description": (
            "Cuts your exchange message latency from 200ms to 20ms once "
            "latency tiers go live in week 7. The purest speed advantage in "
            "the game — and the clearest market-structure debate. Against a "
            "classroom-sized field it rarely decides a fill; it is priced as "
            "optionality, not as an edge."
        ),
        "effect": "outbound latency 200ms → 20ms",
        "roles": ("trader", "broker"),
    },
    # ── Phase 10 additions ────────────────────────────────────────────────
    "calendar_feed": {
        "key": "calendar_feed",
        "label": "Priority calendar feed",
        "price": PRICE_CALENDAR_FEED,
        "description": (
            "The Bloomberg terminal on your desk. Your bots get the "
            "'event imminent' alert twice as early as everyone else — you are "
            "positioning while the rest of the class is still waiting for the "
            "announcement. Honest about what it is NOT: the week's schedule is "
            "public to everybody, and the direction of the move is never "
            "announced to anyone. You are buying time, not information."
        ),
        "effect": "calendar alerts arrive at 2× the normal announcement lead",
        "roles": ("trader", "broker"),
    },
    "risk_shield": {
        "key": "risk_shield",
        "label": "Margin-call insurance",
        "price": PRICE_RISK_SHIELD,
        "description": (
            "One-time protection. The first time your team would be "
            "force-liquidated, the liquidation is waived, your book is left "
            "exactly as it is, and you get a grace period to fix it yourself. "
            "Then the policy is spent — the second margin call is real. Worth "
            "it if you run leverage; a pure waste if you never come close."
        ),
        "effect": "first forced liquidation waived, once per season",
        "roles": ("trader", "broker"),
    },
    "analytics_pro": {
        "key": "analytics_pro",
        "label": "Execution analytics",
        "price": PRICE_ANALYTICS_PRO,
        "description": (
            "A live TCA panel in MY TEAM: per-symbol slippage against the mid "
            "at the moment of each of your last 200 fills, your maker ratio, "
            "and your fees net of rebates. It changes nothing about how you "
            "trade — it tells you what your execution is actually costing you, "
            "which is the number most desks never look at."
        ),
        "effect": "live execution-quality panel in the portal",
        "roles": ("trader", "broker"),
    },
}

# Upgrades that are consumed rather than owned forever. The roster records
# "used" instead of true once spent, which is truthy — so `owned()` still
# reports it (the shop shows it as spent, not as buyable again).
CONSUMABLE = ("risk_shield",)

# One writer lock for roster mutation inside this process.
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------

def price_of(key: str) -> int:
    """Cost of one upgrade. Raises KeyError if it is not in the catalog."""
    return int(CATALOG[key]["price"])


def owned(team: str) -> dict[str, bool]:
    """Upgrades this team has bought, read fresh from the roster."""
    roster = config._read_roster()
    ups = (roster.get(team) or {}).get("upgrades") or {}
    return {k: bool(v) for k, v in ups.items() if v}


def team_bots(team: str) -> list[str]:
    """Every bot id belonging to a team, per the roster.

    Includes ids that only appear in the `capital` map: a bot holding an
    allocation is one of the team's, and this list decides who funds a
    purchase.
    """
    cfg = config._read_roster().get(team) or {}
    ids = list(roster_shape.bot_ids_of(cfg))
    ids += list((cfg.get("capital") or {}).keys())
    seen, out = set(), []
    for i in ids:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


def raw_state(team: str, key: str):
    """The roster's raw value for one upgrade: True, "used", or None.

    `owned()` flattens everything truthy to True, which is right for the
    engine (a spent shield is not for sale again) but loses the distinction the
    shield check and the shop UI both need.
    """
    roster = config._read_roster()
    ups = (roster.get(team) or {}).get("upgrades") or {}
    return ups.get(key)


def shield_active(team: str) -> bool:
    """True only while the team's margin-call insurance is unspent."""
    return raw_state(team, "risk_shield") is True


def consume(team: str, key: str) -> bool:
    """Mark a consumable upgrade as spent ("used") in the roster.

    Not a revoke: the team still bought it, so the shop must not offer it
    again, and the record of the purchase survives a restart. Returns False if
    the team is unknown, it was never owned, or the write failed.
    """
    with _lock:
        roster = config._read_roster()
        if team not in roster:
            return False
        ups = roster[team].setdefault("upgrades", {})
        if ups.get(key) is not True:
            return False
        ups[key] = "used"
        return _save_roster(roster)


def shop_listing(team: str, purchase_window: bool) -> list[dict]:
    """The shop as the portal should render it, with ownership + availability."""
    have = owned(team)
    out = []
    for key, item in CATALOG.items():
        state = raw_state(team, key)
        out.append({
            "key": key,
            "label": item["label"],
            "price": item["price"],
            "description": item["description"],
            "effect": item["effect"],
            "roles": list(item["roles"]),
            "owned": bool(have.get(key)),
            "spent": state == "used",
            "consumable": key in CONSUMABLE,
            "available": bool(purchase_window) and not have.get(key),
        })
    return out


# ---------------------------------------------------------------------------
# Roster mutation
# ---------------------------------------------------------------------------

def _save_roster(roster: dict) -> bool:
    """Atomically rewrite the roster file."""
    target = config.ROSTER_PATH
    try:
        directory = os.path.dirname(os.path.abspath(target))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(roster, f, indent=2)
            f.write("\n")
        os.replace(tmp, target)
        return True
    except OSError as exc:
        logger.error("Roster write failed (%s)", exc)
        return False


def grant(team: str, key: str) -> bool:
    """Record an upgrade against a team in the roster.

    Returns False if the team is unknown or the write failed. Idempotent:
    granting an owned upgrade succeeds without changing anything.
    """
    with _lock:
        roster = config._read_roster()
        if team not in roster:
            logger.error("grant: unknown team %r", team)
            return False
        ups = roster[team].setdefault("upgrades", {})
        if ups.get(key):
            return True
        ups[key] = True
        return _save_roster(roster)


def revoke(team: str, key: str) -> bool:
    """Remove an upgrade (teacher correction / test cleanup)."""
    with _lock:
        roster = config._read_roster()
        if team not in roster:
            return False
        ups = roster[team].get("upgrades") or {}
        ups.pop(key, None)
        roster[team]["upgrades"] = ups
        return _save_roster(roster)


# ---------------------------------------------------------------------------
# Cost allocation
# ---------------------------------------------------------------------------

def split_cost(bot_cash: dict[str, float], cost: float) -> dict[str, float]:
    """Split `cost` across bots in proportion to their cash.

    Only bots with positive cash contribute — you cannot fund a purchase from
    an already-borrowed account. Rounding is absorbed by the largest
    contributor so the parts sum exactly to `cost`.
    """
    fundable = {b: c for b, c in bot_cash.items() if c > 0}
    total = sum(fundable.values())
    if total <= 0:
        return {}
    shares = {b: round(cost * c / total, 6) for b, c in fundable.items()}
    drift = round(cost - sum(shares.values()), 6)
    if drift and shares:
        biggest = max(shares, key=lambda b: shares[b])
        shares[biggest] = round(shares[biggest] + drift, 6)
    return shares
