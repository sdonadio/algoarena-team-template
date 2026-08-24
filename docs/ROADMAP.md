# The Build Roadmap — every TODO, week by week

Every `TODO` marker in this codebase is an assignment. This page collects
all of them into one plan: what your team implements, where it lives, which
week it's due, and which SDK hook it lands in. Levels follow the unlock
table; the arena enforces week gates, so code shipped early simply waits.

Roles: 🤖 Trader · 🏦 Broker · 🏛 Exchange. Your team does the rows for the
seats it owns.

Note on paths: `engine/…` is the **student template** layout —
`scripts/make_template_repo.py` copies the engine packages into `engine/`.
In this repo the same files live at `shared/`, `exchange/`, `broker/`,
`trader/`.

---

## Level 1 — Connect (Weeks 1–2)

Already working in your starters. Your job is to run it and understand it.

| Role | Task | Where |
|---|---|---|
| all | Register, pass `make test-remote`, see yourself on the dashboard | `docs/CONNECTING.md` |
| 🤖 | Read `on_tick`, place your first manual signal | `team/trader.py` |
| 🏦 | Watch your desk quote a fixed spread | `team/broker.py` (empty subclass quotes) |
| 🏛 | Run your venue, watch a trade match | `team/exchange.py` |

## Level 2 — Live state (Week 2–3)

| Role | Task | Where |
|---|---|---|
| 🤖 | Track your portfolio in real time; log fills via `on_fill` | `team/trader.py` |
| 🏦 | **Smart requoting** — requote only when the price moves enough, not every tick | `engine/broker/broker.py` `needs_requote` (study), tune `REQUOTE_THRESHOLD_BPS` |
| 🏛 | Portfolio tracking + leaderboard broadcast (study the engine's) | `engine/exchange/server.py` |

## Level 3 — Your edge (Weeks 3–4) · *purchase window opens week 4*

| Role | Task | Where |
|---|---|---|
| 🤖 | **A real signal**: momentum (OPTION A) or mean reversion (OPTION B) — or your own | `team/trader.py → on_tick()`; worked options in `engine/trader/trader.py` |
| 🏦 | **Volatility-adjusted spread** — widen in choppy markets | `team/broker.py → spread()` (history arg is your input) |
| 🏛 | **Maker/taker fee model** — replace the flat 50/50 split with rebate/charge | `engine/shared/orderbook.py` (TODO Level 3); publish your schedule in `team/exchange.py` |

## Level 4 — Survive (Week 5)

| Role | Task | Where |
|---|---|---|
| 🤖 | **Real risk checks**: position limits, cash sufficiency, daily-loss halt | `engine/trader/trader.py → RiskManager.check_order` (TODO Level 4) |
| 🤖 | **Stop-loss** — close positions past `STOP_LOSS_PCT` | `RiskManager.check_stop_loss` (TODO Level 4) |
| 🏦 | **Inventory management** — skew quotes to shed exposure; this is what keeps you solvent under margin calls | `team/broker.py → skew(symbol, inventory)` |
| 🏛 | **Circuit breakers** — halt a symbol after an outsized move, auto-resume | `engine/shared/orderbook.py` (TODO Level 4) |
| 🏛 | *(advanced)* **LULD limit states** — pause trading at the band edge before escalating to a halt, the way real LULD does | `engine/exchange/circuit_breaker.py`; band config `LULD_BAND_PCT` |
| 🏛 | *(advanced)* Kyle's lambda — volume-weighted permanent impact | `engine/exchange/price_engine.py` (TODO Level 4) |

## Level 5 — The meta-game (Weeks 6–7) · *purchase window opens week 7*

| Role | Task | Where |
|---|---|---|
| 🤖 | **Trade the calendar** (OPTION G): events are announced with timing but not direction — store them via `on_event`, cut risk into the print or straddle it | `team/trader.py → on_event()`; hints at `engine/trader/trader.py` (TODO Level 5) |
| 🤖 | **Use stops properly**: venue-held `stop`/`stop_limit` as disaster insurance — and understand why a stop cascade IS the flash crash | `team/trader.py` (send `order_type="stop"`, `stop_price=…`) |
| 🏦 | **Toxic flow detection** — identify counterparties who pick you off, stop quoting to them. The retail/VWAP background flow gives you a baseline to segment against. | `team/broker.py → toxic(trader_id)` |
| 🏦 | *(advanced)* **Iceberg quotes** — show small, reload from reserve; the venue only sees the tip | build it client-side: requote a slice on each fill |
| 🏛 | **Rate limiting**: order-to-trade ratio enforcement + per-team order quotas | `engine/shared/orderbook.py` (TODO Level 5), `engine/exchange/config.py` `MAX_ORDERS_PER_MIN_PER_TEAM` |
| 🏛 | *(advanced)* **Odd and round lots** — accept odd lots but exclude them from the published BBO, the way real venues quote in round lots | `engine/shared/orderbook.py` + `engine/exchange/server.py` snapshot code |
| 🏛 | **Detect the whale**: the institutional VWAP slicer fires on a fixed clock — find it in your venue's flow analytics | `team/exchange.py → on_trade()` |

## The IPO weeks (3 and 8)

| Role | Task | Where |
|---|---|---|
| 🤖 | **Play the primary market**: implement `on_ipo` — size your indication against the range, the book heat, and your cash | `team/trader.py → on_ipo()` |
| 🤖 | **Trade the listing**: the price starts at the offer and walks to its hidden value — momentum vs fade on listing day | `on_event("IPO_LISTED")` + `on_tick` |
| 🏦 | **Quote the new name**: nobody has inventory except allocants — spreads are wide and the first market maker in earns them | `team/broker.py` |

## Level 6 — Full markets (Weeks 8–9)

| Role | Task | Where |
|---|---|---|
| 🤖 | **Order-book imbalance** (OPTION C) or **pairs/relative value** (OPTION D) | `team/trader.py`; sketches in `engine/trader/trader.py` |
| 🤖 | **Multi-venue smart order routing** (OPTION F): quote gap across venues → buy cheap, sell dear. The capstone. | `team/trader.py` + `EXCHANGE_URLS` (TODO Level 6) |
| 🤖 | ML signal (OPTION E): train offline on recorded sessions, deploy in `on_tick` | `scripts/backtest.py` for data, your model |
| 🏦 | Multi-exchange quoting from one pricing brain | handled by the SDK — your job is tuning it per venue |
| 🏛 | **VWAP + analytics endpoint** — broadcast VWAP, expose venue stats worth paying for | `engine/exchange/server.py` (TODO Level 6) |
| 🏛 | *(advanced)* **Pegged orders** — quotes that track the NBBO midpoint automatically; consume `GET /api/nbbo` | new order type: your venue, your rules |
| 🏛 | *(advanced)* **Market-data tiering** — sell depth-of-book as a paid upgrade: free tier gets top-of-book, subscribers get the full ladder | `engine/exchange/server.py` snapshots + the upgrade shop (`engine/exchange/upgrades.py`) |
| 🤖 | **Auction strategy**: the opening cross publishes indicative price and imbalance every tick — trade the imbalance, or game the close (MOC manipulation is a compliance lecture waiting to happen) | `on_event("AUCTION_INDICATIVE")` |

---

## How this is graded

- Each level's work must be **committed to your repo** by the end of its
  week — the git history is the audit trail.
- The arena's week gates mean a session only exercises the levels unlocked
  so far; the season leaderboard scores each role on risk-adjusted return,
  not raw P&L.
- Backtest before you deploy: `make sim` runs the same matching engine, and
  `scripts/backtest.py` replays real recorded sessions through your code.

## Course-topic tie-ins

| Week | Course topic | Roadmap work it powers |
|---|---|---|
| 1–2 | UML/OOP, concurrency | Level 1–2: the SDK classes ARE the object model; async plumbing |
| 3 | Sockets & protocols | Level 2–3: the wire protocol in `engine/shared/messages.py` |
| 4 | Backtester architecture | Level 3 signals validated offline before going live |
| 5 | Complexity & data structures | Level 4: the order book's price-time priority, O(·) of matching |
| 6 | Big data | Level 5: session recordings → flow analysis (who is toxic?) |
| 7 | ML & model ops | Level 6 OPTION E: train → backtest → deploy → monitor |
| 8 | CI/CD & testing | Your repo's CI gates every level's merge |
| 9 | Integration | Level 6 multi-venue: your bots + everyone's venues, live |
