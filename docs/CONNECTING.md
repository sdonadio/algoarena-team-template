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
4. From then on: your `MyStrategy.generate_signal()` runs every tick, orders
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

Your edge lives in `team/trader.py` (`MyStrategy.generate_signal()`) and
`team/broker.py`. The loop is:

```bash
make test          # offline unit tests (no network)
make sim           # full simulated session on your machine (no network)
TEAM_ID=... python -m team.trader     # live against the class arena
```

Test offline first — the sim runs the same matching engine the arena uses.
The arena is the real market: other teams' bots are trading there too, and
your P&L is theirs to take.

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
