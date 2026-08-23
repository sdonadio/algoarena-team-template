# Connecting to the Hosted Arena

The class competition runs on a shared server the teacher operates. You never
run the exchange yourself — your bots connect to it over the internet, like a
trading firm connecting to a real exchange.

Your teacher gives you two things in class:

| | Example | What it's for |
|---|---|---|
| **Arena address** | `http://arena-host:8888` | Registration + the live dashboard |
| **Registration code** | `something-secret` | Proves you're in the class |

Everything below uses the arena address; replace it with the one announced
in class.

---

## 1. Register your team (once, one teammate does this)

From your team repo:

```bash
make register
```

The wizard asks for the arena address and the registration code, checks the
code before anything else, then walks your team's **$1,000,000 budget**
across broker desks and trader seats. When it finishes you have:

- `team/` — your bot code (trader + broker starters, your run commands in
  `team/README.md`)
- `.env` — your **team token** and the arena host:

```
ARENA_TOKEN=<32-character secret>
EXCHANGE_HOST=<arena host>
```

**The token IS your team's identity.** Every order your bots send is
authenticated with it, and it controls your team's capital. It is shown once.
Never commit `.env` (the repo's `.gitignore` already blocks it), never post
it in chat, never share it outside your team. If it leaks, tell the teacher
immediately so it can be rotated.

---

## 2. Prove the connection works

```bash
make test-remote CODE=<registration code>
```

This runs the whole pipeline against the live arena and prints a checklist:
dashboard reachable → code accepted → handshake authenticated → market data
flowing → order accepted → **a real trade executes** → portfolio updates.
All green means your machine, network, and token are ready. Common failures:

| Symptom | Cause |
|---|---|
| `dashboard reachable ✗` | Wrong address, or you're on a network blocking the port — try another network / hotspot |
| `registration code accepted ✗` | Typo in the code, or the teacher rotated it |
| `handshake authenticated ✗ AUTH_FAILED` | Wrong/missing `ARENA_TOKEN`, or bot id doesn't belong to your team |
| `no fill — is the session open?` | The market only trades when the teacher has opened the session |

---

## 3. Run your bots

Bot ids are listed in `team/README.md`. Each bot is one process:

```bash
TEAM_ID=<your_team>_trader_1 python -m team.trader
TEAM_ID=<your_team>_broker   python -m team.broker    # if you bought a desk
```

What happens when a bot starts:

1. It reads `ARENA_TOKEN` and `EXCHANGE_HOST` from `.env`.
2. It opens a WebSocket to the exchange (port `8765`) and sends a
   `Handshake` with its id, role, and your token.
3. On success it receives the current order books and leaderboard. If the
   session is already open it activates immediately; otherwise it waits for
   the teacher's START.
4. From then on: your `MyTrader.on_tick()` runs every tick, orders
   go out, fills and portfolio updates come back.

Bots reconnect automatically if the connection drops — you can also just
restart the process any time, even mid-session; your portfolio lives on the
server, not in your process.

**Run your bots whenever you want to test.** The arena is up between classes;
scores only count during official sessions.

---

## 4. Watch yourself on the dashboard

Open the arena address in a browser and **sign in with your team token**:

- **MARKET / FLOW / STATS** — the shared view everyone sees: leaderboard,
  live tape, analytics. Your team's prints show in your team color.
- **MY TEAM** — private to you: per-bot portfolio, cash, positions, fills,
  and the buttons for buying extra seats and upgrades during capital
  windows (weeks 4 and 7).

---

## 5. Iterate

Your edge lives in `team/trader.py` (`MyTrader.on_tick()`) and
`team/broker.py` (`MyBroker.spread()/skew()`) — small classes deriving the
`arena` SDK bases, which handle all plumbing. The loop is:

```bash
make test          # offline unit tests (no network)
make sim           # full simulated session on your machine (no network)
TEAM_ID=... python -m team.trader     # live against the class arena
```

Test offline first — the sim runs the same matching engine the arena uses.
The arena is the real market: other teams' bots are trading there too, and
your P&L is theirs to take.

---

## The rules of the market

The arena enforces the same microstructure rules real US equity venues do.
Your bot will meet these — each rejection names its rule (watch **MY TEAM →
DESK EVENTS** when orders seem to vanish):

| Rule | What it means for you | Reject code |
|---|---|---|
| **Penny ticks** | Prices snap to $0.01, toward the passive side (buys round down, sells up). Queue priority is real — you can't sub-penny the queue. | — (snapped silently; the ack shows the real price) |
| **Self-trade prevention** | Your order never fills against your own resting order — the resting one is cancelled instead (you'll see `STP_CANCEL`). Wash trades are impossible. | `STP_CANCEL` |
| **LULD price band** | Limit prices further than ~10% from the venue mark are rejected — the fat-finger collar. | `PRICE_BAND` |
| **Short-sale rule** | A symbol down 10% from the open goes under SSR: new shorts must be limit orders resting above the bid. | `SSR_RESTRICTED` |
| **Borrow locate** | Total short interest per symbol is capped market-wide. No borrow, no short. | `BORROW_UNAVAILABLE` |
| **Halts** | Per-symbol velocity/session halts and market-wide circuit breakers (−7/−13/−20%). | `SYMBOL_HALTED` |
| **Order quota** | Cancel/replace spam is metered per tick. | `RATE_LIMITED` |

**Order types**: `limit`, `market`, `ioc`, `post_only` (maker-rebate
guaranteed), plus **`stop`** and **`stop_limit`** — held at the venue, armed
at `stop_price`, fired by prints or the mark.

**Auctions**: when enabled, START first runs a pre-open — only limit orders,
resting unmatched, with indicative price/imbalance broadcast every tick —
then one volume-maximizing cross opens the market (`SESSION_PREOPEN` /
`AUCTION_INDICATIVE` / `AUCTION_RESULT` events). A closing auction can
mirror it at the close. Orders sent during an auction window that aren't
limit/post-only get `AUCTION_ONLY_LIMIT`.

**A note on fee scale**: the arena's fees (~15 bps taker / 10 bps maker
rebate) are deliberately ~50× real-world equity fees (real venues charge
about $0.0030/share ≈ 0.3 bps). Exaggerated fees make execution costs
*teachable* — you feel them in a 30-minute session. The mechanics are
faithful; recalibrate the magnitudes before quoting them in a real job.

---

## The consolidated tape and the order ticket

- **`GET <arena>/api/nbbo`** — the SIP: national best bid/offer across every
  venue with venue attribution, plus the last print. If `bid > ask` across
  two venues, that's a real arbitrage (Level 6, OPTION F).
- **MY TEAM → ORDER TICKET** — trade by hand as one of your bots, through
  the exact same order path your code uses. Feel the spread, pay the fee,
  see the fill on FLOW, then go automate it.

---

## IPOs

Some weeks a new stock lists. The deal is announced at the open (range,
shares, book window); subscribe from your bot:

```python
class MyTrader(Trader):
    def on_ipo(self, symbol, lo, hi, shares, data):
        return 200            # bid 200 shares at the top of the range
        # or: return (200, (lo + hi) / 2)   # bid tighter, risk missing it
```

or by hand from **MY TEAM → IPO DESK**. One indication per bot —
resubmitting replaces. Cash is debited only if you're allocated at
pricing; oversubscribed books allocate pro-rata. The listing starts at the
offer price and finds its level in the open market — it may pop, it may
break. `IPO_*` events carry the state; watch the tape at the listing tick.

---

## Choosing a venue — fee schedules

When the game runs **multiple exchanges** (student teams can license their
own venue), all venues list the same securities but **charge different
fees** — that's how they compete for your flow, exactly like real markets.
Where you route directly changes your P&L:

- **Taker fee** — you pay it when your order *takes* liquidity (crosses the
  spread and fills immediately).
- **Maker rebate** — you *earn* it when your resting order is filled by
  someone else.

Every venue's published schedule is public — check it before you route:

- **Dashboard → STATS tab → "Venue fee schedules"** — the comparison table.
- **From code**: `GET <arena>/api/venues` (no token needed):

```python
import json, urllib.request

with urllib.request.urlopen(f"{ARENA_URL}/api/venues") as r:
    for v in json.load(r)["venues"]:
        print(f'{v["venue"]:24} port {v["port"]}  '
              f'taker {v["taker_bps"]} bps / rebate {v["rebate_bps"]} bps')
```

Point a bot at a specific venue with `EXCHANGE_PORT=<port>` (or give a
multi-venue bot every venue via `EXCHANGE_URLS=ws://host:p1,ws://host:p2`).
Back-of-envelope: on a $10,000 fill, each bps is $1 — a venue charging
12 bps instead of 15 saves $3 per trade, which compounds fast at high
frequency. Venues can retune their schedule between sessions (within
teacher-set bounds), so glance at the table each week. Your fills always
show what you actually paid (`fee`) or earned (`maker_rebate`).

---

## FAQ

**Do I need AWS credentials or SSH?** No. The server is the teacher's
problem. You need exactly: the arena address, the registration code (once),
and your team token (generated for you).

**Can I run bots from my laptop on campus WiFi?** Yes — connections are
outbound WebSockets, which work from anywhere that doesn't block the arena's
ports. If campus WiFi misbehaves, a phone hotspot is a fine fallback.

**Two teammates run the same bot id at once?** Don't. The second connection
replaces the first. Split work by bot id — one teammate per seat.

**We lost our token.** The teacher re-registers your team, which issues a
new token (old one stops working). Your season P&L survives — it's keyed to
your team, not the token.
