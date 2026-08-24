# AlgoArena — Student FAQ

Real questions, asked while running this game, answered from the code.
File and constant names are given so you can verify everything yourself —
the engine you're playing against ships in your own repo.

Deeper guides: [QUICKSTART](QUICKSTART.md) · [CONNECTING](CONNECTING.md) ·
[TRADER_GUIDE](TRADER_GUIDE.md) · [BROKER_GUIDE](BROKER_GUIDE.md) ·
[EXCHANGE_GUIDE](EXCHANGE_GUIDE.md) · [SEASON_GUIDE](SEASON_GUIDE.md)

---

## Getting connected

**Q: `make register` says my team name already exists — I never finished registering!**
If your first attempt failed *after* the request reached the arena (flaky
network, proxy), the server created your team but you never received the
token — a "ghost" registration. Pick a different name, or ask your teacher
to remove the ghost entry so you can re-register under the same name. Your
token is shown exactly once, at successful registration.

**Q: Registration failed with an HTML error page / weird 403.**
That error didn't come from the arena — a proxy or firewall on your network
intercepted the request (campus guest Wi-Fi and corporate networks do
this). The wizard detects this and says so. Try another network (a phone
hotspot usually works), then register again.

**Q: I re-ran `make register` — did it overwrite my strategy?!**
No. Starter code files (`team/trader.py`, `team/broker.py`) are never
clobbered: if yours differ from the fresh starter, the new copy is written
to `team/trader.py.new` beside your file. Registration artifacts that must
match your new registration (`team/config.py`, `team/README.md`) are
replaced, with your previous version saved to `.bak`.

**Q: Do I have to `export` the variables from `.env` before running my bot?**
No. Bots auto-load `.env` from the current directory at startup
(`shared/envfile.py`) — so `TEAM_ID=<your_bot_id> python -m team.trader`
just works after `make register`. Variables already set in your shell win
over the file, so explicit overrides still behave. Never commit `.env`.

**Q: How do I connect to a TLS-hosted arena (https:// URL)?**
If you registered against an `https://` arena address, your `.env` already
contains `EXCHANGE_URL=wss://feed.<domain>` and your bots use it
automatically. That keeps your team token encrypted in transit — never
connect with plain `ws://` to a hosted arena.

**Q: My bot connected but the market was already open — did I miss a start signal?**
On the hosted arena the market runs continuously (it opens itself when the
venue starts). Your bot receives `SESSION_OPEN` state on connect and can
trade immediately; there is no bell to wait for outside scheduled class
sessions.

**Q: My bot stopped trading after a market restart and never came back.**
Old template versions had a reconnect bug: when the venue closed the
connection *cleanly* (a restart does), the bot sat forever on a dead
socket. Fixed — `git pull upstream main` in your team repo. Your bot's
`run()` loop now detects the drop, cancels the trading loop, and
reconnects with fresh state (see `trader/trader.py`). Lesson included at
no charge: code paths that have never run have never worked.

---

## Money & accounting

**Q: What happens when my cash goes negative?**
You've taken a margin loan from the exchange — allowed, priced, and
bounded. Buying power is `cash + MARGIN_HAIRCUT × long market value`
(haircut 0.5, `exchange/config.py`). Every tick you pay interest on the
borrowed amount (`MARGIN_RATE_PER_TICK`) and borrow fees on any short
notional (`BORROW_FEE_PER_TICK`) — you'll see it accumulate as *carry* in
your portfolio. The hard stop: if your net worth falls below
`MAINTENANCE_FRACTION` × your starting capital, the exchange
force-liquidates your whole book at market. The `risk_shield` upgrade
(shop) waives exactly one margin call — it's consumable.

**Q: Why did I start with shares I never bought?**
Every participant receives `STARTING_SHARES_PER_SYMBOL` (default 20)
shares of each listed symbol on connect (`exchange/config.py`). Without
starting inventory nobody could sell in the opening minutes and the first
book would be bid-only. The shares are marked at the reference price when
granted, so they arrive P&L-neutral.

**Q: Why is my starting cash different from another team's?**
You chose it. At registration your team split a fixed budget across an
exchange license, broker desks, and trader seats — each bot's allocation
IS its starting cash, enforced by the exchange at connect. The number in
`team/config.py` (`CAPITAL`) is what you allocated, not a default.

**Q: Is the game zero-sum? Who takes money from whom?**
Trading itself is zero-sum: a trade just swaps cash for shares at the
print, and wealth moves only when prices move afterwards — one side's
mark-to-market gain is exactly the other side's loss. What makes the game
slightly *negative*-sum for participants are the costs that sit outside
that exchange: taker fees, margin interest, and borrow fees flow to the
venue (and maker rebates flow back out of it). So: traders and brokers
take from each other; the exchange takes from everyone who crosses the
spread. Plan your strategy net of costs.

**Q: What's a maker vs a taker, and why does my maker ratio matter?**
The resting order that gets hit *made* liquidity; the incoming order that
crossed the spread *took* it. Takers pay `TAKER_FEE_RATE` (0.15% of
notional by default); makers earn `MAKER_REBATE_RATE` (0.10%). A trader
who always takes pays the spread AND the fee on every idea — the idea has
to clear both. A broker below roughly 60% maker isn't really market
making; it's chasing. The STATS page shows your MAKER % — watch it.

---

## Market mechanics

**Q: On the FLOW tab, what does "shares in circulation" (e.g. 140/140) mean?**
The float: how many shares of a symbol exist in the game. It's not a fixed
constant — it's (connected participants × `STARTING_SHARES_PER_SYMBOL`)
plus any shares issued in IPOs. Seven participants × 20 = 140. The bar is
a conservation check: green means every share is accounted for in
someone's portfolio; if it drops mid-session while everyone stays
connected, something is wrong and your teacher will be very interested.

**Q: My bot gets RATE_LIMITED right after connecting. Is the exchange broken?**
No — you're over the message quota (`ORDER_QUOTA_PER_TICK`, default 20
order/cancel messages per tick, with a small burst allowance). The classic
cause: quoting every symbol at once on connect — 10 symbols × a
multi-level two-sided ladder is 60+ messages in one tick. The template
broker now staggers its opening quotes (`QUOTE_BURST_SYMBOLS` per pass,
`broker/config.py`); if you write your own quoting loop, budget your
messages. Cancels count against the quota too.

**Q: A symbol stopped trading mid-session — my orders get rejected. Why?**
Circuit breakers. A large move halts the symbol (LULD bands), and a
sharp drop can trigger the short-sale restriction (SSR). Rejections
during a halt are the rules working, not a bug — read the rejection code
your bot receives, and see [SEASON_GUIDE](SEASON_GUIDE.md) for which
weeks these are armed.

---

## Rankings & grading

**Q: How are we ranked?**
Within your role, never across roles: traders against traders, brokers
against brokers, exchange teams by venue fee revenue. Comparing a
broker's P&L to a trader's would be meaningless — the money comes from
different mechanisms. Season standings are **risk-adjusted** (return
scaled by volatility and drawdown of your equity curve, computed in
`exchange/scoring.py`) — a smaller, steadier profit beats a lucky
rollercoaster.

**Q: What is the EQUITY CURVE column on the STATS page?**
A sparkline of your bot's net worth over the recent leaderboard updates —
your equity curve. It's the picture behind the numbers next to it: SHARPE
and MAX DD are computed from exactly that curve. A flat-then-cliff curve
and a smooth climb can have the same final P&L and very different scores.

**Q: If my P&L is positive, am I doing well?**
Not necessarily — check what it cost. If your total fees exceed your
|P&L|, you're overtrading: the edge exists but is smaller than the cost of
expressing it. If your unrealized losses dwarf realized gains, you're
holding losers. The dashboard shows all of these per bot.

---

## Tech under the hood

**Q: What actually moves between my bot and the exchange?**
JSON messages over a WebSocket, every one validated by a Pydantic schema
in `shared/messages.py` — the wire protocol is typed, versioned by
addition, and the same in both directions. Your bot never parses raw
dicts; it gets `BookSnapshot`, `TradeExecution`, `PortfolioUpdate`, …
objects. If you send something malformed, validation rejects it before
the matching engine ever sees it.

**Q: Why asyncio everywhere?**
One bot juggles a WebSocket listener, a trading/quoting loop, and timers
concurrently — that's I/O-bound concurrency, asyncio's home turf. The one
exception is the broker's Yahoo Finance poller, which is blocking HTTP
and therefore lives on a thread, off the event loop. Never call blocking
code inside an `async def` — it freezes every task in the process.

**Q: Where does all the data go? Can I analyze past sessions?**
Every broadcast of every session is appended to a JSONL recording — one
`{ts, msg}` object per line, streamable with constant memory, and
greppable. That tape is the dataset for your feature pipelines, the input
to `scripts/backtest.py` (replay your strategy against a recorded
session), and the evidence everything is graded from. The offline
simulator (`make sim`) needs no network at all — see
[SIMULATOR_GUIDE](SIMULATOR_GUIDE.md) and `tests/sim_session.py`.

**Q: Is my local engine copy the real market?**
No — the hosted arena runs the teacher's engine; your copy exists so you
can read the rules, run the simulator, and backtest. Engine updates are
announced; pull them with `git remote add upstream <template-url> &&
git pull upstream main`. Your `team/` directory and `.env` are never in
the template, so pulls won't touch your strategy.
