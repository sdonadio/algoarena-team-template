# AlgoArena — 5-Minute Setup

> **Playing in the hosted class competition?** This page describes *local*
> play, where you run everything on your own machine. To connect to the
> class arena the teacher hosts, follow **[CONNECTING.md](CONNECTING.md)**
> instead (`make register`, not `make team`).

Follow these steps regardless of your role (Exchange, Broker, or Trader).

---

## Step 1 — Install dependencies

```bash
pip install -r requirements.txt
```

Python 3.11 or later is required.

---

## Step 2 — Create your team

```bash
make team
```

The wizard gives your team a **$1,000,000 budget** and walks you through
investing it:

| Seat | Cost | What it does |
|------|------|--------------|
| 🏛 Exchange license | $300,000 | Run your own venue — earn the maker/taker fee spread on every trade |
| 🏦 Broker desk | $100,000+ | Quote two-sided markets — earn the bid-ask spread plus maker rebates |
| 🤖 Trader seat | $50,000+ | Run an algo strategy — maximize P&L |

Whatever you allocate to a seat becomes that bot's **starting cash** — the
exchange enforces it. Unspent budget is wasted, so invest it all.

The wizard then generates your team's code package:

```
students/<your_team>/
├── README.md    ← your exact run commands
├── config.py    ← your tunables
├── trader.py    ← MyStrategy.generate_signal() — write your edge here
└── broker.py    ← quoting + inventory management (if you bought a desk)
```

and registers you in `teacher/teams.json` so `make bots` and the dashboard
know about you.

**Brokers:** reference prices come from Yahoo Finance — no API keys needed.

---

## Step 3 — Start the exchange (exchange team runs this)

```bash
make exchange
# or: python -m exchange.server
```

The exchange prints its address. Share it with your brokers and traders.

---

## Step 4 — Connect your component

Once the exchange is running, start your role:

```bash
make broker     # Broker team
make trader     # Trader team
```

Connecting to a remote exchange:

```bash
EXCHANGE_HOST=192.168.1.10 make trader
```

You should see a connection confirmation and your team ID in the logs.

---

## Step 5 — Wait for SESSION_OPEN

Your bot connects and **waits**. Trading begins when the teacher presses
**START** on the dashboard — at that moment every participant receives
$100,000 cash plus a starting inventory of shares, and your bot's trading
loop activates. Watch yourself live at the teacher's dashboard
(`http://<teacher-ip>:8888`) — MARKET tab for the leaderboard, FLOW tab
for the animated flow map.

---

## Verify everything is working

Run the offline simulation — no exchange or network needed:

```bash
make sim
```

It runs a full session in a fraction of a second and prints a P&L chart.
If you see the leaderboard, your environment is set up correctly.

---

## Useful environment variables

```bash
EXCHANGE_HOST=192.168.1.10   # connect to a remote exchange
EXCHANGE_PORT=8765           # override default port
TEAM_ID=my_team_name         # override config.py without editing it
```

---

## Run the tests

```bash
make test
```

All 215 tests should pass before you submit.
