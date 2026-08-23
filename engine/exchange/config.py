"""
Exchange configuration. Environment variables override all defaults.

See CLAUDE.md for the full list of supported env vars.
"""

import os

import shared.roster as roster_shape

HOST = os.environ.get("EXCHANGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("EXCHANGE_PORT", "8765"))

# Require team tokens at handshake (hosted/AWS deployments). Local play
# defaults to open access. Tokens are issued by the registration API and
# verified against TOKENS_PATH — see shared/auth.py and docs/AWS_DEPLOY.md.
AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "false").lower() in ("true", "1", "yes")
# Hosted arenas run 24/7 so students can practise between classes — reopen the
# session automatically on startup (nightly unattended-upgrades bounce the
# service). Local play keeps the teacher's explicit START.
SESSION_AUTOOPEN = os.environ.get("SESSION_AUTOOPEN", "false").lower() in ("true", "1", "yes")
FEE_RATE = float(os.environ.get("ARENA_FEE_RATE", "0.001"))
# Minimum price increment (Reg NMS Rule 612: one penny). The book snaps
# incoming prices toward the passive side. Ticks are what make queue
# priority real — with infinite granularity, pennying is free.
TICK_SIZE = float(os.environ.get("ARENA_TICK_SIZE", "0.01"))
# Self-trade prevention scope: "bot" (an order never matches ITS OWN resting
# order) or "team" (never matches ANY of its roster team's orders — full
# firm-level STP, disabling intra-team internalization).
STP_SCOPE = os.environ.get("STP_SCOPE", "bot").lower()

INITIAL_CASH = 100_000.0          # starting cash for every team
SNAPSHOT_INTERVAL_SEC = 0.5       # book-snapshot broadcast cadence
LEADERBOARD_INTERVAL_SEC = 2.0    # leaderboard broadcast cadence
MAX_ORDER_SIZE = 500               # max shares / units per single order
SESSION_DURATION_SEC = 3600        # nominal session length (teacher may close early)

# ── Price formation ───────────────────────────────────────────────────────────
# Equity market structure, mapped onto the game:
#
#   fundamental  one shared news process per symbol, identical on every venue
#                (plugins/securities/defaults.py — deterministic in
#                (symbol, tick, FUNDAMENTAL_SEED), so no networking needed)
#   discovery    the venue's own book, read as a depth-weighted MICROPRICE:
#                imbalanced demand moves it toward the heavy side
#   impact       every fill pushes the price in the aggressor's direction
#                (see the impact engine below) — the main intraday driver
#
# How much the traded market (the microprice) outweighs the shared fundamental
# when setting fair value. High, because a real equity's price IS its market;
# the remaining fundamental weight is the common information that keeps
# fragmented venues in line, the way arbitrage and consolidated data do.
MID_BLEND_WEIGHT = float(os.environ.get("MID_BLEND_WEIGHT", "0.85"))

# Largest fair-value move allowed in ONE tick from the blend above, as a
# fraction. Real prices do not teleport: a name that moves 0.2% in a second is
# already moving fast. Shocks, calendar prints and dividends bypass this — they
# own the fair value directly through the ramp scheduler (exchange/calendar.py),
# which is how news is allowed to move a price several percent in seconds.
MAX_TICK_MOVE = float(os.environ.get("MAX_TICK_MOVE", "0.002"))

# When a book is one-sided, empty or torn, the venue keeps its PREVIOUS
# internal reference instead of falling back to a stale last trade (which is
# what used to make the mid teleport during a market maker's cancel/replace).
# A book that stays dead drifts this fraction of the way home to the
# fundamental each tick, so an abandoned symbol converges instead of freezing.
DEAD_BOOK_DECAY = float(os.environ.get("DEAD_BOOK_DECAY", "0.05"))

# ── Price Impact Engine ───────────────────────────────────────────────────────
IMPACT_COEFFICIENT    = 0.0001  # base impact per dollar notional
IMPACT_MODEL          = "sqrt"  # "linear" | "sqrt"  (sqrt is more realistic)
PERMANENT_FRACTION    = 0.30    # fraction of impact that permanently shifts fair value
MEAN_REVERSION_SPEED  = 0.10    # fraction of temp impact that decays per tick

# ── Circuit Breakers (Level 4) ────────────────────────────────────────────────
# Disabled by default so shocks don't freeze the game. Set CIRCUIT_BREAKERS=true to enable.
CIRCUIT_BREAKERS_ENABLED = os.environ.get("CIRCUIT_BREAKERS", "false").lower() in ("true", "1", "yes")
VELOCITY_WINDOW_SEC   = 60      # lookback window for velocity check (seconds)
VELOCITY_HALT_PCT     = 0.05    # halt if price moves 5% in VELOCITY_WINDOW_SEC
SESSION_HALT_PCT      = 0.25    # halt if price moves 25% from session open
# LULD price band: limit prices further than this from the venue mark are
# rejected at entry (the erroneous-order collar every real venue runs).
# Halts stop trading AFTER a move; the band stops the fat-finger PRINT from
# happening at all. 0 disables.
LULD_BAND_PCT = float(os.environ.get("LULD_BAND_PCT", "0.10"))
# Short-sale rule (Rule 201): tripped when a symbol falls this far below the
# session open; from then on shorts may only rest above the bid. 0 disables.
SSR_TRIGGER_PCT = float(os.environ.get("SSR_TRIGGER_PCT", "0.10"))
# Borrow availability: total short interest per symbol across the whole
# market is capped at this many shares (a locate, in effect). 0 = unlimited.
SHORT_LOCATE_CAP = int(os.environ.get("SHORT_LOCATE_CAP", "2000"))
# Private borrow beyond the market pool — granted by the locate_desk upgrade.
SHORT_LOCATE_EXTRA = 0

# ── Auctions ──────────────────────────────────────────────────────────────────
# Opening auction: START enters a pre-open of this many ticks — limit orders
# rest without matching, indicative price/imbalance broadcasts every tick,
# then one volume-maximizing cross opens the market. 0 = open directly
# (the historical behavior; local tests and quick demos want this).
OPENING_AUCTION_TICKS = int(os.environ.get("OPENING_AUCTION_TICKS", "0"))
# Closing auction: the first close request freezes continuous matching for
# this many ticks (orders rest into the closing book), then one cross prints
# the official close and the session ends.
CLOSING_AUCTION = os.environ.get("CLOSING_AUCTION", "false").lower() in ("true", "1", "yes")
CLOSING_AUCTION_TICKS = int(os.environ.get("CLOSING_AUCTION_TICKS", "5"))
HALT_DURATION_SEC     = 300     # default halt duration (5 minutes)
MARKET_WIDE_L1_PCT    = -0.07   # market-wide Level 1 breaker (-7%)
MARKET_WIDE_L2_PCT    = -0.13   # market-wide Level 2 breaker (-13%)

# ── Margin facility ────────────────────────────────────────────────────────────
# Market makers (role=broker) may borrow against their long inventory:
#   buying power = cash + MARGIN_HAIRCUT × long inventory market value
# Borrowed cash (negative balance) pays interest every tick, and short
# positions pay a stock-borrow fee every tick — realistic financing costs
# that punish bad inventory management without killing the market.
MARGIN_ENABLED       = os.environ.get("MARGIN", "true").lower() in ("true", "1", "yes")
MARGIN_HAIRCUT       = float(os.environ.get("MARGIN_HAIRCUT", "0.5"))
MARGIN_RATE_PER_TICK = float(os.environ.get("MARGIN_RATE_PER_TICK", "0.00002"))   # on borrowed cash
BORROW_FEE_PER_TICK  = float(os.environ.get("BORROW_FEE_PER_TICK", "0.00001"))    # on short notional

# ── Position limits & liquidation ──────────────────────────────────────────────
# Per-symbol absolute position cap enforced at order entry (0 = unlimited).
POSITION_LIMIT_SHARES = int(os.environ.get("POSITION_LIMIT_SHARES", "1000"))
# If a team's conservatively-marked net worth falls below
# MAINTENANCE_FRACTION × starting capital, the exchange force-flattens all
# positions at market (with a penalty spread) and bars further trading.
LIQUIDATION_ENABLED  = os.environ.get("LIQUIDATION", "true").lower() in ("true", "1", "yes")
MAINTENANCE_FRACTION = float(os.environ.get("MAINTENANCE_FRACTION", "0.10"))
LIQUIDATION_PENALTY  = float(os.environ.get("LIQUIDATION_PENALTY", "0.01"))   # 1% through the mark
# How long the `risk_shield` upgrade's one-time waiver holds the maintenance
# check off after it fires (≈1 minute at the default half-second tick). Without
# a grace window the waiver would buy exactly one tick: the next check finds
# the same book, and the policy is already spent.
RISK_SHIELD_GRACE_TICKS = int(os.environ.get("RISK_SHIELD_GRACE_TICKS", "120"))

# ── Conservative marking ────────────────────────────────────────────────────────
# Mark longs at best bid and shorts at best ask (when a book exists) for net
# worth and liquidation checks — the way real risk systems mark.
CONSERVATIVE_MARKS = os.environ.get("CONSERVATIVE_MARKS", "true").lower() in ("true", "1", "yes")

# ── Session recording ───────────────────────────────────────────────────────────
# Record every broadcast message to sessions/session_<ts>.jsonl between
# SESSION_OPEN and SESSION_CLOSED. Replay with scripts/replay_session.py.
RECORD_SESSIONS = os.environ.get("RECORD_SESSIONS", "true").lower() in ("true", "1", "yes")
SESSIONS_DIR    = os.environ.get("SESSIONS_DIR", "sessions")
# How many recent fills the server keeps in memory. Cumulative counters
# (trade_count, part_stats) are never trimmed, so trimming the log only
# affects the "recent trades" view, not the leaderboard.
TRADE_LOG_MAXLEN = int(os.environ.get("TRADE_LOG_MAXLEN", "20000"))

# ── Per-team capital allocation ───────────────────────────────────────────────
# Teams created with scripts/create_team.py choose how to invest their budget
# across exchange / broker / trader seats. Allocations are stored in the
# roster (teacher/teams.json) under each team's "capital" map. Bots listed
# there start with their allocated capital instead of INITIAL_CASH.
ROSTER_PATH = os.environ.get(
    "ROSTER_PATH",
    os.path.join(os.path.dirname(__file__), "..", "teacher", "teams.json"),
)

def _read_roster() -> dict:
    """Parse the roster file, or {} if it is missing/unreadable.

    Re-read on every call on purpose: the dashboard writes the roster while
    the exchange is running (registrations, upgrade purchases), and the
    exchange must see those edits without a restart.
    """
    try:
        import json
        with open(ROSTER_PATH) as f:
            roster = json.load(f)
        return roster if isinstance(roster, dict) else {}
    except (OSError, ValueError):
        return {}


# What fraction of a bot's allocation a STUDENT venue funds. The roster
# capital is real money exactly once — at the primary. Before this rule,
# every venue granted the full allocation, so connecting to N venues minted
# N× capital (audit finding R2). A quarter is a margin deposit: enough to
# quote and settle on a secondary venue, not a second fortune.
SECONDARY_VENUE_CASH_FRACTION = float(
    os.environ.get("SECONDARY_VENUE_CASH_FRACTION", "0.25"))


def venue_cash_fraction() -> float:
    """1.0 on the primary venue, SECONDARY_VENUE_CASH_FRACTION on a venue
    whose port belongs to a roster team (a student-licensed venue)."""
    for team_cfg in _read_roster().values():
        try:
            if int(team_cfg.get("exchange_port") or 0) == PORT:
                return SECONDARY_VENUE_CASH_FRACTION
        except (TypeError, ValueError):
            continue
    return 1.0


def funded_cash_for(team_id: str) -> float:
    """The cash THIS venue actually funds the bot with at first connect."""
    return starting_cash_for(team_id) * venue_cash_fraction()


def starting_cash_for(team_id: str) -> float:
    """Return the capital allocated to this bot in the roster, or INITIAL_CASH."""
    for team_cfg in _read_roster().values():
        cap = team_cfg.get("capital") or {}
        if team_id in cap:
            try:
                return float(cap[team_id])
            except (TypeError, ValueError):
                break
    return INITIAL_CASH


def team_of(bot_id: str) -> str | None:
    """Team name owning this bot id, via the roster.

    The `capital` map counts here, unlike in `shared.roster.bot_ids_of`: an
    allocation is proof of ownership even when the seat is not declared, and
    resolving OWNERSHIP too generously only ever costs a team its own upgrades.
    Role is never inferred from it — see shared/roster.py.
    """
    for name, cfg in _read_roster().items():
        if bot_id in roster_shape.bot_ids_of(cfg) \
                or bot_id in (cfg.get("capital") or {}):
            return name
    return None


def upgrades_for(bot_id: str) -> dict:
    """The owning team's purchased upgrades: {"fee_tier": true, ...}."""
    team = team_of(bot_id)
    if team is None:
        return {}
    ups = _read_roster().get(team, {}).get("upgrades") or {}
    return ups if isinstance(ups, dict) else {}


# ── Purchasable upgrades (the engine-side effect table) ───────────────────────
# Prices and shop copy live in teacher/registration.py (the economy owns
# those); the engine owns what each upgrade DOES. The key list here is the
# single source of truth for both.
#
# Each tunable maps to its config attribute, the upgrades that improve it, and
# which direction "better" is — so an upgrade can never make a team worse off
# than the base configuration.
UPGRADE_EFFECTS: dict[str, dict] = {
    "position_limit": {
        "attr": "POSITION_LIMIT_SHARES",
        "by_upgrade": {"position_limit": 2000},
        "lower_is_better": False,
    },
    "margin_haircut": {
        "attr": "MARGIN_HAIRCUT",
        "by_upgrade": {"margin_plus": 0.65},
        "lower_is_better": False,
    },
    "margin_rate": {
        "attr": "MARGIN_RATE_PER_TICK",
        # A financing discount: cheaper carry on borrowed cash.
        "by_upgrade": {"margin_plus": 0.000014},
        "lower_is_better": True,
    },
    "locate_extra": {
        "attr": "SHORT_LOCATE_EXTRA",
        # A securities-lending relationship: private borrow beyond the pool.
        "by_upgrade": {"locate_desk": 2000},
        "lower_is_better": False,
    },
    "taker_fee": {
        "attr": "TAKER_FEE_RATE",
        "by_upgrade": {"fee_tier": 0.0012},
        "lower_is_better": True,
    },
    "maker_rebate": {
        "attr": "MAKER_REBATE_RATE",
        "by_upgrade": {"fee_tier": 0.0012},
        "lower_is_better": False,
    },
    "order_quota": {
        "attr": "ORDER_QUOTA_PER_TICK",
        # Doubling is applied relative to the base quota, so a week that
        # tightens the quota still leaves the upgrade worth twice as much.
        "by_upgrade": {"order_quota": None},
        "multiplier": {"order_quota": 2.0},
        "lower_is_better": False,
    },
    "latency_ms": {
        "attr": "LATENCY_MS_DEFAULT",
        "by_upgrade": {"colocation": None},
        "from_attr": {"colocation": "LATENCY_MS_COLOCATED"},
        "lower_is_better": True,
    },
}


def config_for_team(bot_id: str, key: str):
    """Resolve a tunable for one bot: base config value, then upgrades.

    The base value already reflects any week-scenario override, because
    scenarios write straight onto these module globals (exchange/scenario.py).
    Upgrades are looked up per call so a mid-session purchase takes effect
    immediately.
    """
    spec = UPGRADE_EFFECTS.get(key)
    if spec is None:
        raise KeyError(f"Unknown per-team tunable: {key!r}")
    if key in ("taker_fee", "maker_rebate"):
        # The venue's own schedule is the base for both fee tunables, so a
        # live roster edit reaches settlement (throttled — see
        # refresh_venue_fees).
        refresh_venue_fees()
    base = globals()[spec["attr"]]
    value = base
    owned = upgrades_for(bot_id)
    lower_is_better = bool(spec.get("lower_is_better"))

    for upgrade_key, fixed in spec["by_upgrade"].items():
        if not owned.get(upgrade_key):
            continue
        if fixed is not None:
            candidate = fixed
        elif upgrade_key in (spec.get("multiplier") or {}):
            # Relative effect (e.g. "double the quota"), so a week that
            # tightens the base still leaves the upgrade proportionally worth it.
            candidate = base * spec["multiplier"][upgrade_key]
        elif upgrade_key in (spec.get("from_attr") or {}):
            candidate = globals()[spec["from_attr"][upgrade_key]]
        else:
            continue
        # Upgrades only ever help: keep whichever value favours the team.
        value = min(value, candidate) if lower_is_better else max(value, candidate)
    return value

# ── Season ────────────────────────────────────────────────────────────────────
# The season is the 10-week meta-game: portfolios and equity history persist
# across sessions (see exchange/persistence.py) and rank is risk-adjusted
# (exchange/scoring.py). All of it is inert until a week scenario is loaded
# via SCENARIO_PATH or GAME_WEEK, so local play is unaffected.
#
# How often (in ticks) to append an equity snapshot per team while a session
# is open. Snapshots are the input to season scoring, so this sets the
# resolution of the vol and drawdown numbers.
SEASON_SNAPSHOT_TICKS = int(os.environ.get("SEASON_SNAPSHOT_TICKS", "60"))

# Whether the exchange reads and writes the season file at all.
#   "auto"  (default) — only when a week is configured (GAME_WEEK /
#           SCENARIO_PATH / set_week). Plain local play therefore stays
#           ephemeral, exactly as it was before the season system: restarting
#           the exchange resets portfolios.
#   "true"  — always persist, even in open play.
#   "false" — never persist.
SEASON_PERSIST = os.environ.get("SEASON_PERSIST", "auto").lower()

# ── Market calendar & shock ramps ─────────────────────────────────────────────
# Events come from the week scenario file (see exchange/calendar.py). They are
# ANNOUNCED this many ticks ahead — timing is public, direction never is.
CALENDAR_ANNOUNCE_LEAD = int(os.environ.get("CALENDAR_ANNOUNCE_LEAD", "120"))
# Real news moves a price over seconds to minutes, with an overshoot that
# fades. Every price event (calendar or teacher-injected) walks to
# SHOCK_OVERSHOOT × move over SHOCK_RAMP_TICKS, then settles to the move over
# half as many ticks again. Set SHOCK_RAMP_TICKS=1 for the old instant step.
SHOCK_RAMP_TICKS = int(os.environ.get("SHOCK_RAMP_TICKS", "15"))
SHOCK_OVERSHOOT  = float(os.environ.get("SHOCK_OVERSHOOT", "1.3"))

# ── Interest on idle cash ─────────────────────────────────────────────────────
# Positive balances earn this per tick in the carry pass — the hurdle rate a
# strategy has to beat before "just hold cash" is the wrong answer. Kept well
# below MARGIN_RATE_PER_TICK so borrowing is never free money.
# 0.000002/tick ≈ 0.7% over a 3600-tick session.
CASH_INTEREST_PER_TICK = float(os.environ.get("CASH_INTEREST_PER_TICK", "0.000002"))

# ── Message quotas (Level 5 / week 7) ─────────────────────────────────────────
# Per-team token bucket on order and cancel messages, refilled every tick.
# The default is deliberately generous — a two-sided quote across ten symbols
# is 20 messages — so quotas do not bite until a week scenario tightens
# `order_quota`. 0 disables metering entirely.
ORDER_QUOTA_PER_TICK = float(os.environ.get("ORDER_QUOTA_PER_TICK", "20"))
# Burst allowance = quota × this. Market makers legitimately fire one order
# per symbol per requote, so the burst has to cover a full refresh.
ORDER_QUOTA_BURST_MULTIPLE = float(os.environ.get("ORDER_QUOTA_BURST_MULTIPLE", "3"))

# ── Cancellation fees (week 7) ────────────────────────────────────────────────
# Charged per cancelled order, and optionally per resting share pulled, when
# the week's `cancellation_fees` flag is on. Teaches quote-lifetime
# management: cancel/replace every tick stops being free.
CANCEL_FEE_PER_ORDER = float(os.environ.get("CANCEL_FEE_PER_ORDER", "0.05"))
CANCEL_FEE_PER_SHARE = float(os.environ.get("CANCEL_FEE_PER_SHARE", "0.0"))

# ── Latency tiers (week 7) ────────────────────────────────────────────────────
# When the week's `latency_enabled` flag is on, the exchange delays each
# team's OUTBOUND messages by its tier. Observers and the teacher are never
# delayed. Colocation (shop upgrade) cuts the delay to LATENCY_MS_COLOCATED.
LATENCY_MS_DEFAULT   = float(os.environ.get("LATENCY_MS_DEFAULT", "200"))
LATENCY_MS_COLOCATED = float(os.environ.get("LATENCY_MS_COLOCATED", "20"))

# ── Index futures (week 9) ────────────────────────────────────────────────────
# Symbols the exchange settles as CASH-SETTLED FUTURES rather than as shares:
#   * a fill posts margin instead of paying the notional
#   * open positions are marked to the index and settled in cash every
#     FUTURES_SETTLE_TICKS ticks (the "daily" variation margin call)
#   * no starting-share grant, no dividends, no stock-borrow fee on shorts
# Tradeable only when the week's `futures_enabled` flag is on.
FUTURES: set[str] = {
    s.strip() for s in os.environ.get("FUTURES", "ARENA10").split(",") if s.strip()
}
# Initial margin per contract, each side. Symmetric: a short contract carries
# the same requirement as a long one.
FUTURES_MARGIN_PER_CONTRACT = float(
    os.environ.get("FUTURES_MARGIN_PER_CONTRACT", "40"))
# Ticks between variation-margin settlements ("daily" mark).
FUTURES_SETTLE_TICKS = int(os.environ.get("FUTURES_SETTLE_TICKS", "300"))


# How much a future's OWN book influences its mark. Zero by default: a
# cash-settled future is marked against its index, so its book may trade at a
# basis (an arbitrage for students to find) without moving the settlement
# price. Contrast MID_BLEND_WEIGHT above, which is high for cash equities
# because those genuinely have no reference outside the market.
FUTURES_MID_BLEND_WEIGHT = float(os.environ.get("FUTURES_MID_BLEND_WEIGHT", "0.0"))


def is_future(symbol: str) -> bool:
    """True if this symbol settles as a cash-settled future."""
    return symbol in FUTURES


# ── Starting inventory ────────────────────────────────────────────────────────
# Each connected participant receives this many shares of every listed symbol
# when the teacher fires SESSION_OPEN. Set to 0 to start with cash-only.
STARTING_SHARES_PER_SYMBOL = int(os.environ.get("STARTING_SHARES", "20"))

# ── Maker/Taker fee model (Level 3) ───────────────────────────────────────────
# The passive (resting) side of a trade MAKES liquidity and earns a rebate.
# The aggressive (crossing) side TAKES liquidity and pays the fee.
# The exchange keeps the difference. This is how real equity venues reward
# market makers: brokers quoting two-sided markets earn rebate income on
# every fill, replenishing the cash they spend holding inventory.
# Disable with MAKER_TAKER=false to fall back to the flat 50/50 fee split.
MAKER_TAKER_ENABLED = os.environ.get("MAKER_TAKER", "true").lower() in ("true", "1", "yes")
TAKER_FEE_RATE    = float(os.environ.get("TAKER_FEE_RATE", "0.0015"))   # 0.15% of notional
MAKER_REBATE_RATE = float(os.environ.get("MAKER_REBATE_RATE", "0.0010"))  # 0.10% of notional
# TODO Level 5: MAX_ORDERS_PER_MIN_PER_TEAM


# ── Per-venue fee schedules (the exchange as a competitor) ────────────────────
# An exchange-owning team picks its OWN taker fee and maker rebate — that is
# how real venues compete — within teacher-set bounds that make the choice a
# trade-off rather than a way to break the game:
#
#   * taker in [TAKER_MIN_BPS, TAKER_MAX_BPS]: below the floor a venue cannot
#     fund its own rebate; above the ceiling the flow simply goes elsewhere.
#   * rebate in [0, taker - VENUE_NET_MIN_BPS]: the venue always nets at
#     least VENUE_NET_MIN_BPS per matched trade, so it can never rebate itself
#     into bankruptcy.
#
# The schedule lives in the roster under the team's "fees" key:
#     "fees": {"taker": 0.0015, "rebate": 0.0010}
# and is matched to a running exchange by its `exchange_port`.
TAKER_MIN_BPS      = 5.0
TAKER_MAX_BPS      = 30.0
REBATE_MIN_BPS     = 0.0
VENUE_NET_MIN_BPS  = 2.0
DEFAULT_TAKER_BPS  = 15.0
DEFAULT_REBATE_BPS = 10.0
# How often the resolver may re-read the roster. Cheap enough to do on demand
# (settlement asks for the rate on every fill) while keeping a live teacher or
# portal edit visible within half a minute, with no restart.
VENUE_FEE_REFRESH_SEC = float(os.environ.get("VENUE_FEE_REFRESH_SEC", "30"))

# Explicit env vars always win over the roster: a teacher who pins a rate on
# the command line means it. Captured once, at import.
_TAKER_FEE_FROM_ENV    = "TAKER_FEE_RATE" in os.environ
_MAKER_REBATE_FROM_ENV = "MAKER_REBATE_RATE" in os.environ

_last_fee_refresh = 0.0


def validate_fee_bps(taker_bps: float, rebate_bps: float) -> tuple[float, float]:
    """Check a proposed schedule against the teacher bounds.

    Returns (taker_bps, rebate_bps) as floats, or raises ValueError with a
    message that is safe to show a student.
    """
    try:
        taker = float(taker_bps)
        rebate = float(rebate_bps)
    except (TypeError, ValueError):
        raise ValueError("Fees must be numbers, in basis points") from None
    if not TAKER_MIN_BPS <= taker <= TAKER_MAX_BPS:
        raise ValueError(
            f"Taker fee must be between {TAKER_MIN_BPS:g} and "
            f"{TAKER_MAX_BPS:g} bps (you asked for {taker:g})")
    if rebate < REBATE_MIN_BPS:
        raise ValueError("Maker rebate cannot be negative")
    if rebate > taker - VENUE_NET_MIN_BPS:
        raise ValueError(
            f"Maker rebate must leave the venue at least "
            f"{VENUE_NET_MIN_BPS:g} bps per trade — with a {taker:g} bps "
            f"taker fee the most you can rebate is "
            f"{taker - VENUE_NET_MIN_BPS:g} bps")
    return taker, rebate


def venue_fees_for(port: int | None = None) -> dict | None:
    """The roster fee schedule of the exchange running on `port`.

    Returns {"taker": rate, "rebate": rate} as fractions of notional, or None
    when no roster team owns that port or its entry has no valid schedule.
    An out-of-bounds roster entry is ignored rather than honoured — the
    exchange never charges a schedule the teacher's bounds forbid.
    """
    target = PORT if port is None else int(port)
    for cfg in _read_roster().values():
        own = cfg.get("exchange_port")
        if not own:
            continue
        try:
            if int(own) != target:
                continue
        except (TypeError, ValueError):
            continue
        fees = cfg.get("fees")
        if not isinstance(fees, dict):
            return None
        try:
            taker = float(fees["taker"])
            rebate = float(fees["rebate"])
            validate_fee_bps(taker * 10_000.0, rebate * 10_000.0)
        except (KeyError, TypeError, ValueError):
            return None
        return {"taker": taker, "rebate": rebate}
    return None


def resolve_venue_fees(port: int | None = None) -> dict:
    """The schedule this exchange actually charges: env → roster → default."""
    roster = venue_fees_for(port)
    taker = TAKER_FEE_RATE if _TAKER_FEE_FROM_ENV else (
        roster["taker"] if roster else DEFAULT_TAKER_BPS / 10_000.0)
    rebate = MAKER_REBATE_RATE if _MAKER_REBATE_FROM_ENV else (
        roster["rebate"] if roster else DEFAULT_REBATE_BPS / 10_000.0)
    taker_src = "env" if _TAKER_FEE_FROM_ENV else ("roster" if roster else "default")
    rebate_src = "env" if _MAKER_REBATE_FROM_ENV else ("roster" if roster else "default")
    return {"taker": taker, "rebate": rebate,
            "source": taker_src if taker_src == rebate_src
                      else f"taker:{taker_src} rebate:{rebate_src}"}


def refresh_venue_fees(force: bool = False) -> dict:
    """Re-resolve TAKER_FEE_RATE / MAKER_REBATE_RATE from the roster.

    Called on demand by `config_for_team()` (i.e. on every settled trade) but
    throttled to VENUE_FEE_REFRESH_SEC, so an exchange-owning team's live fee
    change from the portal takes effect within half a minute with no restart,
    at the cost of one file read per half minute.

    Only a valid roster schedule ever writes the globals: with no "fees" entry
    the module defaults (and anything a test or scenario has set) are left
    exactly as they are.
    """
    global TAKER_FEE_RATE, MAKER_REBATE_RATE, _last_fee_refresh
    import time as _time
    now = _time.monotonic()
    if not force and now - _last_fee_refresh < VENUE_FEE_REFRESH_SEC:
        return {"taker": TAKER_FEE_RATE, "rebate": MAKER_REBATE_RATE}
    _last_fee_refresh = now
    roster = venue_fees_for()
    if roster is None:
        return {"taker": TAKER_FEE_RATE, "rebate": MAKER_REBATE_RATE}
    if not _TAKER_FEE_FROM_ENV:
        TAKER_FEE_RATE = roster["taker"]
    if not _MAKER_REBATE_FROM_ENV:
        MAKER_REBATE_RATE = roster["rebate"]
    return {"taker": TAKER_FEE_RATE, "rebate": MAKER_REBATE_RATE}
