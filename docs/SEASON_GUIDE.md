# AlgoArena — Season Guide

How the 10-week season works: persistent state, week gates, scoring, and the
scenario file format.

Read this alongside [TEACHER_GUIDE.md](TEACHER_GUIDE.md) (how to run a
session) and [ROADMAP.md](ROADMAP.md) (what is built).

---

## 1. What a season is

A season is ten weekly sessions that share one continuous book. Portfolios,
cash, positions, and cumulative P&L persist from week to week — a team that
closes week 3 short 200 NVDA opens week 4 short 200 NVDA, carrying the
borrow cost. Nothing resets until the teacher runs **NEW SEASON**.

Two things make the season a game rather than ten disconnected exercises:

- **Week gates.** Each week unlocks mechanics (shorts, post-only, circuit
  breakers, latency, the index future). Students cannot use a mechanic before
  its week; the exchange rejects the order with a clear reason.
- **Risk-adjusted rank.** The season is *not* won by the biggest final net
  worth. It is won by the best return-per-unit-risk, penalised for drawdown
  (§4). This is announced up front so it shapes behaviour from week 2.

Without any season configuration the exchange runs `OPEN PLAY`: every gate
permissive, nothing persisted beyond the default checkpoint, exactly the
behaviour AlgoArena had before the season system. Local play needs no setup.

## 2. Running a season

```bash
# Week 3 of the season, hosted
GAME_WEEK=3 make exchange

# or point straight at a scenario file
SCENARIO_PATH=teacher/season/week03.json make exchange
```

The teacher can also switch week live from the dashboard **SEASON** tab
(`SET WEEK` picker) or with the `set_week` teacher command.

### Session controls

| Action | Where | Effect |
|--------|-------|--------|
| ▶ START | header | `SESSION_OPEN` — bots activate, share grant, first equity snapshot |
| ⏻ RESET | header | `close_session` — flatten and lock scores (also checkpoints) |
| `end_session` | teacher command | Close **and** explicitly bank the week to the season file |
| ★ NEW SEASON | header (confirm dialog) | Wipes every portfolio, all P&L, all scores. Irreversible. |
| SET WEEK | SEASON tab | Switch the live rule set mid-session |

### Where state lives

| Path | Contents |
|------|----------|
| `data/season.json` | The season: portfolios, tick, exchange revenue, equity history, sessions played. Gitignored — it holds live student capital. |
| `teacher/season/weekNN.json` | The ten scenario files (source-controlled) |
| `sessions/session_*.jsonl` | Per-session recordings for replay and TCA |

The season file is written on session close and every
`SEASON_SAVE_INTERVAL_SEC` (default 60 s) while a session is open, using an
atomic replace — a crash costs at most a minute, never the season.

**Persistence follows the season.** By default (`SEASON_PERSIST=auto`) the
exchange only reads and writes `data/season.json` once a week is configured —
via `GAME_WEEK`, `SCENARIO_PATH`, or a live `set_week`. Plain local play
(`make exchange` with no env vars) therefore stays ephemeral exactly as it was
before the season system: restarting the exchange resets portfolios. Force it
either way with `SEASON_PERSIST=true` / `SEASON_PERSIST=false`.

## 3. Scenario file format

`teacher/season/weekNN.json`:

```json
{
  "week": 4,
  "label": "Market making — post-only unlocked, purchase window 1",
  "flags": {
    "shorts_allowed": true,
    "post_only_allowed": true,
    "circuit_breakers": false,
    "cancellation_fees": false,
    "latency_enabled": false,
    "multi_venue": true,
    "futures_enabled": false,
    "purchase_window": true
  },
  "position_limit": 800,
  "scoring_counts": true,
  "events": [
    {"kind": "earnings",   "symbol": "AMZN", "tick": 350,
     "magnitude_range": [0.03, 0.08]},
    {"kind": "econ_print", "tick": 1150, "market_wide": true,
     "magnitude_range": [0.01, 0.025]},
    {"kind": "dividend",   "symbol": "AAPL", "tick": 600,
     "amount_per_share": 0.25}
  ]
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `week` | int | Week number (0 = open play) |
| `label` | str | Shown in the dashboard header and logs |
| `flags` | object | Gates, below. Absent keys default to permissive. |
| `position_limit` | int | Per-symbol position cap; overrides `POSITION_LIMIT_SHARES`. Omit to leave config alone. |
| `order_quota` | int | Order/cancel messages per tick; overrides `ORDER_QUOTA_PER_TICK`. Omit to leave the generous default (which never bites). |
| `scoring_counts` | bool | `false` on paper weeks — no equity snapshots are taken, so the week cannot affect season rank |
| `config` | object | Whitelisted market-structure overrides: `OPENING_AUCTION_TICKS`, `CLOSING_AUCTION`, `CLOSING_AUCTION_TICKS`, `SSR_TRIGGER_PCT`, `SHORT_LOCATE_CAP`, `LULD_BAND_PCT`. This is how auctions phase in (opening from week 4, closing from week 6) and how the borrow pool tightens across the season (2000 → 1200 → 1000 → 800 → 600 shares). |
| `events` | array | Market calendar (§5) |

### Gate flags

| Flag | When false | Enforced at |
|------|-----------|-------------|
| `shorts_allowed` | A sell may only reduce an existing long → `SHORTS_LOCKED` | order entry |
| `post_only_allowed` | Post-only orders rejected → `ORDER_TYPE_LOCKED` | order entry |
| `circuit_breakers` | Volatility halts disabled | sets `CIRCUIT_BREAKERS_ENABLED` |
| `cancellation_fees` | No fee for cancelling a resting order | cancel path |
| `latency_enabled` | All participants see messages immediately | outbound send |
| `multi_venue` | (informational) single-venue week | — |
| `futures_enabled` | ARENA-10 not tradeable | order entry |
| `purchase_window` | The upgrade shop is closed | `/api/upgrade` |

Flags only ever *restrict* relative to open play, so a scenario can never
enable something the engine does not otherwise support.

### The ten weeks

| Week | Label | Unlocks |
|------|-------|---------|
| 1 | Paper trading — nothing counts | long-only, limit/market only, limit 200, **not scored** |
| 2 | Season starts | short selling, scoring begins, limit 400 |
| 3 | Signals & earnings season | earnings calendar, dividends, limit 600 |
| 4 | Market making | post-only, **purchase window 1**, limit 800 |
| 5 | Risk | circuit breakers + liquidation drama |
| 6 | Midterm tournament | limit 1000, dense event calendar |
| 7 | Execution | rate quotas, cancellation fees, latency tiers, **purchase window 2** |
| 8 | Multi-venue | routing and fee competition across team exchanges (see §8b) |
| 9 | ARENA-10 index future | cash-settled index future, broker hedging |
| 10 | Finale | everything live |

Position limits widen monotonically across the season, so a strategy tuned
in an early week keeps working as size grows.

## 4. Season scoring — the exact formula

The exchange appends an **equity snapshot** per participant every
`SEASON_SNAPSHOT_TICKS` ticks (default 60) while a session is open, but
**only in weeks where `scoring_counts` is true**. Snapshots use conservative
marks (longs at the bid, shorts at the ask), so an unsellable position is not
scored as if it were cash.

Given a team's snapshots `e[0..n]` (oldest first, across the whole season):

```
r[i]              = e[i] / e[i-1] - 1                    for i = 1..n
cumulative_return = e[n] / e[0] - 1
mean_return       = mean(r)
vol               = population standard deviation of r
peak[i]           = max(e[0..i])
max_drawdown      = max over i of (peak[i] - e[i]) / peak[i]

risk_adjusted     = (mean_return / vol) * max(0, 1 - PENALTY * max_drawdown)
```

- `PENALTY` = `SEASON_DRAWDOWN_PENALTY`, default **2.0**: a 25% max drawdown
  halves your score, a 50% drawdown zeroes it.
- The multiplier is floored at zero so a catastrophic drawdown cannot flip a
  negative Sharpe into a positive score.
- `vol == 0` scores **0**. You cannot win the season by never trading — the
  do-nothing control is the benchmark to beat, not a winning strategy.
- Fewer than 3 snapshots scores 0 (not enough data to be meaningful).

Season rank is `risk_adjusted`, descending. Two teams with the same total
return are separated by how smoothly they earned it.

### Reading the SEASON tab

- **Cumulative equity** — one curve per bot, each normalised to 100 at its
  first snapshot so books of different sizes are comparable. The dashed line
  at 100 is break-even.
- **Risk-adjusted standings** — rank, net worth, cumulative return, vol, max
  drawdown, and the score above.
- The header shows the current week; a paper week is badged
  `PAPER WEEK — DOES NOT COUNT`.

## 5. The upgrade shop and the student portal

### The shop

During a **purchase window** (weeks 4 and 7) a team can convert capital into
a permanent edge. Prices are constants in `exchange/upgrades.py`, set at
3–10% of the ~$650k a typical team allocates to bot cash. Repriced in Phase
10 (see *Why the prices changed* below) — the launch prices were a trap.

| Upgrade | Price | Effect |
|---------|-------|--------|
| Volume fee tier | $40,000 | taker 0.15% → 0.12%, maker rebate 0.10% → 0.12% |
| Prime brokerage terms | $40,000 | margin haircut 0.50 → 0.65, cheaper carry |
| Colocation | $50,000 | outbound latency 200ms → 20ms |
| Position limit increase | $30,000 | per-symbol cap → 2,000 shares |
| Message quota increase | $30,000 | order/cancel rate limit doubled |
| Priority calendar feed | $35,000 | calendar alerts at 2× the normal announcement lead |
| Margin-call insurance | $45,000 | first forced liquidation waived, once per season |
| Execution analytics | $25,000 | live execution-quality panel in the portal |

#### Why the prices changed

A 42-season ROI study found the launch prices (15–25% of bot cash) could not
pay back over the eight sessions after the first purchase window. Measured:

- **`fee_tier` is the only consistently valuable upgrade**, at about
  **+$5.6k per session** for the highest-churn desk in the field — and most of
  that comes through the *maker rebate*, not the taker cut. Its value scales
  with your own volume, so it is worth roughly `your volume × 2 bps`. A quiet
  desk never gets it back, which is exactly the calculation students should do.
- **The others never bind at classroom scale.** Nobody hits a 1,000-share cap,
  a message quota, or a maintenance call often enough to justify six figures.
  They are now priced as **optionality**: cheap enough to be a genuine choice,
  never so cheap that buying the whole shop is obviously correct (the full
  catalog still costs more than a trader seat).

#### The three Phase 10 enhancements

- **Priority calendar feed** — the exchange announces an imminent event to
  buyers at `2×` the normal lead (`CALENDAR_ANNOUNCE_LEAD`), as a per-client
  send; the normal wave still reaches everyone at the usual time.
  It sells **delivery speed, not information**: the week's schedule is public
  to everybody through `/api/calendar`, and the direction is never announced
  to anyone. Enforced in `exchange/calendar.due_early_announcements` +
  `ExchangeServer._announce_calendar(only_upgrade=...)`.
- **Margin-call insurance** — the first time any of the team's books would be
  force-liquidated, the liquidation is waived, the position is left completely
  untouched, and the team gets `RISK_SHIELD_GRACE_TICKS` (default 120, ~1
  minute) to fix it themselves. Then the policy is spent: the roster records
  `"risk_shield": "used"`, a `RISK_SHIELD` event is broadcast, and the second
  margin call is real. Held per **team**, so the first book to breach spends it
  for all of them. The grace window is what makes the waiver mean anything —
  without it the next maintenance check, half a second later, would find the
  same book with the policy already gone.
- **Execution analytics** — a live TCA panel in MY TEAM: per-symbol slippage
  against the venue mid *at the moment of each fill* over the team's last 200
  fills, its maker ratio, and fees net of rebates. Positive slippage is a cost.
  Non-owners see the panel greyed out with the price, because the shop should
  sell what it shows. Computed in `teacher/portal.build_analytics` from the
  dashboard relay's tape, which stamps the mid on every trade as it arrives.

Mechanics:

- Ownership lives in the roster under `"upgrades": {"fee_tier": true}`, so a
  purchase survives an exchange restart.
- The cost is debited **pro-rata across the team's bots' cash** held by the
  exchange. Bots with a negative balance do not contribute — you cannot fund
  a purchase from a margin loan.
- The proceeds are credited to the **hosting exchange team's** revenue, which
  is where colocation and market-data money goes in reality. It shows up in
  `exchange_fees_by_team` on the leaderboard.
- The exchange resolves every upgraded tunable per check through
  `config.config_for_team()`, so a purchase takes effect on the very next
  order. Upgrades only ever move a value in the team's favour.

### How a purchase flows

```
browser (portal)  --POST /api/upgrade {token, upgrade}-->  dashboard :8888
dashboard  verifies the team token, catalog key, ownership, purchase window
dashboard  --UpgradeRequest over the teacher relay-->      exchange
exchange   re-validates window / ownership / cash, debits pro-rata,
           credits venue revenue, writes the roster, replies UPGRADE_RESULT
dashboard  returns the exchange's verdict to the browser
```

The exchange performs the debit because it is the only component that can see
live cash, and it is the only writer of the purchase — so a request that
arrives twice cannot double-charge. In a multi-venue week the dashboard sends
the request to one exchange only; the others pick the upgrade up when they
next re-read the roster.

### The portal

`http://<arena-host>:8888/team` — one page per team, gated by the team token
(kept in the browser's localStorage, never on the server).

| Endpoint | Auth | Returns |
|----------|------|---------|
| `GET /team` | — | the portal page |
| `GET /api/me?token=…` | team token | members, per-bot portfolio and P&L, season history, owned upgrades, the shop, the calendar, report links |
| `GET /api/calendar` | public | upcoming events, **direction excluded** |
| `GET /api/whoami?token=…` | any token | `{role: teacher}` / `{role: team, …}` / 401 |
| `POST /api/upgrade` | team token | buy from the shop |
| `POST /api/seat` | team token | hire a trader/broker/exchange seat |
| `POST /api/venue` | team token | set your own venue's fee schedule |

The portal shows a team its own book in the detail the class dashboard
deliberately withholds: per-bot cash, positions, fees versus rebates, season
sparkline, and what it owns. The shop buttons are disabled outside a purchase
window and name the next one.

### One dashboard, three views (Phase 10)

The class dashboard at `:8888/` has a **SIGN IN** control in its header, and
`GET /api/whoami` decides what it renders. Students see the class dashboard,
not a lesser version of it:

| Signed in as | Sees |
|--------------|------|
| teacher token | every tab, session control (START / RESET / LIFT HALTS / NEW SEASON / SET WEEK), the shock panel |
| team token | the **same** MARKET / FLOW / STATS / SEASON tabs — market data is common knowledge — with every teacher control hidden, a team badge in the header, and a fifth **MY TEAM** tab carrying that team's portal |
| nobody, hosted | read-only market tabs until they sign in |
| nobody, plain local play | the teacher view, unchanged |

MY TEAM is `/team?embedded=1` in a same-origin iframe, so there is one
implementation of the shop, MY VENUE, and the season history rather than two.
`/team` still works standalone and students can bookmark it.

**Session control is gated server-side.** The browser's WebSocket carries the
token with every command and the relay verifies `auth.verify_teacher()` before
forwarding anything to an exchange — hiding a button is a courtesy, not a
control. The one exception is deliberate: when **no teacher token has ever been
issued** (`python -m shared.auth teacher` never run) there is no secret to
check against, so commands are allowed exactly as before and plain local play
needs no setup. Issue a teacher token the moment the class is hosted.

### Grow the firm: hiring mid-season

Registration buys a team its opening line-up out of a fixed $1M budget. During
a purchase window a team can also **reinvest its winnings** from MY TEAM:

| Seat | Cost | Cap per team |
|------|------|--------------|
| Trader seat | the capital you allocate, from $50,000 | 5 |
| Broker desk | the capital you allocate, from $100,000 | 2 |
| Exchange licence | $300,000 | 1 |

For a trader or broker the money **moves rather than being spent**: it is
debited pro-rata from the team's existing bots (a bot running a margin loan
contributes nothing — you cannot fund a desk by borrowing more) and written
into the roster as the new bot's capital, which `config.starting_cash_for()`
hands over the first time that bot connects. A team's combined net worth is
therefore unchanged at the moment of purchase; what changes is that a new
process can now put it at risk.

An exchange licence is the exception and a genuine outflow: it buys the right
to charge other people's flow, not a book, and it is **not** credited to the
venue that happened to process the purchase. The new venue gets the next free
port and the default 15/10 bps schedule, and the dashboard picks it up within
`VENUE_SCAN_SEC` without a restart.

The response carries the new bot id and the exact shell line to run it, which
MY TEAM shows immediately. `SEAT_PURCHASED` is broadcast so the dashboards and
logs see the hire.

A mid-season entrant cannot game the season rank: fewer than three equity
snapshots scores 0 (`exchange/scoring.MIN_SNAPSHOTS`).

```
browser (MY TEAM) --POST /api/seat {token, kind, capital}--> dashboard :8888
dashboard  verifies the team token, kind, minimum, roster cap, window, cash
dashboard  --SeatRequest over the teacher relay-->           exchange
exchange   re-validates, writes the roster, debits pro-rata, replies
           SEAT_RESULT with the bot id and the run command
```

### Balance-testing an upgrade price

```bash
PYTHONPATH=. python scripts/season_sim.py --compare-upgrade fee_tier \
    --upgrade-target mm_aggressive --ticks 5000 --seed 11
```

Runs the same seeded season twice — with and without the upgrade granted to
one bot — and prints the net-worth delta against the price, both for one
session and extrapolated over the remaining sessions (an upgrade is
permanent, so one week understates it).

Two caveats when reading the output: variance across seeds is large, so use
several; and the simulated bots cap their own size, so a position-limit
upgrade may not bind on them even though it would bind on a student bot.

## 5b. The shop as strategy — what buys an edge, when

Purchase windows open in **weeks 4 and 7**. The scenarios are designed so
each upgrade has weeks where it earns its price — buying the right thing at
the right window IS part of the game:

| Upgrade | $ | Buy at window 1 (wk 4) if… | Buy at window 2 (wk 7) if… |
|---|---|---|---|
| `fee_tier` | 40k | you run a busy desk — pays ~2 bps × your own volume, mostly via the rebate. The one upgrade that's almost always right for high-churn teams. | still the best pure-ROI buy if you skipped it |
| `locate_desk` | 35k | — (borrow is plentiful, 2000/symbol) | **the window-2 sleeper**: the pool tightens to 1200 → 1000 → 800 → 600 through weeks 7–10. When everyone wants the same short into earnings, you're the only one who can get it. |
| `risk_shield` | 45k | week 5 raises maintenance margin to 0.4 and liquidations go live — insurance before the storm | cheaper to skip if you survived wk 5–6 comfortably |
| `calendar_feed` | 35k | every week has scheduled events; earlier warning compounds | same |
| `order_quota` | 30k | — (no quotas yet) | quotas (8/tick) start in wk 7 — buy only if you see `RATE_LIMITED` rejects |
| `colocation` | 50k | — (no latency yet) | latency tiers start wk 7; matters most for auction-close racing and NBBO arbitrage in wk 8–10 |
| `margin_plus` | 40k | brokers carrying heavy inventory into volatile weeks 5–6 | broker desks quoting multiple venues |
| `position_limit` | 30k | only if you're hitting the cap (check DESK EVENTS) | wk 9–10 futures + equities can crowd the 1000 cap |
| `analytics_pro` | 25k | TCA on your own fills — cheap, pays in better execution homework | same |

Rule of thumb the scenarios enforce: **window 1 buys production (fee_tier,
calendar_feed, risk_shield), window 2 buys scarcity (locate_desk, quota,
colocation)** — because weeks 7–10 are where quotas, latency, and the borrow
squeeze actually bind.

## 5c. IPOs — the primary market

Weeks 3 and 8 bring a new listing (`"kind": "ipo"` in the scenario file):

```json
{"kind": "ipo", "symbol": "ORCA", "name": "Orca Analytics",
 "offer_range": [24.0, 28.0], "shares": 4000,
 "window": [150, 450], "tick": 550}
```

Lifecycle, broadcast as `IPO_ANNOUNCE → IPO_OPEN → IPO_PRICED → IPO_LISTED`:

1. **Announce** (session open): range, size, and the book window.
2. **Bookbuild**: bots submit one indication each — `(quantity,
   max_price)` — via the SDK hook `Trader.on_ipo()` (return `qty` for the
   top of the range or `(qty, max_price)`), or by hand from MY TEAM's IPO
   DESK. Resubmitting replaces.
3. **Pricing**: the offer prices at the highest level that fills the book
   (undersubscribed → bottom of the range). Oversubscribed → pro-rata
   allocation. Cash is debited only for allocated shares; proceeds go to
   the issuer (a real cash sink, ledgered by the EOD reconciliation).
4. **Listing**: the stock starts trading AT the offer price while its
   hidden true value sits at `offer × exp(N(+12%, 18%))` — drawn
   deterministically per symbol, so every venue agrees. The tick-capped
   engine walks the price toward the truth over the next minutes: the
   average deal pops, roughly a quarter break issue, and the first hour of
   trading is the discovery.

What it teaches: underpricing ("leaving money on the table"), the winner's
curse (a hot book prices at the top — exactly when the pop is smallest),
oversubscription games (bid more than you want, get cut back pro-rata —
and pay for it when the book is cold), and listing-day momentum.

## 6. Market calendar

Calendar events come from the scenario `events` array. See
[TEACHER_GUIDE.md](TEACHER_GUIDE.md) for operating notes.

| `kind` | Fields | Effect |
|--------|--------|--------|
| `earnings` | `symbol`, `tick`, `magnitude_range` | Single-symbol shock; direction randomised **at fire time** |
| `econ_print` | `tick`, `magnitude_range`, `market_wide` | Market-wide shock |
| `dividend` | `symbol`, `tick`, `amount_per_share` | Cash to holders; shorts **pay** it |

Events are **announced in advance and without direction**, so predicting them
is a strategy rather than luck: you know an earnings print lands at tick 350
and roughly how big it will be, not which way it goes. Straddling, sizing
down into the event, and trading the ramp afterwards are all legitimate
plays. Announcements arrive as a `SessionEvent` with `event="CALENDAR"` (at
session open, and again `CALENDAR_ANNOUNCE_LEAD` ticks before each event), and
bots can also poll `GET /api/calendar`. The direction is drawn only when the
event fires, and is then published as `CALENDAR_EVENT`.

### Shock ramps

No price event lands in a single tick. Every price move — calendar event or
teacher-injected shock — is applied as a ramp:

```
fair value walks to  SHOCK_OVERSHOOT × move   over SHOCK_RAMP_TICKS  (15)
then settles back to             1.0 × move   over half as many again (7)
```

So a +6% earnings beat rises to +7.8% over 15 ticks and then eases back to
+6%. Momentum strategies have a real move to catch; mean-reversion
strategies have the overshoot to fade. Set `SHOCK_RAMP_TICKS=1` for the old
instant step.

Two consequences worth telling students:

- **Exiting on the tick the news breaks captures almost none of the move.**
  The simulator's shock predictor holds through the ramp for exactly this
  reason.
- While a ramp runs it owns that symbol's fair value, so it is not averaged
  away by the usual book/fundamental blend, and it is exempt from
  `MAX_TICK_MOVE` — news is *supposed* to move a price several percent in
  seconds. The fundamental is relevelled with it, so the new level persists
  once the ramp ends.

### How a price is formed

Equity market structure, mapped onto the game. Three forces, and no injected
randomness anywhere else:

| Force | Where | What it does |
|-------|-------|--------------|
| **Fundamental (news)** | `plugins/securities/defaults.py` | ONE path per symbol. Each tick's innovation is drawn from `random.Random(f"{symbol}:{tick}:{FUNDAMENTAL_SEED}")`, so *every venue computes the identical path with no networking*. ~0.02% per tick — visible drift over minutes, never a jump. |
| **Discovery (supply/demand)** | `exchange/server.py` | The venue's own book, read as a depth-weighted **microprice** `(bid×ask_size + ask×bid_size)/(bid_size+ask_size)`. Size on one side pulls the price toward the other: that is demand pressure. |
| **Impact (flow)** | `exchange/price_engine.py` | Every fill pushes the price in the aggressor's direction, √notional, 30% permanent / 70% decaying. This is the main intraday driver, as in a real market. |

Fair value each tick is `MID_BLEND_WEIGHT` (0.85) × microprice + 0.15 ×
fundamental, and the move is capped at `MAX_TICK_MOVE` (0.2%) — except for
ramps, which own the fair value outright. Because the fundamental is *shared*,
that 0.15 is a force pulling venues **together**; each venue's own book is
what lets them differ, and only within a spread or two.

Two rules that keep it honest:

- **A price never teleports.** If a book is one-sided, empty or crossed this
  tick (which happens on every market-maker requote), the venue keeps its
  previous reference and lets it drift `DEAD_BOOK_DECAY` of the way home to
  the fundamental. It does *not* fall back to the last trade, which could be
  minutes old and a long way away.
- **A market maker does not invent prices.** `broker/broker.py` quotes around
  the venue's published mark (`BookSnapshot.ref_price`), pulled toward the
  Yahoo reference with a 45s half-life. It has no random walk of its own; it
  requotes when the centre moves `REQUOTE_THRESHOLD_BPS` (5 bps), when a side
  is filled, or when the quote goes stale.

Rotate `FUNDAMENTAL_SEED` per session if you want a fresh path (all venues in
one session must share the value), and set `FUNDAMENTAL_VOL=0` for a session
where only order flow moves prices — a good demonstration of what impact
alone looks like.

### Dividends

A dividend credits `amount_per_share × position` to longs and **debits the
same from shorts** — a borrowed share still owes its dividend. The price is
also marked down by the dividend amount ex-dividend, so buying the tick
before is not free money. Net worth for a holder is therefore roughly
unchanged: cash in, mark-to-market out. That equivalence is the lesson.

### Interest on idle cash

Positive cash balances earn `CASH_INTEREST_PER_TICK` (default 0.000002, about
0.7% over a 3600-tick session) in the same pass that charges margin interest
and borrow fees. It is the hurdle rate: a strategy has to beat *holding
cash*, not zero. Borrowers pay carry and earn no interest.

## 7. Microstructure (weeks 7+)

Three mechanics turn on in week 7 and stay on. All three are inert until a
scenario enables them, so earlier weeks are unaffected.

### Message quotas

Each bot gets `order_quota` order/cancel messages per tick, with a burst
allowance of `ORDER_QUOTA_BURST_MULTIPLE ×` that (default 3×). Over quota the
exchange answers `RATE_LIMITED` and the order is refused. Cancels count too —
cancel/replace spam is precisely what is being metered.

The default config quota (20/tick) never bites; weeks 7–10 set `order_quota`
to 8–10, which is below the 20 messages a two-sided refresh across ten
symbols costs. That is the point: brokers must choose which symbols to quote,
widen instead of requoting, or buy the quota upgrade.

A refused message consumes nothing, and a cancel for an order that does not
exist is not charged — quota is spent on real work.

### Cancellation fees

With `cancellation_fees` on, each cancel costs
`CANCEL_FEE_PER_ORDER + CANCEL_FEE_PER_SHARE × shares pulled` (default $0.05
flat). It lands in the team's `total_fees_paid` and in the venue's revenue, so
it shows up in TCA. Twenty requotes now cost a dollar; a broker requoting ten
symbols every tick for an hour pays real money.

### Latency tiers

With `latency_enabled` on, the exchange holds each team's **outbound**
messages for its tier — `LATENCY_MS_DEFAULT` (200ms), or
`LATENCY_MS_COLOCATED` (20ms) for teams that bought colocation. Delayed sends
are fired as tasks, so a slow tier never holds up matching or anyone else's
data.

**Observers and the teacher are never delayed**, so the dashboard always shows
the truth as it happens.

Note: the headless simulator disables latency, because wall-clock delays are
meaningless with a virtual clock (it would simply sleep). Quotas and
cancellation fees *are* simulated — they are tick-based and change balance.

## 8. ARENA-10 — the index future (week 9)

### Contract specification

| | |
|---|---|
| **Symbol** | `ARENA10` |
| **Underlying** | Equal-weighted average of the ten listed equities (AAPL, MSFT, NVDA, TSLA, AMZN, GOOGL, META, NFLX, AMD, INTC) |
| **Contract size** | 1 contract = 1 index unit (opening level ≈ 290.20) |
| **Settlement** | Cash. No delivery, no expiry. |
| **Initial margin** | `FUTURES_MARGIN_PER_CONTRACT`, default **$40 per contract, each side** (symmetric: a short costs the same as a long) |
| **Variation margin** | Marked to the index and settled in cash every `FUTURES_SETTLE_TICKS` ticks (default 300) |
| **Venue** | The ordinary CLOB — same order types, same maker/taker fees, same quotas |
| **Availability** | Only when the week's `futures_enabled` flag is on (week 9). Before that, orders are rejected with `FUTURES_LOCKED`. |
| **Dividends / grants** | None. It is a contract, not a shareholding. |
| **Stock borrow** | None on shorts — there is no share to locate, only margin. |

### How it differs from a share

This is the part worth walking through in class:

- **Buying does not pay the notional.** A fill moves only fees; the position
  and its entry price are what you acquire. Ten contracts at 290 costs $400 of
  margin, not $2,900.
- **Net worth counts only the unsettled variation** — `(mark − entry) × qty` —
  not `qty × price`. Counting the notional would credit money nobody paid.
- **P&L becomes real cash at each mark.** The variation is paid or collected
  and the entry price resets, so nothing accumulates unsettled. A team that is
  wrong on the index pays for it during the session, not at some distant expiry.
- **It is not collateral.** A long future gives no buying power for stock; only
  owned inventory does.
- **The mark follows the index, not the contract's own book**
  (`FUTURES_MID_BLEND_WEIGHT`, default 0). Cash equities are marked mostly from
  their own book because they have no outside reference; a cash-settled future
  has one by definition. This means the contract's book *can* trade at a basis
  to fair value — which is a genuine arbitrage for students to find, and it
  never contaminates settlement.

### Why it exists: the broker's first real hedge

A market maker accumulates a basket of single names just by doing its job.
Before week 9 the only way to cut that directional exposure was to stop
quoting. Now it can sell the index against the book: keep the spread income,
shed most of the beta, and be left holding the residual (idiosyncratic) risk —
which is what a market maker should actually be paid for.

Measure it:

```bash
PYTHONPATH=. python scripts/season_sim.py --compare-hedge --ticks 1200 --seed 5
```

Runs week 9 twice with the same seed and the same flow, changing only whether
the market maker hedges. A representative result:

| arm | net worth | equity vol | max drawdown |
|-----|-----------|-----------|--------------|
| unhedged | $305,093 | 0.0325% | 2.35% |
| hedged | $306,274 | 0.0293% | 1.75% |

Volatility down ~10%, drawdown down ~25%, net worth essentially unchanged.
Since season rank is risk-adjusted (§4), that is a scoring win even when the
P&L is flat — which is exactly the lesson.

## 8b. Week 8 — multi-venue and fee competition

Week 8 is the week the market becomes fragmented. Every team exchange lists
the same securities, so the same stock has a price on each venue.

**All venues are usable by all participants.** `make bots` launches brokers
with `EXCHANGE_URLS` covering every venue (one shared pricing brain, N
execution gateways) and splits traders round-robin across venues — see
[TEACHER_GUIDE §2.1](TEACHER_GUIDE.md). Traders remain single-venue by
default: routing across venues is OPTION F in `trader/trader.py`, the Level 6
challenge.

### Keeping the venues coherent

Two things keep venues in line, and they are the same two that do it in real
markets: **common information** and **arbitrage**.

The common information is the shared fundamental — every venue computes the
identical news path from `(symbol, tick, FUNDAMENTAL_SEED)`, and 15% of each
venue's fair value is anchored to it every tick (see *How a price is formed*).
That alone holds two venues within a few basis points of each other; measured
live over 180s with three brokers and ten traders, the mean cross-venue gap
was 0.047%. **All venues in one session must run the same
`FUNDAMENTAL_SEED`** — the default is a fixed constant, so this is automatic
unless you set it.

Arbitrage is what closes the rest, and it is the part students can see and
trade, so run the shipped arbitrageur alongside the venues:

```bash
make arb URLS=ws://localhost:8765,ws://localhost:8766
```

It takes both sides whenever a venue's ask plus both taker fees is below
another venue's bid. See the simulator's **Venue coherence** line
(`--venues 2`) for the before/after.

**Sync the opening reference before the season** (`make sync-prices`): stale
base prices force every venue onto a slow clamped migration toward the Yahoo
anchor at venue-specific speeds — measured up to an 11% cross-venue gap when
INTC's base was 4x below the real price. The snapshot in
`data/base_prices.json` makes all venues open AT the reference.

**Start and reset venues together.** The fundamental walks off each venue's
own price level and tick counter, so two venues restored from *different*
persisted seasons (or reset at different times) wake up at different levels
— measured live: a stale-state restart opened a 1.6% mean gap that arbitrage
then has to grind away against position limits. When you run one market on
several venues: fire **NEW SEASON on all venues in the same breath**, and
restart them together after crashes. (The Phase A session scheduler will do
both automatically.)

### Venue fee competition

Each exchange-owning team sets its own schedule — taker fee and maker rebate,
in basis points — chosen at registration and changeable live from the student
portal's **MY VENUE** section:

| Bound | Value | Why |
|-------|-------|-----|
| taker fee | 5–30 bps | below 5 bps a venue cannot fund its rebate; above 30 bps flow simply leaves |
| maker rebate | 0 bps – (taker − 2 bps) | the venue always nets at least 2 bps per matched trade, so it can never go bust by rebating |
| defaults | 15 / 10 bps | the pre-existing global default schedule |

The trade-off is the real one: a fat rebate buys market makers (tighter
quotes, more flow) but shrinks the margin on every trade; a high taker fee
maximises revenue per trade until the flow goes to the cheaper venue. Both
sides are visible — the FLOW tab's exchange card and the portal show each
venue's taker/rebate/net, and the leaderboard shows each venue's revenue.

Announcements: changing the schedule broadcasts a `FEE_SCHEDULE` SessionEvent
on that venue, so bots (including the arbitrageur, whose edge depends on it)
can react within a tick.

## 9. Environment variables

| Variable | Default | Meaning |
|----------|---------|---------|
| `GAME_WEEK` | — | Load `teacher/season/weekNN.json` at startup |
| `SCENARIO_PATH` | — | Load a specific scenario file (wins over `GAME_WEEK`) |
| `SEASON_DIR` | `teacher/season` | Where scenario files live |
| `SEASON_PATH` | `data/season.json` | Season state file |
| `SEASON_PERSIST` | `auto` | `auto` = persist only when a week is configured; `true`/`false` to force |
| `SEASON_SAVE_INTERVAL_SEC` | `60` | Checkpoint cadence while open |
| `SEASON_SNAPSHOT_TICKS` | `60` | Ticks between equity snapshots |
| `EQUITY_HISTORY_MAX` | `2000` | Snapshots kept per team |
| `SEASON_DRAWDOWN_PENALTY` | `2.0` | `PENALTY` in the scoring formula |
| `CALENDAR_ANNOUNCE_LEAD` | `120` | Ticks of advance notice for an event |
| `SHOCK_RAMP_TICKS` | `15` | Ticks a price move ramps over (1 = instant) |
| `SHOCK_OVERSHOOT` | `1.3` | Peak multiple before settling to the final level |
| `CASH_INTEREST_PER_TICK` | `0.000002` | Interest earned on positive cash |
| `REPORTS_DIR` | `data/reports` | Where per-team TCA reports are written/served |
| `UPGRADE_TIMEOUT_SEC` | `4` | How long the portal waits for the exchange's purchase verdict |
| `ORDER_QUOTA_PER_TICK` | `20` | Order/cancel messages allowed per tick (0 = unmetered) |
| `ORDER_QUOTA_BURST_MULTIPLE` | `3` | Burst allowance as a multiple of the quota |
| `CANCEL_FEE_PER_ORDER` | `0.05` | Flat fee per cancelled order |
| `CANCEL_FEE_PER_SHARE` | `0.0` | Additional fee per resting share pulled |
| `LATENCY_MS_DEFAULT` | `200` | Standard outbound latency tier |
| `LATENCY_MS_COLOCATED` | `20` | Latency for teams owning colocation |
| `FUTURES` | `ARENA10` | Comma-separated symbols settled as cash futures |
| `FUTURES_MARGIN_PER_CONTRACT` | `40` | Initial margin per contract, each side |
| `FUTURES_SETTLE_TICKS` | `300` | Ticks between variation-margin settlements |
| `FUTURES_MID_BLEND_WEIGHT` | `0.0` | How much a future's own book affects its mark |
| `MID_BLEND_WEIGHT` | `0.85` | How much a cash equity's own book (microprice) outweighs the shared fundamental |
| `MAX_TICK_MOVE` | `0.002` | Cap on one tick's fair-value move (ramps are exempt) |
| `DEAD_BOOK_DECAY` | `0.05` | Drift home per tick when a symbol has no two-sided book |
| `FUNDAMENTAL_SEED` | `algoarena-fundamental-1` | Seed of the shared news path — identical on every venue in a session |
| `FUNDAMENTAL_VOL` | `1.0` | Multiplier on every security's annualized vol (0 = flat) |
| `TAKER_FEE_RATE` | `0.0015` | Venue taker fee — **wins over the roster's per-venue schedule** |
| `MAKER_REBATE_RATE` | `0.0010` | Venue maker rebate — same precedence |
| `EXCHANGE_URLS` | — | Venues a broker quotes / the arbitrageur watches |
| `MAX_ARB_CLIP` | `20` | Largest single cross-venue arb clip (shares) |

## 10. Balance testing before you teach

Never run a new week on students without simulating it first:

```bash
make season                          # random shocks, default field
make season-weeks WEEKS=1-10 SEED=1  # every scenario file back to back
PYTHONPATH=. python scripts/season_sim.py --weeks 9 --ticks 2000 --seed 3

# "who wins this matchup?" — one line, plain-language verdict
make whowins WEEK=3 LINEUP="momentum:2,shock_predictor:1" SHOCK=flash_crash
```

Recipe-first walkthrough: [SIMULATOR_GUIDE.md](SIMULATOR_GUIDE.md).

The simulator drives the real exchange (same matching, fees, margin,
liquidation, and week gates) with a population of bots, then reports
standings versus a do-nothing control, broker survival, exchange revenue,
and **shock attribution** — how much each bot made in the window around each
event. Check three things:

1. The control benchmark is beatable but not trivially so.
2. Market makers survive the week (no `LIQUIDATED` in broker survival).
3. Foreknowledge of events pays, but not so much that nothing else matters.
